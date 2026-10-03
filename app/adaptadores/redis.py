#!/usr/bin/env python3
"""Adaptador de respaldo y restauracion para Redis.

Expone DOS estrategias distintas:

  1. `rdb_dump`     -> SAVE/BGSAVE, que produce un unico archivo dump.rdb.
  2. `aof_append`   -> appendonly.aof, con BGREWRITEAOF para compactar.

La diferencia de fondo es el formato. RDB es una foto binaria compacta que
solo se puede leer con redis-check-rdb; el AOF es un registro de ordenes de
escritura que se puede reejecutar clave por clave. Por eso el AOF se pierde
antes en un corte de luz: guarda el historial, no el estado.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
from typing import Any

import redis

from .. import config
from ..utileria import (
    copiar_arbol,
    cronometro,
    escribir_json_atomico,
    escribir_manifiesto,
    leer_json,
    sha256_de_archivo,
    sha256_de_manifiesto,
    tamano_de_ruta,
    verificar_manifiesto,
)

NOMBRE_REDIS = "Redis"
VERSION_REDIS = "7.0.15"

BIN_REDIS_SERVER = "/usr/bin/redis-server"
BIN_REDIS_CHECK_RDB = "/usr/bin/redis-check-rdb"
BIN_REDIS_CLI = "/usr/bin/redis-cli"


ESTRATEGIAS = {
    "rdb_dump": {
        "nombre": "Respaldo RDB con BGSAVE",
        "descripcion": (
            "Pide al servidor un punto de control binario de toda la base de "
            "datos y lo copia a un archivo dump.rdb. Redis 7.0 usa "
            "copy-on-write: mientras se copia, las paginas modificadas se "
            "duplican, de modo que el archivo es una foto consistente aunque "
            "el servidor siga aceptando escrituras."
        ),
        "cuando_usarla": (
            "Copia rapida y de tamano minimo para respaldos periodicos y "
            "clonados de maquina, cuando no importa perder los ultimos "
            "segundos de escritura."
        ),
        "protege": (
            "El estado completo del conjunto de claves en el instante del "
            "punto de control, con toda su codificacion interna."
        ),
        "no_protege": (
            "Los comandos ejecutados entre checkpoints, el historial de "
            "escrituras, los streams y las claves ya expiradas. Tampoco es "
            "legible sin redis-check-rdb."
        ),
        "requiere_parar": False,
    },
    "aof_append": {
        "nombre": "Respaldo del AOF con BGREWRITEAOF",
        "descripcion": (
            "Replica el archivo appendonly.aof, que es un registro de todas "
            "las ordenes de escritura. Con BGREWRITEAOF se reescribe en un "
            "solo archivo compacto equivalente al estado actual, lo que evita "
            "acarrear un historial infinito."
        ),
        "cuando_usarla": (
            "Cuando no se puede tolerar perder escrituras: el AOF conserva "
            "las ordenes, no solo el estado, asi que su ventana de perdida es "
            "mucho menor que la del RDB."
        ),
        "protege": (
            "Todas las escrituras aceptadas desde la ultima vez que se "
            "reescribio el archivo, en orden y con su secuencia."
        ),
        "no_protege": (
            "Lo que se perdio si el servidor murio antes de vaciar el buffer "
            "a disco: el AOF tambien escribe por lotes, asi que su garantia "
            "depende de appendfsync."
        ),
        "requiere_parar": False,
    },
}


# --- Conexion ----------------------------------------------------------------


def conectar_redis(puerto: int = config.REDIS_PUERTO) -> redis.Redis:
    """Abre un cliente de Redis y comprueba que responde."""
    cliente = redis.Redis(
        host=config.REDIS_HOST,
        port=puerto,
        socket_connect_timeout=config.TIMEOUT_CONEXION_MS / 1000.0,
        decode_responses=False,
    )
    cliente.ping()
    return cliente


def version_redis(cliente) -> str:
    return cliente.info("server")["redis_version"]


def ruta_dump_rdb(cliente) -> str:
    """Ruta absoluta del dump.rdb segun la configuracion del servidor."""
    directorio = cliente.config_get("dir").get("dir")
    nombre = cliente.config_get("dbfilename").get("dbfilename")
    if directorio in (None, "") or nombre in (None, ""):
        raise RuntimeError("El servidor no informo de dir ni de dbfilename.")
    if not os.path.isabs(directorio):
        directorio = os.path.abspath(ositorio)
    return os.path.join(directorio, nombre)


def ruta_appendonly(cliente) -> str:
    """Ruta absoluta del AOF. En Redis 7.0 vive en un subdirectorio propio."""
    directorio = cliente.config_get("dir").get("dir")
    sub = cliente.config_get("appenddirname").get("appenddirname") or "appendonlydir"
    nombre = (
        cliente.config_get("appendfilename").get("appendfilename")
        or "appendonly.aof"
    )
    base = directorio if os.path.isabs(directorio) else os.path.abspath(directorio)
    return os.path.join(base, sub, nombre)


def listar_archivos_aof(cliente) -> list:
    """Lista los archivos reales del AOF, incluidos el manifiesto y los base.

    Redis 7.0 usa AOF multipart: hay un archivo manifest que declara los
    archivos base y los incrementales, mas un incremental propio. Copiar solo
    el appendonly.aof dejaria fuera los incrementales, y con ellos se pierde
    todo lo escrito despues del ultimo base.
    """
    ruta = ruta_appendonly(cliente)
    directorio = os.path.dirname(ruta)
    encontrados = sorted(glob.glob(os.path.join(directorio, "*")))
    return [r for r in encontrados if os.path.isfile(r)]


def conectar_redis_db(
    indice: int, puerto: int = config.REDIS_PUERTO
) -> redis.Redis:
    """Abre un cliente apuntando a un numero de base concreto.

    En redis-py 8 el numero de base NO se pasa a los comandos: va en el
    constructor. Por eso los comandos que lo necesitan (dbsize, flushdb) se
    ejecutan sobre un cliente creado para esa base, y no sobre el cliente
    principal.
    """
    return redis.Redis(
        host=config.REDIS_HOST,
        port=puerto,
        db=indice,
        socket_connect_timeout=config.TIMEOUT_CONEXION_MS / 1000.0,
        decode_responses=False,
    )


def total_claves(cliente, indice: int = 0) -> int:
    """Cuenta las claves con datos en un indice de la base.

    Se cuenta con SCAN sobre ese indice, en vez de con DBSIZE: DBSIZE suma
    tambien las claves que ya expiraron y siguen marcadas para eliminar, asi
    que puede dar mas claves de las que realmente se restauraron.
    """
    if indice == config.DB_REDIS_ORIGEN and cliente.connection_pool.connection_kwargs.get(
        "db", 0
    ) == indice:
        return _contar_con_scan(cliente)
    propio = conectar_redis_db(indice)
    try:
        return _contar_con_scan(propio)
    finally:
        propio.close()


def _contar_con_scan(cliente) -> int:
    """Recorre las claves de la base del cliente y las cuenta de verdad."""
    total = 0
    for _clave in cliente.scan_iter(match="*", count=500):
        total += 1
    return total


# --- Estrategia 1: RDB -------------------------------------------------------


def generar_respaldo_rdb(
    destino: str,
    indice: int = config.DB_REDIS_ORIGEN,
    usar_bgsave: bool = True,
) -> dict:
    """Toma el punto de control RDB y copia el dump.rdb a `destino`.

    Redis 7 PROTEGE el parametro `dir`: CONFIG SET dir devuelve "can't set
    protected config", asi que no se puede decirle al servidor que escriba el
    archivo donde queremos. Por eso el metodo es BGSAVE y luego copiar el
    dump.rdb de donde el servidor decidio dejarlo.

    Se elige BGSAVE y no SAVE porque SAVE bloquea el servidor mientras
    escribe, y en una base grande deja el servicio sin responder.

    El archivo cubre LAS 16 BASES del servidor, no una sola: es el volcado
    completo. Si lo que se quisiera fuera una base concreta, la via seria
    redis-cli --rdb, que usa replicacion y si admite elegir.
    """
    os.makedirs(destino, exist_ok=True)
    cliente = conectar_redis()
    try:
        ruta_original = ruta_dump_rdb(cliente)
        directorio_original = os.path.dirname(ruta_original)

        inicio = time.perf_counter()
        if usar_bgsave:
            _esperar_bgsave(cliente)
        else:
            cliente.execute_command("SAVE")
        duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)

        # Redis escribe primero en un temporal y lo renombra al terminar: si
        # el archivo definitivo existe con el tamano esperado, el BGSAVE
        # acabo de verdad y no a medias.
        if not os.path.isfile(ruta_original):
            raise RuntimeError(
                f"BGSAVE dijo que guardo pero no hay dump.rdb en {ruta_original}. "
                "Revisa los permisos de escritura del directorio del servidor."
            )

        # Se copia el archivo definitivo al destino del proyecto.
        copia = os.path.join(destino, "dump.rdb")
        shutil.copy2(ruta_original, copia)

        validacion = _validar_rdb(copia)
        manifiesto = escribir_manifiesto(os.path.join(destino, "manifiesto.json"), destino)

        return {
            "estrategia": "rdb_dump",
            "archivo": copia,
            "origen": ruta_original,
            "manifiesto": manifiesto,
            "bytes": os.path.getsize(copia),
            "duracion_ms": duracion_ms,
            "validacion_rdb": validacion,
            "metodo": "BGSAVE" if usar_bgsave else "SAVE",
            "base_indice": indice,
            "ambito": "las 16 bases del servidor (volcado completo)",
        }
    finally:
        cliente.close()


def _esperar_bgsave(cliente, timeout: float = 180.0) -> float:
    """Lanza BGSAVE y espera a que termine de verdad.

    Espera a que desaparezca rdb_bgsave_in_progress, no a que el comando
    responda: BGSAVE responde de inmediato y el trabajo sigue en segundo
    plano. Copiar el dump.rdb mientras se escribe daria un archivo a medias.

    Si ya hay un BGSAVE en curso, se espera a que termine y se lanza otro, en
    vez de tratar el mensaje de error como un fallo.
    """
    inicio = time.perf_counter()
    marca_previa = cliente.info("persistence").get("rdb_last_save_time", 0)

    for _intento in range(3):
        estado = cliente.info("persistence")
        if estado.get("rdb_bgsave_in_progress") == 0:
            break
        # Hay un guardado en curso: se espera a que termine.
        limite = time.time() + timeout
        while time.time() < limite:
            if cliente.info("persistence").get("rdb_bgsave_in_progress") == 0:
                break
            time.sleep(0.1)
        marca_previa = cliente.info("persistence").get("rdb_last_save_time", 0)

    try:
        cliente.execute_command("BGSAVE")
    except redis.ResponseError as error:
        # "Background save already in progress" es concurrencia, no un fallo:
        # si ya hay un guardado en curso, ese sirve y se espera a que acabe.
        if "already in progress" not in str(error):
            raise RuntimeError(f"BGSAVE fue rechazado: {error}") from error

    limite = time.time() + timeout
    while time.time() < limite:
        estado = cliente.info("persistence")
        if (
            estado.get("rdb_bgsave_in_progress") == 0
            and estado.get("rdb_last_save_time", 0) > marca_previa
        ):
            if estado.get("rdb_last_bgsave_status") != "ok":
                raise RuntimeError(
                    "BGSAVE termino con estado "
                    f"{estado.get('rdb_last_bgsave_status')}"
                )
            return round((time.perf_counter() - inicio) * 1000.0, 3)
        time.sleep(0.05)
    raise RuntimeError(f"BGSAVE no termino dentro de {timeout} segundos.")


def _validar_rdb(archivo: str) -> dict:
    """Ejecuta redis-check-rdb y devuelve si la estructura es correcta."""
    if not os.path.isfile(BIN_REDIS_CHECK_RDB):
        return {"ejecutado": False, "motivo": "redis-check-rdb no esta instalado"}
    resultado = subprocess.run(
        [BIN_REDIS_CHECK_RDB, archivo],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return {
        "ejecutado": True,
        "codigo": resultado.returncode,
        "ok": resultado.returncode == 0,
        "salida": resultado.stdout.strip().split("\n")[-1] if resultado.stdout else "",
    }


def restaurar_rdb(
    archivo_rdb: str,
    puerto_destino: int = config.REDIS_PUERTO_SATELITE,
    indice: int = config.DB_REDIS_ORIGEN,
    timeout_arranque: float = 30.0,
) -> dict:
    """Arranca un redis-server efimero que carga el RDB y cuenta las claves.

    No se usa DEBUG RELOAD ni FLUSHALL sobre el servidor principal: se levanta
    una instancia nueva en otro puerto que solo existe para medir el restore.
    """
    directorio = os.path.dirname(os.path.abspath(archivo_rdb))
    puerto = puerto_destino

    inicio = time.perf_counter()
    proceso = _arrancar_redis_satelite(directorio, puerto, indice, timeout_arranque)
    cliente = redis.Redis(
        host=config.REDIS_HOST, port=puerto, socket_connect_timeout=5,
        decode_responses=False,
    )
    try:
        # Se espera a que termine la carga antes de contar: si se pregunta
        # durante el loading, dbsize responde sobre una base a medio cargar.
        loaded = _esperar_carga_terminada(cliente)
        claves = total_claves(cliente, indice)
        total = _contar_todos_los_indices(cliente)
    finally:
        cliente.close()
        _detener_redis_satelite(proceso, puerto)

    duracion_ms = round((time.perf_counter() - inicio) * 1000.0, 3)
    return {
        "estrategia": "rdb_dump",
        "puerto": puerto,
        "claves_indice": indice,
        "claves_restauradas": claves,
        "claves_totales": total,
        "duracion_ms": duracion_ms,
        "carga_completa": loaded,
    }


def _esperar_carga_terminada(cliente, timeout: float = 60.0) -> bool:
    """Espera a que el servidor termine de cargar el RDB o el AOF.

    Devuelve False si la carga no acaba dentro del plazo, que en la practica
    significa que el archivo estaba roto: un servidor que no puede cargar
    sigue respondiendo ping y por eso el fallo pasaria inadvertido.
    """
    limite = time.time() + timeout
    while time.time() < limite:
        if cliente.info("persistence").get("loading", 0) == 0:
            return True
        time.sleep(0.05)
    return False


def _contar_todos_los_indices(cliente) -> int:
    """Suma las claves reales de los 16 indices por defecto."""
    total = 0
    for indice in range(config.REDIS_PUERTO_DB_MIN, config.REDIS_PUERTO_DB_MAX + 1):
        propio = conectar_redis_db(indice)
        try:
            total += _contar_con_scan(propio)
        finally:
            propio.close()
    return total


# --- Estrategia 2: AOF -------------------------------------------------------


def _esperar_bgrewrite(cliente, timeout: float = 180.0) -> float:
    """Lanza BGREWRITEAOF y espera a que termine de verdad.

    El error "Background append only file rewriting already in progress" NO es
    un fallo del respaldo: significa que hay una reescritura en curso. Si se
    trata como error, la estrategia falla siempre contra un servidor vivo que
    acaba de reescribir. Lo correcto es tolerarlo y esperar a que termine.

    Se espera a que aof_rewrite_in_progress pase a 0 y luego se comprueba
    aof_last_bgrewrite_status: si dice err, ahi si hay un problema real.
    """
    inicio = time.perf_counter()
    try:
        cliente.execute_command("BGREWRITEAOF")
    except redis.ResponseError as error:
        if "already in progress" not in str(error):
            raise RuntimeError(f"BGREWRITEAOF fue rechazado: {error}") from error
        # Ya habia una reescritura en curso: se espera a que acabe.

    limite = time.time() + timeout
    while time.time() < limite:
        estado = cliente.info("persistence")
        if not estado.get("aof_rewrite_in_progress"):
            if estado.get("aof_last_bgrewrite_status") != "ok":
                raise RuntimeError(
                    "BGREWRITEAOF termino con estado "
                    f"{estado.get('aof_last_bgrewrite_status')}"
                )
            return round((time.perf_counter() - inicio) * 1000.0, 3)
        time.sleep(0.1)
    raise RuntimeError(f"BGREWRITEAOF no termino dentro de {timeout} segundos.")


def generar_respaldo_aof(destino: str, reescribir: bool = True) -> dict:
    """Activa el AOF, lo compacta con BGREWRITEAOF y copia los archivos.

    Redis 7.0 escribe el AOF en partes (multipart): un manifest mas los
    archivos base e incremental. Se copian TODOS, porque restaurar solo el
    .aof principal deja fuera los incrementales y se pierde lo escrito
    despues del ultimo base.
    """
    os.makedirs(destino, exist_ok=True)
    cliente = conectar_redis()
    try:
        directorio = cliente.config_get("dir").get("dir")
        if not os.path.isabs(directorio):
            directorio = os.path.abspath(directorio)

        # Se activa el AOF: es la unica forma de que Redis lo escriba. A
        # diferencia de `dir`, el parametro appendonly SI se puede cambiar en
        # caliente en Redis 7.
        cliente.config_set("appendonly", "yes")
        # aof-use-rdb-preamble es el punto clave de Redis 7.0: el AOF arranca
        # con un bloque RDB y sigue con las ordenes en incremental. Asi el
        # archivo arranca rapido aun con millones de claves.
        cliente.config_set("aof-use-rdb-preamble", "yes")

        if reescribir:
            # Al activar appendonly Redis ya dispara una reescritura por su
            # cuenta; por eso puede haber una en curso al pedir la nuestra y
            # el helper debe tolerarlo.
            duracion_ms = _esperar_bgrewrite(cliente)
        else:
            duracion_ms = 0.0

        origen = os.path.join(directorio, "appendonlydir")
        if not os.path.isdir(origen):
            raise RuntimeError(
                f"El AOF esta activo pero no existe {origen}. "
                "Puede que el servidor no tenga permiso de escritura."
            )

        destino_aof = os.path.join(destino, "appendonlydir")
        if os.path.isdir(destino_aof):
            shutil.rmtree(destino_aof)
        copiados = copiar_arbol(origen, destino_aof)

        manifiesto = escribir_manifiesto(os.path.join(destino, "manifiesto.json"), destino)

        return {
            "estrategia": "aof_append",
            "directorio": destino_aof,
            "manifiesto": manifiesto,
            "archivos": sorted(os.listdir(destino_aof)),
            "bytes": copiados,
            "duracion_ms": duracion_ms,
            "reescrito": reescribir,
        }
    finally:
        cliente.close()


def restaurar_aof(
    origen: str,
    puerto_destino: int = config.REDIS_PUERTO_SATELITE,
    indice: int = config.DB_REDIS_ORIGEN,
    timeout_arranque: float = 30.0,
) -> dict:
    """Levanta un redis-server que carga el AOF y comprueba las claves.

    El servidor tiene que arrancar con appendonly yes y el directorio
    apuntando a la copia: si no, Redis ignora el AOF y crea un RDB vacio, y
    el conteo daria 0 sin que nada pareciera estar mal.
    """
    directorio = os.path.dirname(os.path.abspath(origen))
    puerto = puerto_destino
    inicio = time.perf_counter()
    proceso = _arrancar_redis_satelite(
        directorio, puerto, indice, timeout_arranque, aof=True
    )
    cliente = redis.Redis(
        host=config.REDIS_HOST, port=puerto, socket_connect_timeout=5,
        decode_responses=False,
    )
    try:
        cargada = _esperar_carga_terminada(cliente)
        claves = total_claves(cliente, indice)
        total = _contar_todos_los_indices(cliente)
        # Se lee una clave para confirmar que el contenido llego de verdad.
        muestra = _primera_clave(cliente, indice)
    finally:
        cliente.close()
        _detener_redis_satelite(proceso, puerto)

    return {
        "estrategia": "aof_append",
        "puerto": puerto,
        "claves_indice": indice,
        "claves_restauradas": claves,
        "claves_totales": total,
        "carga_completa": cargada,
        "muestra": muestra,
        "duracion_ms": round((time.perf_counter() - inicio) * 1000.0, 3),
    }


def _primera_clave(cliente, indice: int) -> Any:
    """Devuelve el valor de la primera clave, para probar que no es un SDL vacio.

    Un RDB o un AOF corruptos pueden cargar sin error y devolver conteos
    correctos mientras los valores estan vacios. Leer una clave real lo
    descarta.
    """
    primera = cliente.scan_iter(count=1)
    for clave in primera:
        tipo = cliente.type(clave)
        if tipo == b"string":
            return {"clave": clave.decode("utf-8", "replace"), "valor": str(cliente.get(clave))[:120]}
        if tipo == b"hash":
            return {
                "clave": clave.decode("utf-8", "replace"),
                "campos": len(cliente.hgetall(clave)),
            }
        if tipo == b"list":
            return {"clave": clave.decode("utf-8", "replace"), "elementos": cliente.llen(clave)}
        if tipo == b"zset":
            return {"clave": clave.decode("utf-8", "replace"), "miembros": cliente.zcard(clave)}
        if tipo == b"set":
            return {"clave": clave.decode("utf-8", "replace"), "miembros": cliente.scard(clave)}
    return None


# --- Instancias satellite ----------------------------------------------------


def _arrancar_redis_satelite(
    directorio: str,
    puerto: int,
    indice: int,
    timeout: float,
    aof: bool = False,
) -> subprocess.Popen:
    """Arranca un redis-server que carga el respaldo y espera a que acepte."""
    log = open(os.path.join(config.DIR_RESPALDOS, "redis-satelite.log"), "ab")
    argumentos = [
        BIN_REDIS_SERVER,
        "--port",
        str(puerto),
        "--dir",
        directorio,
        "--dbfilename",
        "dump.rdb",
        "--save",
        "",
        "--daemonize",
        "no",
    ]
    if aof:
        argumentos += [
            "--appendonly",
            "yes",
            "--appendfilename",
            "appendonly.aof",
            "--appenddirname",
            "appendonlydir",
        ]
    else:
        argumentos += ["--appendonly", "no"]

    proceso = subprocess.Popen(argumentos, stdout=log, stderr=subprocess.STDOUT)

    limite = time.time() + timeout
    cliente = redis.Redis(
        host=config.REDIS_HOST, port=puerto, socket_connect_timeout=1,
        decode_responses=False,
    )
    while time.time() < limite:
        try:
            if cliente.ping():
                return proceso
        except redis.RedisError:
            time.sleep(0.1)
    _detener_redis_satelite(proceso, puerto)
    raise RuntimeError(
        f"El redis-server satelite en el puerto {puerto} no respondio en {timeout} s."
    )


def _detener_redis_satelite(proceso: subprocess.Popen, puerto: int) -> None:
    """Cierra el servidor efimero con SHUTDOWN NOSAVE para no crear mas RDB."""
    try:
        cliente = redis.Redis(
            host=config.REDIS_HOST,
            port=puerto,
            socket_connect_timeout=2,
        )
        try:
            cliente.execute_command("SHUTDOWN", "NOSAVE")
        except redis.RedisError:
            pass  # el cierre correcto corta la conexion: es lo esperado
        finally:
            cliente.close()
    except Exception:
        pass
    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proceso.kill()
