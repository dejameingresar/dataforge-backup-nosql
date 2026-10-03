#!/usr/bin/env python3
"""Configuracion central del proyecto DataForge Backup.

Reune las rutas y los parametros de conexion de los tres motores NoSQL.
Todo el codigo del proyecto lee sus rutas desde aqui, de modo que cambiar
el directorio de respaldos es un cambio de un solo lugar.
"""
from __future__ import annotations

import os

# --- Raiz del proyecto -------------------------------------------------------

RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `respaldos/` guarda los artefactos generados; `indice/` guarda el indice JSON.
DIR_RESPALDOS = os.path.join(RAIZ_PROYECTO, "respaldos")
DIR_INDICE = os.path.join(RAIZ_PROYECTO, "indice")

# Cada motor recibe un subdirectorio propio para no mezclar artefactos.
DIR_MONGODB = os.path.join(DIR_RESPALDOS, "mongodb")
DIR_REDIS = os.path.join(DIR_RESPALDOS, "redis")
DIR_NEO4J = os.path.join(DIR_RESPALDOS, "neo4j")

# Bases de trabajo usadas para medir los round-trip de restauracion.
# Los restaures nunca tocan el motor principal: se levantan instancias
# satelite en otros puertos para poder comparar conteos sin riesgo.
# Cada motor tiene su base propia: MongoDB la base dataforge_backup, Redis el
# indice 0 y Neo4j la base neo4j del servidor.
DB_MONGO_ORIGEN = "dataforge_backup"
# Neo4j Community 5.24 no tiene el subcomando 'database create', asi que no se
# puede crear una base de datos nueva sin reinstalar el motor. El grafo de
# pruebas vive en la base neo4j del servidor y se separa por la propiedad
# 'codigo' y por las etiquetas propias, que en Neo4j 5 ya son espacios de
# nombres implicitos.
DB_NEO4J_ORIGEN = "neo4j"
DB_REDIS_ORIGEN = 0
DB_REDIS_DESTINO = 1
# Redis tiene 16 bases numeradas de 0 a 15. Los helpers recorren ese rango
# cuando necesitan contar el contenido de todas, no solo de una.
REDIS_PUERTO_DB_MIN = 0
REDIS_PUERTO_DB_MAX = 15

# --- Rutas de los binarios ---------------------------------------------------

MONGOD_BIN = (
    "/home/prodriguez/opt/mongodb/mongodb-linux-x86_64-ubuntu2204-7.0.14/bin/mongod"
)
NEO4J_DIR = "/home/prodriguez/opt/neo4j-community-5.24.0"
NEO4J_ADMIN_BIN = os.path.join(NEO4J_DIR, "bin", "neo4j-admin")
NEO4J_DB_DIR = os.path.join(NEO4J_DIR, "data", "databases")
NEO4J_LOG_DIR = os.path.join(NEO4J_DIR, "logs", "neo4j-admin.log")

# mongodump/mongorestore van aparte del servidor: se descargan como
# "mongodb-database-tools" y se decomprimen en ~/opt/mongodb-tools. La ruta se
# resuelve en tiempo de ejecucion para que funcione tanto en esta maquina como
# en Render, donde hay que instalarlos en el buildCommand.
_HERRAMIENTAS_MONGODB = os.environ.get(
    "DATAFORGE_MONGO_TOOLS",
    os.path.join(os.path.expanduser("~"), "opt", "mongodb-tools",
                 "mongodb-database-tools-ubuntu2204-x86_64-100.10.0", "bin"),
)


def _herramienta(nombre):
    """Ruta de mongodump/mongorestore: la de DATAFORGE_MONGO_TOOLS si existe,
    y si no, la primera que se encuentre en el PATH."""
    en_tools = os.path.join(_HERRAMIENTAS_MONGODB, nombre)
    if os.path.isfile(en_tools) and os.access(en_tools, os.X_OK):
        return en_tools
    import shutil
    return shutil.which(nombre) or en_tools


MONGODUMP_BIN = _herramienta("mongodump")
MONGORESTORE_BIN = _herramienta("mongorestore")

# --- Conexion ----------------------------------------------------------------

MONGO_URI = "mongodb://127.0.0.1:27017"
MONGO_HOST = "127.0.0.1"
MONGO_PUERTO = 27017
MONGO_PUERTO_SATELITE = 27018  # instancia donde se prueba el restore
# Puerto de la instancia que guarda los datos DE ESTA APLICACION, cuando hace
# falta levantarla separada del mongod compartido.
MONGO_PUERTO_APLICACION = 27019

REDIS_HOST = "127.0.0.1"
REDIS_PUERTO = 6379
REDIS_PUERTO_SATELITE = 6380  # instancia donde se prueba el restore

NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_USUARIO = None  # la instancia local corre sin autenticacion
NEO4J_CLAVE = None

# Tiempos de espera (ms) para no bloquear el proceso si un motor no responde.
TIMEOUT_CONEXION_MS = 4000
TIMEOUT_ESPERA_MS = 120_000

# Semilla fija del generador de datos de prueba: hace que los conteos sean
# reproducibles entre ejecuciones y que el round-trip sea comparable.
SEMILLA_DATOS = 20260101

CANTIDAD_CLIENTES = 150
CANTIDAD_PRODUCTOS = 400
CANTIDAD_PEDIDOS = 600
CANTIDAD_EVENTOS = 250
CANTIDAD_NODOS_GRAFO = 200


def asegurar_directorios() -> None:
    """Crea la arbol de directorios de trabajo si todavia no existe."""
    for ruta in (
        DIR_RESPALDOS,
        DIR_INDICE,
        DIR_MONGODB,
        DIR_REDIS,
        DIR_NEO4J,
    ):
        os.makedirs(ruta, exist_ok=True)


def ruta_mongod_dbpath() -> str:
    """Devuelve el dbpath DEDICADO de esta aplicacion.

    No se usa el dbpath compartido de la maquina (~nosql-lab/mongo): ahi
    viven las bases de otros proyectos y una copia fisica del almacenamiento
    arrastraria sus datos. Ademas, los restos de otra base harian que el
    round-trip se midiera contra el contenido equivocado.
    """
    return "/home/prodriguez/nosql-lab/mongo-backup"


def ruta_mongod_log() -> str:
    return os.path.join(DIR_RESPALDOS, "mongod-satelite.log")
