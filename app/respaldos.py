#!/usr/bin/env python3
"""respaldos — el indice de estrategias de respaldo.

Este modulo es/documentacion declarativa: describe QUE protege y QUE NO protege
cada estrategia. No ejecuta nada; los motores los ejecuta `adaptadores/`, y la
restauracion la dirige `ejecucion.py`.

La columna de "no protege" es la que importa: una estrategia que no dice que
limita no esta documentada, esta vendida.

Las fichas se toman de los adaptadores, que son la fuente unica: lo que la
web muestra y lo que el indice mide son lo mismo.
"""

try:
    from .adaptadores import mongodb, neo4j, redis
except ImportError:  # pragma: no cover - importacion como modulo suelto
    from adaptadores import mongodb, neo4j, redis

ESTRATEGIAS = {
    "mongodb:logico_mongodump": {
        "id": "mongodb:logico_mongodump",
        "motor": "mongodb",
        "clave": "logico_mongodump",
        "nombre": "Respaldo logico con mongodump",
        "resumen": "Exporta la base completa a archivos BSON por coleccion usando mongodump, comprimidos con gzip. Se ejecuta con el servidor encendido porque habla el protocolo de red en vez de tocar archivos.",
        "cuando_usarla": "Respaldo diario o por noche sin parar el servicio, migraciones de version y transporte de datos entre servidores.",
        "protege": ["Los documentos y los indices de las colecciones. Al ser un dump logico, ignora el formato fisico: el archivo se puede cargar en cualquier motor de la misma version mayor."],
        "no_protege": ["Los usuarios, los roles y los permisos (van aparte, en authSchema), los datos locales de oplog, el estado interno de WiredTiger ni los archivos de configuracion."],
        "soporta": ["mongodb"],
        "requiere_parar_servidor": False,
    },
    "mongodb:fisico_wiredtiger": {
        "id": "mongodb:fisico_wiredtiger",
        "motor": "mongodb",
        "clave": "fisico_wiredtiger",
        "nombre": "Copia en frio de WiredTiger",
        "resumen": "Con el motor DETENIDO se copia el dbpath entero: los archivos .wt de cada coleccion, los metadatos del motor (WiredTiger, WiredTiger.turtle, el catalogo y sizeStorer) y el directorio journal. Se empaqueta en un solo .tar.gz con rutas planas, que es como WiredTiger espera encontrarlos. NO es un snapshot en caliente.",
        "cuando_usarla": "Consolidaciones, migraciones de hardware, o cuando se quiere recuperar el almacenamiento completo tal cual estaba, incluidos sus metadatos internos y el journal.",
        "protege": ["El estado completo del almacenamiento: datos, indices internos, punto de control del journal y catalogo de colecciones."],
        "no_protege": ["No se puede elegir una base: el archivo es del MOTOR COMPLETO, con todas las bases del dbpath. Por eso no sustituye a mongodump cuando lo que se quiere es respaldar una base concreta. Solo sirve para la misma version y formato de WiredTiger y es opaco. El restore exige un dbpath vacio y el motor apagado, asi que la ventana sin servicio es doble."],
        "soporta": ["mongodb"],
        "requiere_parar_servidor": True,
    },
    "redis:rdb_dump": {
        "id": "redis:rdb_dump",
        "motor": "redis",
        "clave": "rdb_dump",
        "nombre": "Respaldo RDB con BGSAVE",
        "resumen": "Pide al servidor un punto de control binario de toda la base de datos y lo copia a un archivo dump.rdb. Redis 7.0 usa copy-on-write: mientras se copia, las paginas modificadas se duplican, de modo que el archivo es una foto consistente aunque el servidor siga aceptando escrituras.",
        "cuando_usarla": "Copia rapida y de tamano minimo para respaldos periodicos y clonados de maquina, cuando no importa perder los ultimos segundos de escritura.",
        "protege": ["El estado completo del conjunto de claves en el instante del punto de control, con toda su codificacion interna."],
        "no_protege": ["Los comandos ejecutados entre checkpoints, el historial de escrituras, los streams y las claves ya expiradas. Tampoco es legible sin redis-check-rdb."],
        "soporta": ["redis"],
        "requiere_parar_servidor": False,
    },
    "redis:aof_append": {
        "id": "redis:aof_append",
        "motor": "redis",
        "clave": "aof_append",
        "nombre": "Respaldo del AOF con BGREWRITEAOF",
        "resumen": "Replica el archivo appendonly.aof, que es un registro de todas las ordenes de escritura. Con BGREWRITEAOF se reescribe en un solo archivo compacto equivalente al estado actual, lo que evita acarrear un historial infinito.",
        "cuando_usarla": "Cuando no se puede tolerar perder escrituras: el AOF conserva las ordenes, no solo el estado, asi que su ventana de perdida es mucho menor que la del RDB.",
        "protege": ["Todas las escrituras aceptadas desde la ultima vez que se reescribio el archivo, en orden y con su secuencia."],
        "no_protege": ["Lo que se perdio si el servidor murio antes de vaciar el buffer a disco: el AOF tambien escribe por lotes, asi que su garantia depende de appendfsync."],
        "soporta": ["redis"],
        "requiere_parar_servidor": False,
    },
    "neo4j:dump_binario": {
        "id": "neo4j:dump_binario",
        "motor": "neo4j",
        "clave": "dump_binario",
        "nombre": "Dump binario con neo4j-admin",
        "resumen": "neo4j-admin database dump empaqueta la base completa en un solo archivo .dump con los registros, los indices y los registros de transaccion. Es una foto exacta del almacenamiento.",
        "cuando_usarla": "Restauraciones completas y rapidas sobre la misma version de Neo4j, por ejemplo al recuperar un servidor caido.",
        "protege": ["Todo el contenido del grafo mas los indices internos y el punto de control de transacciones."],
        "no_protege": ["No se puede leer ni consultar sin restaurarlo, no se puede cargar en otra version mayor y, en la edicion Community, exige que la base este detenida mientras se hace y mientras se carga."],
        "soporta": ["neo4j"],
        "requiere_parar_servidor": True,
    },
    "neo4j:exportacion_cypher": {
        "id": "neo4j:exportacion_cypher",
        "motor": "neo4j",
        "clave": "exportacion_cypher",
        "nombre": "Exportacion a JSON Lines con Cypher",
        "resumen": "Extrae los nodos y las aristas en consultas separadas y los escribe como texto JSON Lines, con los identificadores reemplazados por claves de negocio para poder reconstruirlos.",
        "cuando_usarla": "Cuando el archivo tiene que ser legible, auditable, comparable entre versiones o versionable en un repositorio de codigo.",
        "protege": ["Nodos, etiquetas, propiedades y relaciones con sus tipos y propiedades, en un formato que se puede revisar a mano."],
        "no_protege": ["Los identificadores internos de Neo4j, los indices, las restricciones, los permisos y el historial de transacciones. Reconstruir el grafo cuesta tiempo si es muy grande."],
        "soporta": ["neo4j"],
        "requiere_parar_servidor": False,
    },
}



def listar_estrategias():
    """El indice completo de estrategias, como lista de dicts.

    La web lo consume tal cual, asi que cada ficha lleva ademas el motor al que
    pertenece y si exige detener el servicio: son los dos datos que un lector
    necesita para elegir, y antes no aparecian.
    """
    return [dict(ficha) for ficha in ESTRATEGIAS.values()]
