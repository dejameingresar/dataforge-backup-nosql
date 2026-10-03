#!/usr/bin/env python3
"""Adaptador de respaldo y restauracion para MongoDB.

Expone DOS estrategias distintas, que es lo que pide el enunciado para poder
compararlas:

  1. `logico_mongodump`  -> mongodump/mongorestore (BSON nativo, comprimido).
  2. `fisico_wiredtiger` -> copia en frio de los archivos .wt y el journal.

La diferencia real entre las dos no es el formato sino el momento: la primera
se puede correr con el servidor EN MARCHA y la segunda exige apagarlo. Ese
matiz es el que se documenta en el indice como limitacion.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

import pymongo
from pymongo.errors import PyMongoError

from .. import config
from ..utileria import (
    copiar_arbol,
    cronometro,
    escribir_json_atomico,
    escribir_manifiesto,
    listar_rutas,
    sha256_de_arbol,
    sha256_de_archivo,
    tamano_de_ruta,
)

NOMBRE_MONGO = "MongoDB"
VERSION_MONGO = "7.0.14"

BIN_HERRAMIENTAS = (
    "/home/prodriguez/opt/mongodb-tools/"
    "mongodb-database-tools-ubuntu2204-x86_64-100.10.0/bin"
)
RUTA_MONGODUMP = os.path.join(BIN_HERRAMIENTAS, "mongodump")
RUTA_MONGORESTORE = os.path.join(BIN_HERRAMIENTAS, "mongorestore")


# --- Descripcion de las estrategias -------------------------------------------

ESTRATEGIAS = {
    "logico_mongodump": {
        "nombre": "Respaldo logico con mongodump",
        "descripcion": (
            "Exporta la base completa a archivos BSON por coleccion usando "
            "mongodump, comprimidos con gzip. Se ejecuta con el servidor "
            "encendido porque habla el protocolo de red en vez de tocar "
            "archivos."
        ),
        "cuando_usarla": (
            "Respaldo diario o por noche sin parar el servicio, migraciones "
            "de version y transporte de datos entre servidores."
        ),
        "protege": (
            "Los documentos y los indices de las colecciones. Al ser un "
            "dump logico, ignora el formato fisico: el archivo se puede "
            "cargar en cualquier motor de la misma version mayor."
        ),
        "no_protege": (
            "Los usuarios, los roles y los permisos (van aparte, en "
            "authSchema), los datos locales de oplog, el estado interno de "
            "WiredTiger ni los archivos de configuracion."
        ),
        "requiere_parar": False,
    },
    "fisico_wiredtiger": {
        "nombre": "Copia en frio de WiredTiger",
        "descripcion": (
            "Con el motor DETENIDO se copia el dbpath entero: los archivos "
            ".wt de cada coleccion, los metadatos del motor (WiredTiger, "
            "WiredTiger.turtle, el catalogo y sizeStorer) y el directorio "
            "journal. Se empaqueta en un solo .tar.gz con rutas planas, que es "
            "como WiredTiger espera encontrarlos. NO es un snapshot en "
            "caliente."
        ),
        "cuando_usarla": (
            "Consolidaciones, migraciones de hardware, o cuando se quiere "
            "recuperar el almacenamiento completo tal cual estaba, incluidos "
            "sus metadatos internos y el journal."
        ),
        "protege": (
            "El estado completo del almacenamiento: datos, indices internos, "
            "punto de control del journal y catalogo de colecciones."
        ),
        "no_protege": (
            "No se puede elegir una base: el archivo es del MOTOR COMPLETO, "
            "con todas las bases del dbpath. Por eso no sustituye a "
            "mongodump cuando lo que se quiere es respaldar una base "
            "concreta. Solo sirve para la misma version y formato de "
            "WiredTiger y es opaco. El restore exige un dbpath vacio y el "
            "motor apagado, asi que la ventana sin servicio es doble."
        ),
        "requiere_parar": True,
    },
}


# --- Conexion ----------------------------------------------------------------


def conectar_mongodb(uri: str = config.MONGO_URI, servidor: str = "principal"):
    """Abre un cliente pymongo y comprueba que el motor responde."""
    cliente = pymongo.MongoClient(uri, serverSelectionTimeoutMS=config.TIMEOUT_CONEXION_MS)
    cliente.admin.command("ping")  # falla de inmediato si el motor no esta arriba
    return cliente


def version_mongodb(cliente) -> str:
    return cliente.server_info()["version"]


def hay_mongodump() -> bool:
    """Indica si la herramienta oficial esta instalada en esta maquina."""
    return os.path.isfile(RUTA_MONGODUMP) and os.access(RUTA_MONGODUMP, os.X_OK)


# --- Instrumentos del motor para la estrategia fisica ------------------------


def _mongod_corriendo(proceso_preexistente=None) -> bool:
    """True si hay algun mongod vivo."""
    resultado = subprocess.run(
        ["pgrep", "-f", "mongod --dbpath"], capture_output=True, text=True, check=False
    )
    return bool(resultado.stdout.strip())


def detener_mongod_satelite(proceso: subprocess.Popen) -> None:
    """Cierra el mongod satelite sin tocar el principal.

    Se cierra el descriptor del proceso y se espera: matar por nombre
    mataria tambien el mongod principal, que corre en el mismo binario.
    """
    if proceso is None:
        return
    try:
        proceso.terminate()
        proceso.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proceso.kill()
        proceso.wait(timeout=10)
    except Exception:
        pass


def detener_mongod(puerto: int = config.MONGO_PUERTO, timeout: float = 30.0) -> None:
    """Apaga el mongod de un puerto concreto y espera a que cierre los archivos.

    Se busca por PUERTO y no por nombre de proceso: hay dos mongod en esta
    maquina (el principal y el satelite del restore) y un pkill por nombre los
    mataria a los dos.
    """
    subprocess.run(["pkill", "-TERM", "-f", f"--port {puerto}"], check=False)
    limite = time.time() + timeout
    while time.time() < limite:
        if not _mongod_corriendo(puerto):
            return
        time.sleep(0.3)
    subprocess.run(["pkill", "-KILL", "-f", f"--port {puerto}"], check=False)
    time.sleep(1.0)


def _mongod_corriendo(puerto: int) -> bool:
    """True si el mongod de ese puerto sigue vivo."""
    resultado = subprocess.run(
        ["pgrep", "-f", f"--port {puerto}"], capture_output=True, text=True, check=False
    )
    return bool(resultado.stdout.strip())


def arrancar_mongod(dbpath: str, puerto: int, log_extra: str = "") -> subprocess.Popen:
    """Levanta un mongod y devuelve el proceso para poder apagarlo despues."""
    os.makedirs(dbpath, exist_ok=True)
    log_ruta = os.path.join(config.DIR_RESPALDOS, log_extra or "mongod.log")
    registro = open(log_ruta, "ab" if log_extra else "wb")
    proceso = subprocess.Popen(
        [
            config.MONGOD_BIN,
            "--dbpath",
            dbpath,
            "--port",
            str(puerto),
            "--bind_ip",
            "127.0.0.1",
            "--quiet",
        ],
        stdout=registro,
        stderr=subprocess.STDOUT,
    )
    return proceso


def esperar_mongo(uri: str, segundos: float = 60.0) -> bool:
    """Espera a que el mongod acepte conexiones."""
    limite = time.time() + segundos
    while time.time() < limite:
        try:
            cliente = pymongo.MongoClient(uri, serverSelectionTimeoutMS=800)
            cliente.admin.command("ping")
            return True
        except PyMongoError:
            time.sleep(0.3)
    return False


# --- Estrategia 1: respaldo logico con mongodump ------------------------------


def generar_respaldo_logico(
    destino: str,
    base: str = config.DB_MONGO_ORIGEN,
    uri: str = config.MONGO_URI,
    comprimir: bool = True,
) -> dict:
    """Ejecuta mongodump sobre `base` y deja el arbol de archivos en `destino`.

    Devuelve el detalle de lo generado, incluida la lista de archivos .bson
    que despues necesita el restore, porque hay que pasarle uno por uno.
    """
    os.makedirs(destino, exist_ok=True)
    comando = [
        RUTA_MONGODUMP,
        "--uri",
        uri,
        "--db",
        base,
        "--out",
        destino,
    ]
    if comprimir:
        comando.append("--gzip")

    inicio = time.perf_counter()
    resultado = subprocess.run(comando, capture_output=True, text=True, timeout=600)
    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)

    if resultado.returncode != 0:
        raise RuntimeError(
            f"mongodump fallo con codigo {resultado.returncode}: {resultado.stderr.strip()}"
        )

    archivos_bson = sorted(
        os.path.join(carpeta, nombre)
        for carpeta, _sub, archivos in os.walk(destino)
        for nombre in archivos
        if nombre.endswith(".bson.gz") or nombre.endswith(".bson")
    )
    if not archivos_bson:
        raise RuntimeError(
            f"mongodump termino bien pero no genero ningun .bson en {destino}. "
            "Lo mas probable es que la base este vacia o que el nombre sea incorrecto."
        )

    return {
        "estrategia": "logico_mongodump",
        "directorio": destino,
        "archivos_bson": archivos_bson,
        "archivos_metadata": sorted(
            os.path.join(carpeta, nombre)
            for carpeta, _sub, archivos in os.walk(destino)
            for nombre in archivos
            if nombre.endswith("metadata.json")
        ),
        "bytes": tamano_de_ruta(destino),
        "duracion_ms": duracion_ms,
        "salida_mongodump": resultado.stdout.strip(),
    }


def restaurar_logico(
    origen: str,
    destino_base: str,
    uri: str = config.MONGO_URI,
) -> dict:
    """Restaura el dump usando mongorestore archivo por archivo.

    IMPORTANTE: mongorestore no acepta un directorio. Si se le pasa uno,
    responde "don't know what to do with file", restaura 0 documentos y aun
    asi devuelve codigo de salida 0. Por eso aqui se recorren los .bson y se
    les pasa uno a uno, y ademas se comprueba el conteo restaurado leyendo
    el texto que el propio comando imprime.
    """
    if not os.path.isdir(origen):
        raise FileNotFoundError(f"No existe el directorio de respaldo: {origen}")

    archivos_bson = sorted(
        os.path.join(carpeta, nombre)
        for carpeta, _sub, archivos in os.walk(origen)
        for nombre in archivos
        if nombre.endswith(".bson.gz") or nombre.endswith(".bson")
    )
    if not archivos_bson:
        raise RuntimeError(f"No hay archivos .bson dentro de {origen}")

    inicio = time.perf_counter()
    restaurados = 0
    fallos = 0
    detalles = []
    for archivo in archivos_bson:
        comando = [
            RUTA_MONGORESTORE,
            "--uri",
            uri,
            "--drop",
            "--nsFrom",
            os.path.basename(origen),
            "--nsTo",
            destino_base,
            archivo,
        ]
        if archivo.endswith(".gz"):
            # mongorestore usa --gzip para leer, no lo deduce de la extension
            comando.insert(-1, "--gzip")
        resultado = subprocess.run(comando, capture_output=True, text=True, timeout=600)
        salida = resultado.stdout + resultado.stderr
        encontrados = re.findall(r"(\d+) document\(s\) restored successfully", salida)
        if encontrados:
            restaurados += int(encontrados[-1])
        fallidos = re.findall(r"(\d+) document\(s\) failed to restore", salida)
        if fallidos:
            fallos += int(fallidos[-1])
        detalles.append(
            {
                "archivo": os.path.basename(archivo),
                "codigo": resultado.returncode,
                "restaurados": int(encontrados[-1]) if encontrados else 0,
            }
        )
        if resultado.returncode != 0:
            raise RuntimeError(
                f"mongorestore fallo sobre {os.path.basename(archivo)}: {salida.strip()}"
            )

    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)
    if restaurados == 0:
        raise RuntimeError(
            "mongorestore termino sin errores pero no restauro ningun documento. "
            "Suele pasar cuando se le pasa un directorio en vez de un archivo."
        )
    return {
        "estrategia": "logico_mongodump",
        "base_destino": destino_base,
        "documentos_restaurados": restaurados,
        "documentos_fallidos": fallos,
        "duracion_ms": duracion_ms,
        "detalle": detalles,
    }


def listar_archivos_wiredtiger(dbpath: str) -> list:
    """Lista los archivos .wt de TODA la jerarquia del dbpath, con ruta relativa.

    MongoDB no tiene los .wt solo en la raiz: cada base de datos es un
    subdirectorio (data/ en este caso) y dentro estan sus colecciones. Por
    eso se recorre el arbol entero y se devuelven rutas RELATIVAS al dbpath,
    que es justo como hay que restaurarlas.
    """
    encontrados = []
    for carpeta, subcarpetas, archivos in os.walk(dbpath):
        # mongod.lock y los archivos de log no forman parte del estado.
        subcarpetas[:] = [s for s in subcarpetas if s != "journal"]
        for nombre in sorted(archivos):
            if not nombre.endswith(".wt"):
                continue
            if nombre.startswith(("collection-", "index-")) or nombre in (
                "WiredTiger.wt",
                "WiredTigerHS.wt",
                "_mdb_catalog.wt",
                "sizeStorer.wt",
            ):
                encontrados.append(os.path.relpath(os.path.join(carpeta, nombre), dbpath))
    return sorted(encontrados)


# Metadatos del motor que hacen falta para que arranque con el estado copiado.
ARCHIVOS_METADATOS_WT = (
    "WiredTiger",
    "WiredTiger.turtle",
    "WiredTigerHS.wt",
    "_mdb_catalog.wt",
    "sizeStorer.wt",
)

# Archivos que NUNCA se copian, aunque esten en el dbpath:
#   mongod.lock  -> pertenece al proceso anterior; si se restaura, el motor
#                   lo detecta como cerrojo de otra instancia.
#   mongod.log   -> es el registro, no son datos, y pesa mas que la base.
#   storage.bson -> MongoDB lo REESCRIBE al arrancar un dbpath nuevo. Si se
#                   copia y viene vacio (0 bytes, que es lo normal mientras el
#                   motor esta vivo), el satelite aborta con
#                   "Metadata file cannot be empty" y no hay forma de probarlo.
ARCHIVOS_EXCLUIDOS_WT = ("mongod.lock", "mongod.log", "storage.bson")


# --- Estrategia 2: snapshot fisico de WiredTiger -----------------------------


def generar_respaldo_fisico(
    dbpath: str,
    destino: str,
    puerto: int = config.MONGO_PUERTO,
    conservar_descomprimido: bool = False,
) -> dict:
    """Empaqueta el dbpath entero de WiredTiger en un .tar.gz con rutas planas.

    Se llama con el mongod APAGADO: es una COPIA EN FRIO, no un snapshot en
    caliente. Copiar los .wt con el motor vivo da un archivo inconsistente,
    porque el journal sigue creciendo mientras se copia y el checkpoint
    queda a medias. MongoDB trae un mecanismo propio de snapshot en caliente,
    pero este adaptador hace la copia en frio y por eso exige el motor parado.

    El archivo se empaqueta con rutas RELATIVAS A LA RAIZ del dbpath, sin
    envolverlo en un subdirectorio: si el .wt acaba en wiredtiger/dentro/, el
    mongod arranca sin errores y con la base vacia, porque busca los .wt en la
    raiz y no los encuentra.
    """
    if _mongod_corriendo(puerto):
        raise RuntimeError(
            "No se puede tomar un snapshot fisico con el mongod encendido. "
            "Detenlo primero: una copia en caliente del journal da un "
            "archivo inconsistente."
        )
    if not os.path.isdir(dbpath):
        raise FileNotFoundError(f"No existe el dbpath: {dbpath}")

    os.makedirs(destino, exist_ok=True)
    base = os.path.join(destino, "dbpath")
    if os.path.isdir(base):
        shutil.rmtree(base)
    os.makedirs(base, exist_ok=True)

    archivos_wt = listar_archivos_wiredtiger(dbpath)
    excluidos = []

    with cronometro():
        for relativo in archivos_wt:
            nombre = os.path.basename(relativo)
            if nombre in ARCHIVOS_EXCLUIDOS_WT:
                excluidos.append(relativo)
                continue
            origen_completo = os.path.join(dbpath, relativo)
            destino_completo = os.path.join(base, relativo)
            os.makedirs(os.path.dirname(destino_completo), exist_ok=True)
            shutil.copy2(origen_completo, destino_completo)

        # Los metadatos van aparte porque no terminan en .wt.
        for nombre in ARCHIVOS_METADATOS_WT:
            origen_completo = os.path.join(dbpath, nombre)
            if nombre in ARCHIVOS_EXCLUIDOS_WT:
                excluidos.append(nombre)
                continue
            if os.path.isfile(origen_completo) and os.path.getsize(origen_completo) > 0:
                shutil.copy2(origen_completo, os.path.join(base, nombre))

        # El journal va entero: sin el replay no hay estado consistente.
        origen_journal = os.path.join(dbpath, "journal")
        if os.path.isdir(origen_journal):
            copiar_arbol(origen_journal, os.path.join(base, "journal"))

    escribir_manifiesto(os.path.join(base, "manifiesto_interno.json"), base)
    ruta_manifiesto = escribir_manifiesto(
        os.path.join(destino, "manifiesto.json"), base
    )

    inicio = time.perf_counter()
    archivo_final = os.path.join(destino, "snapshot_wiredtiger.tar.gz")
    # El "." final incluye todo lo que hay en base/ manteniendo las rutas
    # relativas, que es justo lo que mongod necesita para restaurarlo.
    subprocess.run(
        ["tar", "-czf", archivo_final, "-C", base, "."],
        capture_output=True,
        text=True,
        timeout=1800,
        check=True,
    )
    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)

    # El arbol descomprimido pesa cientos de megas y no sirve para nada una
    # vez empaquetado: si se deja dentro del proyecto, el repositorio crece
    # con el tamano del dbpath entero. Se borra y solo queda el .tar.gz.
    if not conservar_descomprimido:
        shutil.rmtree(base, ignore_errors=True)

    return {
        "estrategia": "fisico_wiredtiger",
        "archivo": archivo_final,
        "directorio": base,
        "manifiesto": ruta_manifiesto,
        "archivos_wt": archivos_wt,
        "total_archivos_wt": len(archivos_wt),
        "archivos_excluidos": excluidos,
        "bytes": os.path.getsize(archivo_final),
        "duracion_ms": duracion_ms,
        "copia_en_frio": True,
        "ambito": "motor completo, no una base aislada",
    }


def restaurar_fisico(archivo_respaldo: str, dbpath_destino: str) -> dict:
    """Desempaqueta el snapshot en un dbpath NUEVO y lo deja listo para arrancar.

    `archivo_respaldo` tiene que ser el .tar.gz, NO el directorio donde se
    genero: tar se queja de que es un directorio y el error aparece lejos de
    la causa. Por eso se valida antes de invocar a tar.

    El destino tiene que estar VACIO o con un WiredTiger identico. Si se
    extrae encima de un dbpath con otro estado, mongod se niega a arrancar
    porque el catalogo no cuadra.

    La extraccion es plana (tar -C dbpath): los .wt tienen que quedar en la
    raiz y en sus subdirectorios de base, porque ahi los busca el motor. Si
    acabaran dentro de un nivel extra, mongod arrancaria sano y sin datos.
    """
    if os.path.isdir(archivo_respaldo):
        raise RuntimeError(
            f"restaurar_fisico espera el archivo .tar.gz, pero {archivo_respaldo} "
            "es un directorio. Pasa el valor de la clave 'archivo' del resultado "
            "del respaldo, no la de 'directorio'."
        )
    if not os.path.isfile(archivo_respaldo):
        raise FileNotFoundError(f"No existe el archivo de respaldo: {archivo_respaldo}")

    if os.path.isdir(dbpath_destino) and os.listdir(dbpath_destino):
        raise RuntimeError(
            f"El destino {dbpath_destino} no esta vacio. mongod exige un "
            "dbpath limpio para arrancar con el estado extraido."
        )
    os.makedirs(dbpath_destino, exist_ok=True)

    inicio = time.perf_counter()
    resultado = subprocess.run(
        ["tar", "-xzf", archivo_respaldo, "-C", dbpath_destino],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"No se pudo descomprimir el snapshot: {resultado.stderr}")

    # El lock copiado apunta al proceso anterior: hay que borrarlo.
    lock = os.path.join(dbpath_destino, "WiredTiger.lock")
    if os.path.isfile(lock):
        os.remove(lock)

    # Los .wt tienen que estar en la raiz del dbpath. Si el archivo se
    # desempaqueto con un nivel extra, se sube de nivel antes de arrancar.
    anidado = os.path.join(dbpath_destino, "dbpath")
    if os.path.isdir(anidado):
        for nombre in os.listdir(anidado):
            origen = os.path.join(anidado, nombre)
            destino = os.path.join(dbpath_destino, nombre)
            if os.path.isdir(origen):
                if os.path.exists(destino):
                    copiar_arbol(origen, destino)
                else:
                    shutil.move(origen, destino)
            else:
                shutil.move(origen, destino)
        shutil.rmtree(anidado, ignore_errors=True)

    # El manifiesto interno viaja con el snapshot, pero no es parte del estado:
    # si se queda en el dbpath, el motor lo ignora, asi que se aparta.
    interno = os.path.join(dbpath_destino, "manifiesto_interno.json")
    if os.path.isfile(interno):
        shutil.move(interno, os.path.join(dbpath_destino, "..", "manifiesto_restore.json"))

    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)

    archivos_wt = [n for n in os.listdir(dbpath_destino) if n.endswith(".wt")]
    return {
        "estrategia": "fisico_wiredtiger",
        "dbpath": dbpath_destino,
        "archivos_wt_en_raiz": len(archivos_wt),
        "total_archivos": sum(
            len(archivos) for _c, _s, archivos in os.walk(dbpath_destino)
        ),
        "duracion_ms": duracion_ms,
    }
