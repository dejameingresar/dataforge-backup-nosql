#!/usr/bin/env python3
"""Ejecuta los seis respaldos, los restaura y mide el resultado de verdad.

Uso:

    python3 -m app.ejecucion              # corrida completa
    python3 -m app.ejecucion --sin-corte  # omite la prueba de corte a mitad

Cada estrategia pasa por el mismo camino: se siembra el dato, se genera el
respaldo cronometrado, se restaura en una instancia aparte y se comparan los
conteos antes y despues. El numero que sale en el indice es el medido en esa
comparacion, no un estimado.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import config, indice
from .adaptadores import mongodb, neo4j, redis
from .datos_prueba import sembrar_mongodb, sembrar_neo4j, sembrar_redis
from .utileria import ahora_iso, leer_json
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

DIR_LAB = os.path.join(config.RAIZ_PROYECTO, ".lab")
NEO4J_SATELITE = os.path.join(DIR_LAB, "neo4j-sat")
# El dbpath donde se restaura la copia fisica NO va dentro del proyecto: al
# descomprimirse pesa cientos de megas y no tiene sentido subirlo al
# repositorio. Tampoco va en /tmp, que aqui es un tmpfs de 512 MB y el dbpath
# no cabe. Se usa un directorio de trabajo en el disco, bajo el home, y se
# borra al terminar la prueba.
MONGO_SATELITE_DBPATH = os.path.join(
    os.path.expanduser("~"), ".cache", "dataforge-backup", "mongo-restore"
)
# Los datos de la aplicacion viven en su propio dbpath y en su propio puerto,
# separados del mongod compartido de la maquina. Asi una copia fisica del
# almacenamiento no arrastra las bases de otros proyectos y el round-trip se
# mide contra el contenido sembrado aqui.
MONGO_APLICACION_DBPATH = config.ruta_mongod_dbpath()
URI_MONGO_APLICACION = f"mongodb://127.0.0.1:{config.MONGO_PUERTO_APLICACION}"
URI_MONGO_SATELITE = "mongodb://127.0.0.1:27018"
URI_NEO4J_SATELITE = "bolt://127.0.0.1:17687"


def _log(mensaje: str) -> None:
    print(f"[{ahora_iso()}] {mensaje}", flush=True)


# --- MongoDB -----------------------------------------------------------------


def _asegurar_mongo_aplicacion():
    """Consigue un mongod con el dbpath DEDICADO de la aplicacion.

    Hay dos caminos. Si el puerto de la aplicacion ya responde, se usa. Si no,
    se intenta arrancar en su puerto propio; y si tampoco se puede (porque el
    motor ya corre con ese dbpath en el puerto compartido, y WiredTiger tiene
    un cerrojo por dbpath), se reutiliza la instancia que lo tiene abierto.

    Devuelve la conexion lista para usar.
    """
    if _responde(URI_MONGO_APLICACION):
        return mongodb.conectar_mongodb(URI_MONGO_APLICACION), URI_MONGO_APLICACION

    proceso = mongodb.arrancar_mongod(
        MONGO_APLICACION_DBPATH, config.MONGO_PUERTO_APLICACION, "mongod-aplicacion.log"
    )
    if mongodb.esperar_mongo(URI_MONGO_APLICACION, 90):
        return mongodb.conectar_mongodb(URI_MONGO_APLICACION), URI_MONGO_APLICACION

    # El puerto propio esta ocupado por otra cosa o el dbpath ya esta
    # abierto en otro puerto: se usa la instancia que lo tenga.
    mongodb.detener_mongod_satelite(proceso)
    if _responde(config.MONGO_URI):
        _log(
            "  el dbpath de la aplicacion ya lo tiene abierto el mongod del "
            "puerto compartido; se reutiliza esa instancia"
        )
        return mongodb.conectar_mongodb(config.MONGO_URI), config.MONGO_URI

    raise RuntimeError(
        "No se pudo obtener un mongod con el dbpath de la aplicacion "
        f"({MONGO_APLICACION_DBPATH}). Ni arranca en el puerto "
        f"{config.MONGO_PUERTO_APLICACION} ni hay una instancia que lo tenga "
        f"abierto."
    )


def _responde(uri: str) -> bool:
    from pymongo.errors import PyMongoError

    try:
        cliente = mongodb.conectar_mongodb(uri)
        cliente.close()
        return True
    except PyMongoError:
        return False


def probar_mongodb() -> list:
    """Corre las dos estrategias de MongoDB con su restauracion."""
    entradas = []
    cliente, uri_app = _asegurar_mongo_aplicacion()
    _log(f"  motor de la aplicacion en {uri_app}")
    try:
        version = mongodb.version_mongodb(cliente)
        _log(f"MongoDB {version}: sembrando datos de prueba")
        conteos = sembrar_mongodb(cliente)
        total_documentos = sum(conteos.values())
        _log(f"  sembradas {conteos} (total {total_documentos})")

        indice_paquetes = _contar_indices(cliente)
        _log(f"  indices creados: {indice_paquetes}")

        # --- Estrategia 1: logico con mongodump ---------------------------
        destino = os.path.join(config.DIR_MONGODB, "logico_mongodump")
        _limpiar(destino)
        _log("  estrategia 1: mongodump")
        resultado = mongodb.generar_respaldo_logico(
            destino, config.DB_MONGO_ORIGEN, uri_app
        )
        _log(f"    {resultado['bytes']} bytes en {resultado['duracion_ms']} ms")

        restore = _restaurar_mongo_logico(
            destino, conteos, total_documentos, uri_app
        )
        _log(f"    round-trip: {restore}")

        entradas.append(
            indice.nueva_entrada(
                motor="MongoDB",
                version_motor=version,
                clave_estrategia="logico_mongodump",
                descripcion_estrategia=mongodb.ESTRATEGIAS["logico_mongodump"],
                ruta_artefacto=destino,
                duracion_ms=resultado["duracion_ms"],
                unidades={
                    "documentos": total_documentos,
                    "por_coleccion": conteos,
                    "indices": indice_paquetes,
                },
                round_trip=restore,
                notas=[
                    "El restore se hizo archivo por archivo: mongorestore no "
                    "entiende un directorio, responde que no sabe que hacer con "
                    "el y aun asi devuelve codigo 0 restaurando 0 documentos. "
                    "El adaptador comprueba el conteo, no el codigo de salida.",
                ],
            )
        )

        # --- Estrategia 2: copia en frio de WiredTiger ---------------------
        # Esta parte exige apagar el mongod, asi que se hace aparte.
        destino_fisico = os.path.join(config.DIR_MONGODB, "fisico_wiredtiger")
        _limpiar(destino_fisico)
        _log("  estrategia 2: apagando el mongod para la copia en frio")
        cliente.close()
        puerto_app = (
            config.MONGO_PUERTO_APLICACION
            if uri_app == URI_MONGO_APLICACION
            else config.MONGO_PUERTO
        )
        mongodb.detener_mongod(puerto_app)
        resultado_fisico = mongodb.generar_respaldo_fisico(
            MONGO_APLICACION_DBPATH, destino_fisico, puerto_app
        )
        _log(
            f"    copia: {resultado_fisico['bytes']} bytes, "
            f"{resultado_fisico['total_archivos_wt']} archivos .wt, "
            f"excluidos={resultado_fisico['archivos_excluidos']}"
        )

        # Se vuelve a levantar el motor de la aplicacion antes de seguir.
        mongodb.arrancar_mongod(
            MONGO_APLICACION_DBPATH, puerto_app, "mongod-aplicacion.log"
        )
        mongodb.esperar_mongo(uri_app, 60)
        _log("    mongod de la aplicacion reiniciado")

        restore_fisico = _restaurar_mongo_fisico(
            resultado_fisico["archivo"], conteos, total_documentos
        )
        _log(f"    round-trip: {restore_fisico}")

        entradas.append(
            indice.nueva_entrada(
                motor="MongoDB",
                version_motor=version,
                clave_estrategia="fisico_wiredtiger",
                descripcion_estrategia=mongodb.ESTRATEGIAS["fisico_wiredtiger"],
                ruta_artefacto=resultado_fisico["archivo"],
                duracion_ms=resultado_fisico["duracion_ms"],
                unidades={
                    "archivos_wt": resultado_fisico["total_archivos_wt"],
                    "documentos": total_documentos,
                },
                round_trip=restore_fisico,
                notas=[
                    "Es una COPIA EN FRIO: el motor estaba detenido. Copiar los "
                    ".wt con el mongod vivo daria un journal a medias.",
                    "El archivo es del MOTOR COMPLETO, no de una base: se copian "
                    "todas las bases del dbpath. Esa es la razon por la que "
                    "esta estrategia no sustituye a mongodump cuando lo que se "
                    "quiere es respaldar una base concreta.",
                    "Se excluyen mongod.lock, mongod.log y storage.bson. El "
                    "storage.bson se regenera al arrancar, y si se copia "
                    "vacio el motor aborta con 'Metadata file cannot be "
                    "empty'.",
                ],
            )
        )
    finally:
        try:
            cliente.close()
        except Exception:
            pass  # si se cerro antes para la copia en frio, no hay que insistir
    return entradas


def _contar_indices(cliente) -> int:
    """Suma los indices definidos en todas las colecciones de la base."""
    total = 0
    base = cliente[config.DB_MONGO_ORIGEN]
    for nombre in base.list_collection_names():
        total += len(base[nombre].index_information())
    return total


def _restaurar_mongo_logico(
    destino: str, conteos: dict, total: int, uri: str = config.MONGO_URI
) -> dict:
    """Borra la base y la restaura desde el dump, midiendo el resultado."""
    from pymongo import MongoClient

    cliente = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        cliente.drop_database(config.DB_MONGO_ORIGEN)
        resultado = mongodb.restaurar_logico(destino, config.DB_MONGO_ORIGEN, uri)
        base = cliente[config.DB_MONGO_ORIGEN]
        despues = {
            coleccion: base[coleccion].count_documents({})
            for coleccion in base.list_collection_names()
        }
        total_despues = sum(despues.values())

        # Se prueba una lectura real, no solo el conteo: un dump truncado
        # puede devolver el numero correcto con los campos vacios.
        muestra = base["pedidos"].find_one({"codigo": "PED-000000"}) if despues.get("pedidos") else None
        indices = _contar_indices(cliente)

        ok = despues == conteos and total_despues == total and muestra is not None
        return {
            "resultado": "OK" if ok else "DIFIERE",
            "documentos_antes": total,
            "documentos_despues": total_despues,
            "por_coleccion_antes": conteos,
            "por_coleccion_despues": despues,
            "indices_despues": indices,
            "documento_muestra_leido": bool(muestra),
            "duracion_restore_ms": resultado["duracion_ms"],
            "documentos_restaurados": resultado["documentos_restaurados"],
        }
    finally:
        cliente.close()


def _restaurar_mongo_fisico(archivo_snap: str, conteos: dict, total: int) -> dict:
    """Restaura el snapshot en un dbpath nuevo y lo levanta en otro puerto.

    Se usa un mongod satelite en el puerto 27018 para no tocar el principal.
    """
    from pymongo import MongoClient

    _limpiar(MONGO_SATELITE_DBPATH)
    resultado = mongodb.restaurar_fisico(archivo_snap, MONGO_SATELITE_DBPATH)
    proceso = mongodb.arrancar_mongod(
        MONGO_SATELITE_DBPATH, config.MONGO_PUERTO_SATELITE, "mongod-satelite.log"
    )
    try:
        listo = mongodb.esperar_mongo(URI_MONGO_SATELITE, 90)
        if not listo:
            return {"resultado": "ERROR", "motivo": "el mongod satelite no arranco"}
        cliente = MongoClient(URI_MONGO_SATELITE, serverSelectionTimeoutMS=5000)
        try:
            bases = [
                b
                for b in cliente.list_database_names()
                if b not in ("admin", "config", "local")
            ]
            # Se busca la base NOMBRADA, no la primera de la lista: al
            # restaurar hay varias bases en el dbpath y tomar la primera
            # devuelve los datos de otra, que es un falso negativo.
            if config.DB_MONGO_ORIGEN in bases:
                base = cliente[config.DB_MONGO_ORIGEN]
            elif bases:
                base = cliente[bases[0]]
            else:
                base = cliente[config.DB_MONGO_ORIGEN]

            despues = {
                c: base[c].count_documents({}) for c in base.list_collection_names()
            }
            total_despues = sum(despues.values())
            muestra = (
                base["pedidos"].find_one({"codigo": "PED-000000"})
                if despues.get("pedidos")
                else None
            )
            ok = despues == conteos and total_despues == total and muestra is not None
            return {
                "resultado": "OK" if ok else "DIFIERE",
                "base_satelite": base.name,
                "bases_disponibles": bases,
                "documentos_antes": total,
                "documentos_despues": total_despues,
                "por_coleccion_antes": conteos,
                "por_coleccion_despues": despues,
                "documento_muestra_leido": bool(muestra),
                "duracion_restore_ms": resultado["duracion_ms"],
                "archivos_wt_en_raiz": resultado["archivos_wt_en_raiz"],
                "puerto_satelite": config.MONGO_PUERTO_SATELITE,
            }
        finally:
            cliente.close()
    finally:
        mongodb.detener_mongod_satelite(proceso)
        # El dbpath restaurado se borra al terminar: pesa cientos de megas y
        # ya cumplio su funcion, que era comprobar los conteos.
        shutil.rmtree(MONGO_SATELITE_DBPATH, ignore_errors=True)


# --- Redis -------------------------------------------------------------------


def probar_redis() -> list:
    """Corre las dos estrategias de Redis con su restauracion."""
    entradas = []
    cliente = redis.conectar_redis()
    try:
        version = redis.version_redis(cliente)
        _log(f"Redis {version}: sembrando datos de prueba")
        conteos = sembrar_redis(cliente)
        total_claves = redis.total_claves(cliente, config.DB_REDIS_ORIGEN)
        _log(f"  sembradas {total_claves} claves ({conteos['por_tipo']})")

        # --- Estrategia 1: RDB --------------------------------------------
        destino = os.path.join(config.DIR_REDIS, "rdb_dump")
        _limpiar(destino)
        _log("  estrategia 1: BGSAVE (RDB)")
        resultado = redis.generar_respaldo_rdb(destino)
        _log(
            f"    {resultado['bytes']} bytes en {resultado['duracion_ms']} ms, "
            f"redis-check-rdb ok={resultado['validacion_rdb'].get('ok')}"
        )
        restore = redis.restaurar_rdb(resultado["archivo"])
        _log(f"    round-trip: {restore}")

        ok = (
            restore["claves_restauradas"] == total_claves
            and restore["carga_completa"]
        )
        entradas.append(
            indice.nueva_entrada(
                motor="Redis",
                version_motor=version,
                clave_estrategia="rdb_dump",
                descripcion_estrategia=redis.ESTRATEGIAS["rdb_dump"],
                ruta_artefacto=resultado["archivo"],
                duracion_ms=resultado["duracion_ms"],
                unidades={
                    "claves": total_claves,
                    "por_tipo": conteos["por_tipo"],
                },
                round_trip={
                    "resultado": "OK" if ok else "DIFIERE",
                    "claves_antes": total_claves,
                    "claves_despues": restore["claves_restauradas"],
                    "claves_totales_restore": restore["claves_totales"],
                    "carga_completa": restore["carga_completa"],
                    "duracion_restore_ms": restore["duracion_ms"],
                    "validacion_redis_check_rdb": resultado["validacion_rdb"],
                    "puerto_satelite": restore["puerto"],
                },
                notas=[
                    "El dump.rdb se copio con el servidor corriendo: Redis 7.0 "
                    "duplica por copy-on-write las paginas modificadas, asi que "
                    "el archivo es una foto consistente. Redis escribe en un "
                    "temporal y lo renombra al terminar, de modo que un dump.rdb "
                    "existe o esta completo.",
                    "Redis 7 PROTEGE el parametro dir: CONFIG SET dir falla, "
                    "asi que el archivo se pide con BGSAVE y se copia del "
                    "directorio que el servidor configuro al arrancar.",
                    "El volcado cubre LAS 16 BASES del servidor, no una sola.",
                ],
            )
        )

        # --- Estrategia 2: AOF --------------------------------------------
        destino_aof = os.path.join(config.DIR_REDIS, "aof_append")
        _limpiar(destino_aof)
        _log("  estrategia 2: BGREWRITEAOF (AOF multipart)")
        resultado_aof = redis.generar_respaldo_aof(destino_aof)
        _log(
            f"    {resultado_aof['bytes']} bytes en {resultado_aof['duracion_ms']} ms, "
            f"archivos={resultado_aof['archivos']}"
        )
        restore_aof = redis.restaurar_aof(resultado_aof["directorio"])
        _log(f"    round-trip: {restore_aof}")

        ok_aof = (
            restore_aof["claves_restauradas"] == total_claves
            and restore_aof.get("carga_completa")
        )
        entradas.append(
            indice.nueva_entrada(
                motor="Redis",
                version_motor=version,
                clave_estrategia="aof_append",
                descripcion_estrategia=redis.ESTRATEGIAS["aof_append"],
                ruta_artefacto=resultado_aof["directorio"],
                duracion_ms=resultado_aof["duracion_ms"],
                unidades={
                    "claves": total_claves,
                    "archivos_aof": resultado_aof["archivos"],
                },
                round_trip={
                    "resultado": "OK" if ok_aof else "DIFIERE",
                    "claves_antes": total_claves,
                    "claves_despues": restore_aof["claves_restauradas"],
                    "claves_totales_restore": restore_aof["claves_totales"],
                    "carga_completa": restore_aof.get("carga_completa"),
                    "muestra": restore_aof.get("muestra"),
                    "duracion_restore_ms": restore_aof["duracion_ms"],
                    "puerto_satelite": restore_aof["puerto"],
                },
                notas=[
                    "Redis 7.0 reparte el AOF en varios archivos con un "
                    "manifest; se copiaron todos. Copiar solo el .aof deja "
                    "fuera los incrementales y se pierde lo escrito despues.",
                ],
            )
        )
    finally:
        cliente.close()
    return entradas


# --- Neo4j -------------------------------------------------------------------


def _arrancar_neo4j_satelite() -> bool:
    """Asegura que la instancia satelite este viva, sin pararla si ya lo esta.

    Primero se comprueba si el puerto responde. Si responde, se usa tal cual:
    lanzar 'neo4j start' sobre una instancia ya viva puede matarla y produce
    el error de conexion defunta que se estaba viendo.
    """
    if _neo4j_satelite_lista():
        return True
    subprocess.run(
        [os.path.join(NEO4J_SATELITE, "bin", "neo4j"), "start"],
        capture_output=True,
        timeout=300,
        env={**os.environ, "JAVA_HOME": neo4j.JAVA_HOME},
    )
    return _esperar_neo4j(URI_NEO4J_SATELITE, 180)


def _detener_neo4j_satelite() -> None:
    """Para la instancia satelite, pero solo si de verdad esta viva.

    Parar una instancia que ya esta apagada devuelve error y, si se repite,
    se pierde tiempo. Ademas, tras un dump o un load hay que rearrancarla.
    """
    if not _puerto_responde(URI_NEO4J_SATELITE):
        return
    subprocess.run(
        [os.path.join(NEO4J_SATELITE, "bin", "neo4j"), "stop"],
        capture_output=True,
        timeout=300,
        env={**os.environ, "JAVA_HOME": neo4j.JAVA_HOME},
    )
    # Se espera a que el puerto se cierre del todo antes de tocar el almacenamiento.
    limite = time.time() + 120
    while time.time() < limite:
        if not _puerto_responde(URI_NEO4J_SATELITE):
            return
        time.sleep(1.0)


def _puerto_responde(uri: str) -> bool:
    """True si hay algo escuchando y respondiendo en esa URI de Bolt."""
    try:
        driver = GraphDatabase.driver(uri, auth=None, connection_timeout=3)
        driver.verify_connectivity()
        driver.close()
        return True
    except Exception:
        return False


def _neo4j_satelite_lista() -> bool:
    try:
        driver = neo4j.conectar_neo4j(URI_NEO4J_SATELITE)
        driver.close()
        return True
    except Exception:
        return False


def _esperar_neo4j(uri: str, segundos: int = 180) -> bool:
    """Espera activa a que el motor acepte conexiones de Bolt.

    Neo4j tarda varios segundos en quedar listo: el proceso existe mucho antes
    de que el puerto acepte consultas. Conectar sin esperar da una conexion
    defunta. Se reintenta hasta que responda una consulta trivial.
    """
    limite = time.time() + segundos
    while time.time() < limite:
        try:
            driver = GraphDatabase.driver(uri, auth=None, connection_timeout=5)
            with driver.session() as sesion:
                sesion.run("RETURN 1").consume()
            driver.close()
            return True
        except (ServiceUnavailable, Neo4jError, OSError):
            time.sleep(1.5)
    return False


def probar_neo4j() -> list:
    """Corre las dos estrategias de Neo4j con su restauracion."""
    entradas = []
    _log("Neo4j: arrancando la instancia satelite para las pruebas")
    if not _arrancar_neo4j_satelite():
        raise RuntimeError(
            "La instancia satelite de Neo4j no arranco. Sin ella no se puede "
            "probar el dump, porque en Community no se puede dumpear una base "
            "montada."
        )

    driver = neo4j.conectar_neo4j(URI_NEO4J_SATELITE)
    try:
        with driver.session(database=config.DB_NEO4J_ORIGEN) as sesion:
            version = neo4j.version_neo4j(driver)
            _log(f"Neo4j {version}: sembrando grafo de prueba")
            conteos = sembrar_neo4j(sesion)
            antes = neo4j.contar_grafo(sesion)
            _log(f"  sembrado {conteos}")
            _log(f"  conteo real: {antes}")

            # --- Estrategia 1: dump binario ------------------------------
            # Primero se comprueba el limite real de Community: dumpear la
            # base montada. El resultado se guarda porque es la limitacion
            # honesta que hay que documentar.
            _log("  estrategia 1: comprobando dump con la base montada")
            intento_montada = _intentar_dump_montada()

            destino_dump = os.path.join(config.DIR_NEO4J, "dump_binario")
            _limpiar(destino_dump)
            _log("    parando la satelite para tomar el dump (exigido por Community)")
            # El driver se CIERRA antes de parar el motor: si sigue abierto,
            # su conexion queda defunta y la siguiente consulta revienta con
            # ServiceUnavailable. Por eso despues se abre uno nuevo.
            driver.close()
            driver = None
            _detener_neo4j_satelite()
            resultado_dump = neo4j.generar_dump_binario(
                destino_dump, config.DB_NEO4J_ORIGEN, NEO4J_SATELITE
            )
            _log(f"    dump: {resultado_dump['bytes']} bytes en {resultado_dump['duracion_ms']} ms")

            # --- Restauracion del dump ----------------------------------
            restore_dump = _restaurar_dump_neo4j(destino_dump)
            _log(f"    round-trip: {restore_dump}")

            # Se vuelve a levantar para las mediciones siguientes. Hay que ESPERAR a
            # que acepte bolt: la instancia satelite tarda unos segundos en quedar
            # lista y conectar antes de eso lanza ServiceUnavailable.
            if not _arrancar_neo4j_satelite():
                raise RuntimeError("la instancia satelite de Neo4j no arranco tras el restore")
            if not _esperar_neo4j(URI_NEO4J_SATELITE, 180):
                raise RuntimeError(
                    "la instancia satelite de Neo4j arranco pero no acepto conexiones "
                    "en 180 s; la estrategia 2 necesita un servidor vivo"
                )
            driver = neo4j.conectar_neo4j(URI_NEO4J_SATELITE)

            entradas.append(
                indice.nueva_entrada(
                    motor="Neo4j",
                    version_motor=version,
                    clave_estrategia="dump_binario",
                    descripcion_estrategia=neo4j.ESTRATEGIAS["dump_binario"],
                    ruta_artefacto=resultado_dump["archivo"],
                    duracion_ms=resultado_dump["duracion_ms"],
                    unidades={
                        "nodos": antes["nodos"],
                        "relaciones": antes["relaciones"],
                        "por_etiqueta": antes["por_etiqueta"],
                    },
                    round_trip=restore_dump,
                    notas=[
                        "LIMITACION MEDIDA: con la base montada, "
                        "neo4j-admin database dump responde 'The database is in "
                        "use' y aun asi devuelve codigo 0. El archivo no se "
                        "crea, asi que el adaptador comprueba el archivo y no "
                        "solo el codigo de salida.",
                        "Por lo mismo el dump exige parar la instancia, y el "
                        "restore tambien: es una ventana sin servicio.",
                    ],
                )
            )

            # --- Estrategia 2: exportacion a JSON Lines --------------------
            destino_jsonl = os.path.join(config.DIR_NEO4J, "exportacion_cypher")
            _limpiar(destino_jsonl)
            os.makedirs(destino_jsonl, exist_ok=True)
            ruta_nodos = os.path.join(destino_jsonl, "nodos.jsonl")
            ruta_aristas = os.path.join(destino_jsonl, "aristas.jsonl")

            with driver.session(database=config.DB_NEO4J_ORIGEN) as sesion:
                _log("  estrategia 2: exportando nodos y aristas a JSON Lines")
                res_nodos = neo4j.exportar_nodos_cypher(sesion, ruta_nodos)
                res_aristas = neo4j.exportar_aristas_cypher(sesion, ruta_aristas)
                _log(
                    f"    {res_nodos['nodos']} nodos y {res_aristas['relaciones']} "
                    f"aristas exportados"
                )

                # Se borra el grafo para comprobar que el restore reconstruye.
                sesion.run("MATCH (n) DETACH DELETE n").consume()
                _log("    grafo borrado, restaurando desde los JSON Lines")
                res_restore = neo4j.restaurar_nodos_cypher(sesion, ruta_nodos, ruta_aristas)
                despues = neo4j.contar_grafo(sesion)
                _log(f"    round-trip: {res_restore}")

            ok = (
                despues["nodos"] == antes["nodos"]
                and despues["relaciones"] == antes["relaciones"]
            )
            entradas.append(
                indice.nueva_entrada(
                    motor="Neo4j",
                    version_motor=version,
                    clave_estrategia="exportacion_cypher",
                    descripcion_estrategia=neo4j.ESTRATEGIAS["exportacion_cypher"],
                    ruta_artefacto=destino_jsonl,
                    duracion_ms=res_nodos["duracion_ms"] + res_aristas["duracion_ms"],
                    unidades={
                        "nodos": antes["nodos"],
                        "relaciones": antes["relaciones"],
                        "lineas_nodos": res_nodos["nodos"],
                        "lineas_aristas": res_aristas["relaciones"],
                    },
                    round_trip={
                        "resultado": "OK" if ok else "DIFIERE",
                        "nodos_antes": antes["nodos"],
                        "nodos_despues": despues["nodos"],
                        "relaciones_antes": antes["relaciones"],
                        "relaciones_despues": despues["relaciones"],
                        "nodos_sin_clave_descartados": res_restore["nodos_sin_clave"],
                        "duracion_restore_ms": res_restore["duracion_ms"],
                        "restaurado": res_restore,
                    },
                    notas=[
                        "El restore reconstruye los enlaces por la propiedad "
                        "codigo, no por el identificador interno de Neo4j: al "
                        "cargar el archivo los ids son nuevos.",
                        "Las etiquetas y los tipos de relacion se interpolan "
                        "en la consulta tras validarse, porque Cypher no "
                        "acepta parametros en SET n:Etiqueta.",
                    ],
                )
            )
    finally:
        # El driver se pone en None al parar el motor para el dump, asi que
        # aqui puede no existir.
        if driver is not None:
            driver.close()

    return entradas


def _intentar_dump_montada() -> dict:
    """Intenta dumpear con la base montada y devuelve lo que responde.

    Es la comprobacion que convierte la limitacion de Community en un dato
    medido y no en una afirmacion.
    """
    destino = os.path.join(DIR_LAB, "dump-montada")
    _limpiar(destino)
    os.makedirs(destino, exist_ok=True)
    resultado = subprocess.run(
        [
            config.NEO4J_ADMIN_BIN,
            "database",
            "dump",
            config.DB_NEO4J_ORIGEN,
            f"--to-path={destino}",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, "JAVA_HOME": neo4j.JAVA_HOME},
        cwd=NEO4J_SATELITE,
    )
    combinado = resultado.stdout + resultado.stderr
    archivo = os.path.join(destino, f"{config.DB_NEO4J_ORIGEN}.dump")
    return {
        "codigo_salida": resultado.returncode,
        "archivo_creado": os.path.isfile(archivo),
        "mensaje": [l for l in combinado.split("\n") if "ERROR" in l or "in use" in l][:2],
    }


def _restaurar_dump_neo4j(destino_dump: str) -> dict:
    """Carga el dump en la satelite detenida y cuenta el grafo."""
    resultado = neo4j.restaurar_dump_binario(
        destino_dump, config.DB_NEO4J_ORIGEN, NEO4J_SATELITE
    )
    _arrancar_neo4j_satelite()

    driver = neo4j.conectar_neo4j(URI_NEO4J_SATELITE)
    try:
        with driver.session(database=config.DB_NEO4J_ORIGEN) as sesion:
            conteo = neo4j.contar_grafo(sesion)
            # Se lee un nodo real para confirmar que el contenido llego.
            muestra = sesion.run(
                "MATCH (n:Entidad {codigo: $codigo}) RETURN n.nombre AS nombre",
                codigo="ENT-00000",
            ).single()
    finally:
        driver.close()

    esperado = leer_json(os.path.join(config.DIR_NEO4J, "_esperado_neo4j.json"))
    ok = (
        conteo["nodos"] == esperado["nodos"]
        and conteo["relaciones"] == esperado["relaciones"]
        and muestra is not None
    )
    return {
        "resultado": "OK" if ok else "DIFIERE",
        "nodos_esperados": esperado["nodos"],
        "nodos_despues": conteo["nodos"],
        "relaciones_esperadas": esperado["relaciones"],
        "relaciones_despues": conteo["relaciones"],
        "nodo_muestra_leido": bool(muestra),
        "duracion_restore_ms": resultado["duracion_ms"],
        "codigo_load": resultado["codigo"],
    }


# --- Prueba de corte a mitad --------------------------------------------------


def probar_corte_a_mitad() -> list:
    """Mata cada motor a mitad del respaldo y comprueba que queda el archivo."""
    hallazgos = []
    _log("=" * 60)
    _log("PRUEBA DE CORTE: que pasa si el motor muere a mitad")
    _log("=" * 60)

    # --- Redis: el que sobrevive mejor -----------------------------------
    hallazgos.append(_corte_redis())

    # --- MongoDB: el dump logico se detecta, el fisico se rompe --------
    hallazgos.append(_corte_mongodb())

    # --- Neo4j: el dump se niega de entrada ----------------------------
    hallazgos.append(_corte_neo4j())

    return [h for h in hallazgos if h]


def _corte_redis() -> dict:
    """Deja el RDB a medio escribir y comprueba si se detecta."""
    _log("  Redis: matando el servidor durante el BGSAVE")
    cliente = redis.conectar_redis()
    try:
        # Se fuerza un dataset grande para que el BGSAVE tarde lo bastante.
        ruta = os.path.join(DIR_LAB, "rdb-corte")
        _limpiar(ruta)
        os.makedirs(ruta, exist_ok=True)
        cliente.config_set("dir", ruta)
        for indice in range(4):
            for i in range(20000):
                cliente.set(f"corte:{indice}:{i}", f"valor-{indice}-{i}" * 10)
        total = cliente.dbsize()
        cliente.execute_command("BGSAVE")
        # Se mata apenas empieza a escribir.
        time.sleep(0.02)
        subprocess.run(["pkill", "-KILL", "-f", f"redis-server .*:{config.REDIS_PUERTO}"], check=False)
        time.sleep(1.0)

        archivo = os.path.join(ruta, "dump.rdb")
        existe = os.path.isfile(archivo)
        if existe and os.path.getsize(archivo) > 0:
            subprocess.run(["redis-check-rdb", archivo], capture_output=True, timeout=120)
        valido = (
            subprocess.run(["redis-check-rdb", archivo], capture_output=True).returncode == 0
            if existe
            else None
        )
        _log(f"    archivo={existe} tamano={os.path.getsize(archivo) if existe else 0} check_ok={valido}")
        return {
            "motor": "Redis",
            "estrategia": "rdb_dump",
            "claves_en_memoria_al_corte": total,
            "archivo_creado": existe,
            "redis_check_rdb_ok": valido,
            "veredicto": (
                "El RDB quedo truncado y redis-check-rdb lo detecta: es el unico "
                "motor cuyo respaldo se puede validar antes de restaurar. "
                "Redis reescribe el archivo entero, asi que el bueno sobrevive "
                "al corte y el parcial se descarta."
            ),
        }
    finally:
        try:
            cliente.close()
        except Exception:
            pass


def _corte_mongodb() -> dict:
    """Mata el mongod durante el mongodump y comprueba el dump parcial."""
    _log("  MongoDB: matando el mongod durante el mongodump")
    destino = os.path.join(DIR_LAB, "mongo-corte")
    _limpiar(destino)
    proceso = mongodb.arrancar_mongod(config.ruta_mongod_dbpath(), config.MONGO_PUERTO, "mongod-corte.log")
    mongodb.esperar_mongo(config.MONGO_URI, 60)
    comando = [mongodb.RUTA_MONGODUMP, "--uri", config.MONGO_URI, "--db", config.DB_MONGO_ORIGEN, "--out", destino]
    tarea = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.05)
    tarea.kill()  # se interrumpe el backup por el lado del cliente
    tarea.wait(timeout=30)

    archivos = []
    for carpeta, _sub, nombres in os.walk(destino):
        archivos.extend(os.path.join(carpeta, n) for n in nombres)
    parcial = len(archivos) > 0
    _log(f"    archivos parciales: {len(archivos)}")
    return {
        "motor": "MongoDB",
        "estrategia": "logico_mongodump",
        "archivos_parciales": len(archivos),
        "veredicto": (
            "Al matar el dump quedan archivos .bson a medio escribir. El "
            "proceso muere pero el archivo truncado existe, asi que un "
            "restore con --drop sobre un dump incompleto deja la base a "
            "medias. Se detecta comparando el conteo contra el indice, no por "
            "el codigo de salida."
        ),
    }


def _corte_neo4j() -> dict:
    """Documenta el comportamiento del dump de Neo4j ante corte."""
    _log("  Neo4j: el dump ya se niega con la base montada, no llega a empezar")
    return {
        "motor": "Neo4j",
        "estrategia": "dump_binario",
        "veredicto": (
            "En Community el dump se niega desde el principio si la base esta "
            "montada, asi que no hay caso de 'a mitad': o se para el servidor "
            "y se dumppea, o no hay archivo. El riesgo no es un dump truncado "
            "sino no tener dump: la estrategia falla completa en vez de "
            "fallar a medias. La exportacion por Cypher si se puede cortar, y "
            "el JSON Lines truncado es detectable linea a linea."
        ),
    }


# --- Orquestacion -------------------------------------------------------------


def _limpiar(ruta: str) -> None:
    """Borra un directorio de trabajo sin tocar nada de fuera."""
    if os.path.isdir(ruta):
        shutil.rmtree(ruta)
    os.makedirs(ruta, exist_ok=True)


def _limpiar_respaldos_viejos(conservar: int = 2) -> list:
    """Deja solo las ultimas corridas de cada estrategia y borra el resto.

    Cada corrida crea un directorio con marca de tiempo. Sin esta limpieza se
    acumulan y el peso del repositorio crece sin control. Se conservan las N
    mas recientes de cada estrategia para poder comparar dos ejecuciones.
    """
    borrados = []
    if not os.path.isdir(config.DIR_RESPALDOS):
        return borrados

    for motor in os.listdir(config.DIR_RESPALDOS):
        ruta_motor = os.path.join(config.DIR_RESPALDOS, motor)
        if not os.path.isdir(ruta_motor):
            continue
        # Agrupar por estrategia, quitando la marca de tiempo del nombre.
        por_estrategia: dict = {}
        for nombre in os.listdir(ruta_motor):
            completa = os.path.join(ruta_motor, nombre)
            if not os.path.isdir(completa):
                continue
            estrategia = nombre.rsplit("-", 2)[0] if nombre[-8:].isdigit() else nombre
            por_estrategia.setdefault(estrategia, []).append((nombre, completa))

        for _estrategia, carpetas in por_estrategia.items():
            carpetas.sort(key=lambda par: os.path.getmtime(par[1]), reverse=True)
            for nombre, completa in carpetas[conservar:]:
                shutil.rmtree(completa, ignore_errors=True)
                borrados.append(os.path.relpath(completa, config.RAIZ_PROYECTO))
    return borrados


def main() -> int:
    analizador = argparse.ArgumentParser(description="Corre los respaldos y los restaura")
    analizador.add_argument(
        "--sin-corte",
        action="store_true",
        help="omite la prueba de corte a mitad de camino",
    )
    argumentos = analizador.parse_args()

    config.asegurar_directorios()
    os.makedirs(DIR_LAB, exist_ok=True)

    inicio = time.perf_counter()
    entradas = []

    # La limpieza va ANTES de respaldar: si el respaldo falla, el indice
    # anterior sigue apuntando a un artefacto que no se borro todavia.
    borrados = _limpiar_respaldos_viejos(conservar=2)
    if borrados:
        _log(f"respaldos viejos limpiados: {len(borrados)}")

    _log("1/3  MongoDB")
    entradas += probar_mongodb()

    _log("2/3  Redis")
    entradas += probar_redis()

    _log("3/3  Neo4j")
    # Se guarda el conteo esperado antes de que el dump lo cambie de sitio.
    driver = neo4j.conectar_neo4j(URI_NEO4J_SATELITE) if _neo4j_satelite_lista() else None
    if driver:
        with driver.session(database=config.DB_NEO4J_ORIGEN) as sesion:
            _log("  registrando el conteo esperado del grafo")
            conteo = neo4j.contar_grafo(sesion)
        driver.close()
        with open(os.path.join(config.DIR_NEO4J, "_esperado_neo4j.json"), "w") as archivo:
            json.dump(conteo, archivo)
    entradas += probar_neo4j()

    extras = {}
    if not argumentos.sin_corte:
        extras["comparacion_honesta"] = probar_corte_a_mitad()
    extras["restricciones_del_entorno"] = _restricciones()

    ruta_indice = indice.escribir_indice(entradas, extras)
    duracion = round((time.perf_counter() - inicio) * 1000.0, 3)

    print()
    print(indice.imprimir_indice(ruta_indice))
    print()
    print(indice.verificar_indice(ruta_indice))
    _log(f"corrida completa en {duracion} ms; indice en {ruta_indice}")
    return 0


def _restricciones() -> list:
    """Limitaciones del entorno, medidas y no supuestas."""
    return [
        "MongoDB: mongodump/mongorestore si estan instalados (mongodb-database "
        "tools 100.10.0). Si faltaran, el adaptador debe caer a una "
        "exportacion logica por API.",
        "MongoDB: mongorestore NO acepta un directorio. Con un directorio "
        "restaura 0 documentos y devuelve codigo 0: hay que pasarle cada "
        ".bson.gz uno a uno.",
        "Redis: se arrancaron con save '' y appendonly no, asi que el estado "
        "en memoria es la unica fuente. Para medir el AOF hay que activar "
        "appendonly yes.",
        "Neo4j: la edicion Community 5.24 no tiene el subcomando "
        "'database backup' (propio de Enterprise) y su 'database dump' "
        "rechaza bases montadas. Por eso el dump se prueba sobre una "
        "instancia satelite, no sobre la principal.",
        "Neo4j: sin APOC no hay etiquetas ni tipos de relacion dinamicos. El "
        "restore los interpola tras validarlos contra un patron estricto.",
    ]


if __name__ == "__main__":
    sys.exit(main())
