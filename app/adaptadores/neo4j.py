#!/usr/bin/env python3
"""Adaptador de respaldo y restauracion para Neo4j.

Expone DOS estrategias distintas:

  1. `dump_binario`    -> neo4j-admin database dump, un archivo unico del
                          almacenamiento, que se restaura con el comando load.
  2. `exportacion_cypher` -> exportacion de nodos y aristas a JSON Lines via
                          Cypher, que se restaura reejecutando las consultas.

LIMITACION REAL Y MEDIDA DE ESTA MAQUINA: en la edicion Community 5.24 el
subcomando dump se niega a trabajar sobre una base montada. Devuelve el error
"The database is in use" y aun asi termina con codigo de salida 0. Por eso la
estrategia binaria obliga a detener la instancia, y el adaptador mide y deja
constancia de ese costo en vez de ocultarlo.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Iterator

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from .. import config
from ..utileria import (
    copiar_arbol,
    cronometro,
    escribir_jsonl,
    leer_jsonl,
    sha256_de_archivo,
    sha256_de_manifiesto,
    tamano_de_ruta,
    verificar_manifiesto,
)

NOMBRE_NEO4J = "Neo4j"
VERSION_NEO4J = "5.24.0"

# neo4j-admin es un envoltorio de Java: sin JAVA_HOME no arranca.
JAVA_HOME = "/usr/lib/jvm/java-21-openjdk-amd64"


ESTRATEGIAS = {
    "dump_binario": {
        "nombre": "Dump binario con neo4j-admin",
        "descripcion": (
            "neo4j-admin database dump empaqueta la base completa en un solo "
            "archivo .dump con los registros, los indices y los registros de "
            "transaccion. Es una foto exacta del almacenamiento."
        ),
        "cuando_usarla": (
            "Restauraciones completas y rapidas sobre la misma version de "
            "Neo4j, por ejemplo al recuperar un servidor caido."
        ),
        "protege": (
            "Todo el contenido del grafo mas los indices internos y el punto "
            "de control de transacciones."
        ),
        "no_protege": (
            "No se puede leer ni consultar sin restaurarlo, no se puede "
            "cargar en otra version mayor y, en la edicion Community, exige "
            "que la base este detenida mientras se hace y mientras se carga."
        ),
        "requiere_parar": True,
    },
    "exportacion_cypher": {
        "nombre": "Exportacion a JSON Lines con Cypher",
        "descripcion": (
            "Extrae los nodos y las aristas en consultas separadas y los "
            "escribe como texto JSON Lines, con los identificadores "
            "reemplazados por claves de negocio para poder reconstruirlos."
        ),
        "cuando_usarla": (
            "Cuando el archivo tiene que ser legible, auditable, comparable "
            "entre versiones o versionable en un repositorio de codigo."
        ),
        "protege": (
            "Nodos, etiquetas, propiedades y relaciones con sus tipos y "
            "propiedades, en un formato que se puede revisar a mano."
        ),
        "no_protege": (
            "Los identificadores internos de Neo4j, los indices, las "
            "restricciones, los permisos y el historial de transacciones. "
            "Reconstruir el grafo cuesta tiempo si es muy grande."
        ),
        "requiere_parar": False,
    },
}


# --- Conexion ----------------------------------------------------------------


def conectar_neo4j(
    uri: str = config.NEO4J_URI,
    usuario: str = config.NEO4J_USUARIO,
    clave: str = config.NEO4J_CLAVE,
):
    """Abre un driver de Bolt y comprueba que la instancia responde."""
    autenticacion = (usuario, clave) if usuario else None
    driver = GraphDatabase.driver(uri, auth=autenticacion)
    driver.verify_connectivity()
    return driver


def version_neo4j(driver) -> str:
    with driver.session() as sesion:
        registro = sesion.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS v"
        ).single()
    return registro["v"]


def contar_grafo(sesion, base: str = None) -> dict:
    """Cuenta nodos y relaciones de una base, o de la base por defecto."""
    consulta_nodos = "MATCH (n) RETURN count(n) AS nodos"
    consulta_aristas = "MATCH ()-[r]->() RETURN count(r) AS aristas"
    nodos = sesion.run(consulta_nodos).single()["nodos"]
    aristas = sesion.run(consulta_aristas).single()["aristas"]
    etiquetas = {
        registro["etiqueta"]: registro["n"]
        for registro in sesion.run(
            "MATCH (n) UNWIND labels(n) AS etiqueta "
            "RETURN etiqueta, count(*) AS n ORDER BY etiqueta"
        )
    }
    return {"nodos": nodos, "relaciones": aristas, "por_etiqueta": etiquetas}


# --- Estrategia 1: dump binario ---------------------------------------------


def _entorno_admin() -> dict:
    """Entorno con JAVA_HOME, necesario para que corra neo4j-admin."""
    entorno = dict(os.environ)
    entorno["JAVA_HOME"] = JAVA_HOME
    return entorno


def generar_dump_binario(
    destino: str,
    base: str = "neo4j",
    instancia_satelite: str = None,
) -> dict:
    """Ejecuta neo4j-admin database dump sobre la instancia indicada.

    En Community la base debe estar desmontada. Si se apunta a la instancia
    principal con el servidor encendido, el comando falla con "The database is
    in use" y devuelve 0, asi que ademas del codigo se mira si el archivo
    aparecio de verdad.
    """
    os.makedirs(destino, exist_ok=True)
    inicio = time.perf_counter()
    resultado = subprocess.run(
        [
            config.NEO4J_ADMIN_BIN,
            "database",
            "dump",
            base,
            f"--to-path={destino}",
            "--overwrite-destination=true",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        env=_entorno_admin() if instancia_satelite is None else _entorno_satelite(instancia_satelite),
        cwd=instancia_satelite or config.NEO4J_DIR,
    )
    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)

    archivo = os.path.join(destino, f"{base}.dump")
    combinado = resultado.stdout + resultado.stderr
    if not os.path.isfile(archivo):
        raise RuntimeError(
            "neo4j-admin database dump no produjo el archivo "
            f"{archivo}. Salida del comando: {combinado.strip()[-500:]}"
        )

    # --info sirve para auditar el archivo sin cargarlo.
    info = subprocess.run(
        [config.NEO4J_ADMIN_BIN, "database", "load", base, f"--from-path={destino}", "--info"],
        capture_output=True,
        text=True,
        timeout=600,
        env=_entorno_admin(),
        cwd=config.NEO4J_DIR,
    )

    manifiesto = os.path.join(destino, "manifiesto.json")
    from ..utileria import escribir_manifiesto

    escribir_manifiesto(manifiesto, destino)

    return {
        "estrategia": "dump_binario",
        "archivo": archivo,
        "manifiesto": manifiesto,
        "bytes": os.path.getsize(archivo),
        "duracion_ms": duracion_ms,
        "codigo": resultado.returncode,
        "info_artefacto": [linea for linea in info.stdout.split("\n") if linea.strip()][-6:],
    }


def _entorno_satelite(ruta_instancia: str) -> dict:
    """Entorno de ejecucion apuntando a una instancia satelite."""
    entorno = _entorno_admin()
    entorno["NEO4J_HOME"] = ruta_instancia
    return entorno


def restaurar_dump_binario(
    origen: str,
    base_destino: str,
    instancia_destino: str,
) -> dict:
    """Carga el dump en una instancia detenida con neo4j-admin database load.

    Igual que el dump, load exige que la base de destino NO este montada. Por
    eso la instancia destino se deja apagada mientras se carga.
    """
    inicio = time.perf_counter()
    resultado = subprocess.run(
        [
            config.NEO4J_ADMIN_BIN,
            "database",
            "load",
            base_destino,
            f"--from-path={origen}",
            "--overwrite-destination=true",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        env=_entorno_satelite(instancia_destino),
        cwd=instancia_destino,
    )
    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)
    combinado = resultado.stdout + resultado.stderr

    if "Load failed" in combinado or "Failed" in combinado:
        raise RuntimeError(f"neo4j-admin database load fallo: {combinado.strip()[-500:]}")

    return {
        "estrategia": "dump_binario",
        "base_destino": base_destino,
        "duracion_ms": duracion_ms,
        "codigo": resultado.returncode,
        "salida": combinado.strip()[-400:],
    }


# --- Estrategia 2: exportacion a JSON Lines con Cypher ----------------------


def exportar_nodos_cypher(
    sesion,
    ruta: str,
    clave_negocio: str = "codigo",
) -> dict:
    """Escribe todos los nodos como JSON Lines.

    El identificador interno de Neo4j no se guarda: se guarda una propiedad
    que actua como clave de negocio. Al restaurar se reconstruyen los enlaces
    por ahi, que es la unica forma de que un grafo recargado siga siendo el
    mismo grafo y no un conjunto de nodos sueltos.
    """
    inicio = time.perf_counter()
    consulta = (
        "MATCH (n) "
        f"RETURN elementId(n) AS interno, "
        f"coalesce(n.{clave_negocio}, '') AS clave, "
        "labels(n) AS etiquetas, properties(n) AS propiedades "
        "ORDER BY clave, etiquetas"
    )
    filas = []
    claves_vacias = 0
    for registro in sesion.run(consulta):
        if not registro["clave"]:
            claves_vacias += 1
        filas.append(
            {
                "interno": registro["interno"],
                "clave": registro["clave"],
                "etiquetas": registro["etiquetas"],
                "propiedades": _serializable(registro["propiedades"]),
            }
        )
    total = escribir_jsonl(ruta, filas)
    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)
    return {
        "ruta": ruta,
        "nodos": total,
        "duracion_ms": duracion_ms,
        "nodos_sin_clave": claves_vacias,
    }


def exportar_aristas_cypher(
    sesion,
    ruta: str,
    clave_negocio: str = "codigo",
) -> dict:
    """Escribe todas las relaciones como JSON Lines, en formato liviano.

    Cada linea lleva la clave de los dos extremos y el tipo y las propiedades
    de la relacion. Es autodescriptivo: se puede leer sin el archivo de nodos.
    """
    inicio = time.perf_counter()
    consulta = (
        "MATCH (a)-[r]->(b) "
        f"RETURN coalesce(a.{clave_negocio}, '') AS origen, "
        f"coalesce(b.{clave_negocio}, '') AS destino, "
        "type(r) AS tipo, properties(r) AS propiedades "
        "ORDER BY origen, destino, tipo"
    )
    filas = [
        {
            "origen": registro["origen"],
            "destino": registro["destino"],
            "tipo": registro["tipo"],
            "propiedades": _serializable(registro["propiedades"]),
        }
        for registro in sesion.run(consulta)
    ]
    total = escribir_jsonl(ruta, filas)
    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)
    return {
        "ruta": ruta,
        "relaciones": total,
        "duracion_ms": duracion_ms,
    }


def _serializable(valor: Any) -> Any:
    """Convierte tipos de Neo4j a algo que json pueda escribir.

    Los nodos temporales, los pares de puntos y los rangos no son JSON
    nativos. Si se dejan como estan, json.dump lanza el error al escribir y se
    pierde el archivo entero, asi que se aplanan a texto.
    """
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    if isinstance(valor, list):
        return [_serializable(v) for v in valor]
    if isinstance(valor, dict):
        return {str(k): _serializable(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_serializable(v) for v in valor]
    return str(valor)


def restaurar_nodos_cypher(
    sesion,
    ruta_nodos: str,
    ruta_aristas: str,
    lote: int = 1000,
) -> dict:
    """Recrea el grafo desde los dos JSON Lines.

    Primero se insertan los nodos con MERGE sobre la clave de negocio, que
    hace la operacion idempotente: correrla dos veces no duplica nodos. Luego
    se crean las relaciones emparejando por esa misma clave.

    Las etiquetas y los tipos de relacion NO se pueden pasar como parametros:
    Cypher exige que sean literales al construir el grafo, y la sintaxis
    dinamica (SET n:$(etiqueta)) no existe en la version abierta de Neo4j,
    igual que APOC. Por eso se interpolan en el texto de la consulta, y por
    eso antes se validan contra un patron estricto: sin ese filtro, una
    etiqueta que venga del archivo pasaria a ser codigo ejecutable.
    """
    nodos = leer_jsonl(ruta_nodos)
    aristas = leer_jsonl(ruta_aristas)

    inicio = time.perf_counter()
    nodos_sin_clave = 0
    for desplazamiento in range(0, len(nodos), lote):
        bloque = nodos[desplazamiento: desplazamiento + lote]
        filas = []
        for nodo in bloque:
            clave = nodo["clave"]
            if not clave:
                # Sin clave de negocio no se puede deduplicar: se descarta y
                # se cuenta, para que el resultado no finja ser exacto.
                nodos_sin_clave += 1
                continue
            etiquetas = [e for e in nodo["etiquetas"] if _patron_identificador(e)]
            # Se mete una etiqueta fija ademas de las reales, para poder
            # emparejar los extremos de las relaciones al restaurar.
            filas.append(
                {
                    "clave": clave,
                    "etiquetas": etiquetas,
                    "propiedades": nodo["propiedades"],
                }
            )
        if filas:
            sesion.run(
                "UNWIND $filas AS fila "
                "MERGE (n:Entidad {codigo: fila.clave}) "
                "SET n += fila.propiedades "
                "RETURN count(n) AS c",
                filas=filas,
            ).consume()
            # Las etiquetas van aparte porque su numero varia por nodo: se
            # agrupan y se aplica una consulta por cada conjunto distinto.
            for grupo in _agrupar_etiquetas(filas):
                consulta = (
                    "UNWIND $filas AS fila "
                    "MATCH (n:Entidad {codigo: fila.clave}) "
                    + "".join(f"SET n:{etiqueta} " for etiqueta in grupo["etiquetas"])
                    + "RETURN count(n) AS c"
                )
                sesion.run(consulta, filas=grupo["claves"]).consume()
    duracion_nodos = round((time.perf_counter() - inicio) * 1000.0, 3)

    inicio = time.perf_counter()
    # Las relaciones se agrupan por (tipo, conjunto de propiedades) para no
    # lanzar una consulta por cada arista del grafo.
    agrupadas: dict = {}
    for arista in aristas:
        tipo = arista["tipo"]
        if not _patron_identificador(tipo):
            continue
        clave_grupo = tipo
        agrupadas.setdefault(clave_grupo, []).append(arista)

    relaciones_insertadas = 0
    for tipo, grupo in agrupadas.items():
        for desplazamiento in range(0, len(grupo), lote):
            bloque = grupo[desplazamiento: desplazamiento + lote]
            filas = [
                {
                    "origen": a["origen"],
                    "destino": a["destino"],
                    "propiedades": a["propiedades"],
                }
                for a in bloque
                if a["origen"] and a["destino"]
            ]
            if not filas:
                continue
            consulta = (
                "UNWIND $filas AS fila "
                "MATCH (a:Entidad {codigo: fila.origen}) "
                "MATCH (b:Entidad {codigo: fila.destino}) "
                f"MERGE (a)-[r:{tipo}]->(b) "
                "SET r += fila.propiedades "
                "RETURN count(r) AS c"
            )
            registro = sesion.run(consulta, filas=filas).single()
            relaciones_insertadas += registro["c"] if registro else 0
    duracion_aristas = round((time.perf_counter() - inicio) * 1000.0, 3)

    return {
        "nodos_en_archivo": len(nodos),
        "nodos_sin_clave": nodos_sin_clave,
        "relaciones_en_archivo": len(aristas),
        "nodos_restaurados": len(nodos) - nodos_sin_clave,
        "relaciones_restauradas": relaciones_insertadas,
        "duracion_nodos_ms": duracion_nodos,
        "duracion_aristas_ms": duracion_aristas,
        "duracion_ms": duracion_nodos + duracion_aristas,
    }


# Etiquetas y tipos de relacion de Neo4j: letras, digitos y guion bajo,
# empezando por letra o guion bajo. Es la misma regla que acepta el motor.
PATRON_IDENTIFICADOR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _patron_identificador(texto: str) -> bool:
    """True si el texto es seguro para interpolar como etiqueta o tipo."""
    return bool(texto and PATRON_IDENTIFICADOR.match(texto))


def _agrupar_etiquetas(filas: list) -> list:
    """Agrupa las claves que comparten el mismo conjunto de etiquetas.

    Devuelve listas de dicts con la forma {"clave": ..., "etiquetas": [...]},
    para que cada grupo se pueda resolver con una sola consulta.
    """
    grupos: dict = {}
    for fila in filas:
        conjunto = tuple(sorted(fila["etiquetas"]))
        grupos.setdefault(conjunto, []).append(fila)
    return [
        {"claves": [f["clave"] for f in miembros], "etiquetas": list(conjunto)}
        for conjunto, miembros in grupos.items()
        if conjunto
    ]
