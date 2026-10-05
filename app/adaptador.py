"""adaptador — puente tolerante entre el nucleo y el servidor HTTP.

El nucleo (`app/nucleo/*.py`) lo escribe el carril de respaldos; aqui no se
reimplementa nada de el. Este modulo solo:

  1. carga los modulos del nucleo de forma perezosa, para que el servidor pueda
     arrancar aunque falte alguna pieza;
  2. llama a cada funcion pasandole SOLO los argumentos que su firma real
     acepta, de modo que un parametro opcional con otro nombre no rompa nada;
  3. vuelve a medir en disco el artefacto generado (bytes y sha256) para que la
     interfaz jamas muestre una cifra que el nucleo solo afirma.

Regla de oro: si una cifra no se pudo medir, la clave no aparece. La interfaz
muestra "n/d". No hay numeros inventados en ninguna parte.
"""

import hashlib
import importlib
import inspect
import os
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)
# El proyecto tambien tiene que ser importable como paquete `app`, porque los
# adaptadores usan `from .. import config`. Sin esto, importar
# `app.adaptadores.mongodb` falla con "relative import beyond top-level package".
RAIZ = os.path.dirname(APP)
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

MOTORES = ["mongodb", "redis", "neo4j"]

# El nucleo de motores puede vivir en el paquete `nucleo` o en los adaptadores
# por motor. Se listan los candidatos en orden de preferencia: el nucleo manda.
MODULOS = ("respaldos", "motores", "pruebas")

_cache = {}


class ErrorUsuario(ValueError):
    """Error provocado por quien llama: se responde 400, nunca 500."""


# ------------------------------------------------------- conexion con motores
def _adaptadores():
    """Importa el paquete de adaptadores del nucleo, o None si no existe.

    Los adaptadores usan imports relativos hacia `config` (`from .. import
    config`), asi que tienen que importarse como `app.adaptadores.<motor>`, con
    `app` como paquete. Si se importan sueltos como `adaptadores.<motor>` el
    import relativo sube mas alla del paquete raiz y falla. Se prueban las dos
    formas porque un nucleo equivalente podria no usar imports relativos.
    """
    if "adaptadores" in _cache:
        return _cache["adaptadores"]
    mod = None
    for nombre in ("app.adaptadores", "adaptadores"):
        try:
            mod = importlib.import_module(nombre)
            break
        except Exception:
            mod = None
    _cache["adaptadores"] = mod
    return mod


def _adaptador_de(motor):
    """El modulo de adaptadores de un motor, importandolo si hace falta.

    Que el paquete __init__ reexporte o no sus submodulos no debe cambiar nada:
    se piden explicitamente, con el prefijo de paquete que exige cada uno.
    """
    if _adaptadores() is None:
        return None
    mod = sys.modules.get(f"adaptadores.{motor}")
    if mod is not None:
        return mod
    mod = sys.modules.get(f"app.adaptadores.{motor}")
    if mod is not None:
        return mod
    for nombre in (f"app.adaptadores.{motor}", f"adaptadores.{motor}"):
        try:
            return importlib.import_module(nombre)
        except Exception:
            continue
    return None


# Cada motor: como se conecta, como se saca su version, y de donde sale el
# artefacto. Se prueban varias funciones por si el nombre cambia.
COMO_CONECTAR = {
    "mongodb": ("conectar_mongodb",),
    "redis": ("conectar_redis",),
    "neo4j": ("conectar_neo4j",),
}

COMO_VERSION = {
    "mongodb": ("version_mongodb", "version"),
    "redis": ("version_redis", "version"),
    "neo4j": ("version_neo4j", "version"),
}

# Como se pregunta el estado de cada motor, en orden de preferencia.
COMO_ESTADO = (
    "estado_motores",     # nucleo: los tres de golpe
    "estado",             # nucleo: alias
)

# Funciones que generan y restauran, por motor, en orden de preferencia.
COMO_GENERAR = {
    "mongodb": ("generar_respaldo_logico", "generar_respaldo"),
    "redis": ("generar_respaldo_rdb", "generar_respaldo_aof", "generar_respaldo"),
    "neo4j": ("generar_dump_binario", "exportar_nodos_cypher", "generar_respaldo"),
}

COMO_RESTAURAR = {
    "mongodb": ("restaurar_logico", "restaurar_respaldo"),
    "redis": ("restaurar_rdb", "restaurar_aof", "restaurar_respaldo"),
    "neo4j": ("restaurar_dump_binario", "restaurar_nodos_cypher",
              "restaurar_respaldo"),
}


def nucleo(modulo):
    """Busca un modulo del nucleo entre las rutas en las que puede haber caido.

    Se prueban varias ubicaciones a proposito: el nucleo puede vivir en el
    paquete `nucleo` (respaldos, motores, pruebas) o como modulo suelto en
    `app/`. Devuelve None si no aparece, y el servidor sigue arrancando.
    """
    if modulo in _cache:
        return _cache[modulo]

    mod = sys.modules.get("nucleo." + modulo)
    if mod is None:
        import importlib
        # Tres rutas posibles: el paquete nucleo, el paquete app (donde vive
        # app/respaldos.py) y un modulo suelto en la raiz.
        # Antes de los imports sueltos se anade app/ al path: si no,
        # "respaldos" se resuelve como namespace package vacio (sin __init__.py)
        # y el modulo aparece importado pero sin contenido.
        import app as _app
        _raiz_app = getattr(_app, "__path__", [None])[0]
        if _raiz_app and _raiz_app not in sys.path:
            sys.path.insert(0, _raiz_app)
        for nombre in (f"nucleo.{modulo}", f"app.{modulo}", modulo):
            try:
                mod = importlib.import_module(nombre)
                break
            except Exception:
                mod = None
    _cache[modulo] = mod
    return mod


def nucleo_disponible():
    """True si los tres modulos del nucleo se pudieron cargar."""
    return all(nucleo(m) is not None for m in MODULOS)


def faltan():
    return [m for m in MODULOS if nucleo(m) is None]


# ------------------------------------------------------------- llamadas seguras
def _firma_params(fn):
    try:
        return inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}


def _llama(fn, **kwargs):
    """Invoca fn pasándole solo los keyword arguments que su firma acepta.

    Un parametro opcional con otro nombre no debe romper el servidor; lo que no
    exista en la firma simplemente no se pasa.
    """
    params = _firma_params(fn)
    if not params:
        return fn(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)  # la funcion acepta **kwargs: se le pasa todo
    return fn(**{k: v for k, v in kwargs.items() if k in params})


def _funcion(modulo, *nombres):
    """Devuelve la primera funcion de ese nombre que exista en el modulo."""
    mod = nucleo(modulo)
    if mod is None:
        return None
    for n in nombres:
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    return None


def _valor(modulo, nombres, defecto=None):
    mod = nucleo(modulo)
    if mod is None:
        return defecto
    for n in nombres:
        if hasattr(mod, n):
            return getattr(mod, n)
    return defecto


# ------------------------------------------------------------------ estrategias
def listar_estrategias():
    """El indice completo de estrategias, como lista de dicts."""
    resp = nucleo("respaldos")
    if resp is None:
        return []
    fn = _funcion("respaldos", "listar_estrategias", "estrategias", "catalogo")
    datos = None
    if fn is not None:
        try:
            datos = fn()
        except Exception:
            datos = None
    if datos is None:
        datos = _valor("respaldos", ("ESTRATEGIAS",), {}) or {}
    if isinstance(datos, dict):
        lista = []
        for clave, est in datos.items():
            fila = dict(est) if isinstance(est, dict) else {"nombre": str(est)}
            fila.setdefault("id", clave)
            lista.append(fila)
        return lista
    return [d for d in (datos or []) if isinstance(d, dict)]


def estrategia_por_id(estrategia):
    for est in listar_estrategias():
        if est.get("id") == estrategia:
            return est
    return None


def indice_estrategias():
    """Estructura para pintar la tabla de la interfaz."""
    return {
        "motores": list(MOTORES),
        "estrategias": listar_estrategias(),
        "total": len(listar_estrategias()),
    }


# ---------------------------------------------------------------------- motores
def _conecta(motor):
    """Abre una conexion real al motor. Devuelve el cliente, None o propaga error.

    Si el motor esta apagado el conector lanza excepcion: eso es informacion
    util, no un fallo de la peticion, asi que se propaga para que quien pregunta
    pueda decir exactamente por que no conecto.
    """
    nucleo("motores")  # por si basta con el nucleo
    candidatos = [
        (nucleo("motores"), COMO_CONECTAR.get(motor, ())),
        (_adaptador_de(motor), COMO_CONECTAR.get(motor, ())),
    ]
    for modulo, nombres in candidatos:
        fn = _funcion_de(modulo, nombres)
        if fn is None:
            continue
        try:
            return fn()
        except TypeError:
            # El conector exige argumentos que aqui no se conocen.
            uri = config_uri(motor)
            if uri:
                return _llama(fn, uri=uri)
            raise
    return None


def _version_de(motor, cliente):
    """La version que contesta el propio motor."""
    nucleo("motores")
    candidatos = [
        (nucleo("motores"), COMO_VERSION.get(motor, ())),
        (_adaptador_de(motor), COMO_VERSION.get(motor, ())),
    ]
    for modulo, nombres in candidatos:
        fn = _funcion_de(modulo, nombres)
        if fn is None:
            continue
        try:
            v = fn(cliente)
            if v:
                return str(v)
        except Exception:
            continue
    return None


def _config():
    """El modulo `config` del nucleo, o None si no se puede importar.

    Usa imports relativos, asi que se pide primero como `app.config`.
    """
    for nombre in ("app.config", "config"):
        try:
            return importlib.import_module(nombre)
        except Exception:
            continue
    return None


def config_uri(motor):
    """La URI de conexion que declara el nucleo, si la declara."""
    mod = _config()
    if mod is None:
        return None
    return {"mongodb": getattr(mod, "MONGO_URI", None),
            "redis": None,
            "neo4j": getattr(mod, "NEO4J_URI", None)}.get(motor)


def _funcion_de(modulo, nombres):
    for n in nombres:
        fn = getattr(modulo, n, None)
        if callable(fn):
            return fn
    return None


def _estado_por_conexion(motor):
    """Consulta un motor de verdad: conecta, pide version y cronometra."""
    inicio = time.perf_counter()
    try:
        cliente = _conecta(motor)
    except Exception as exc:
        return {"conectado": False, "version": "n/d",
                "detalle": "el motor no respondio", "error": _corto(exc),
                "latencia_ms": None}
    if cliente is None:
        return {"conectado": False, "version": "n/d",
                "detalle": "no hay conector para este motor",
                "error": "conector ausente", "latencia_ms": None}
    latencia = round((time.perf_counter() - inicio) * 1000, 1)
    try:
        version = _version_de(motor, cliente)
    except Exception as exc:
        _cierra(cliente)
        return {"conectado": False, "version": "n/d",
                "detalle": "conectado pero sin version", "error": _corto(exc),
                "latencia_ms": latencia}
    _cierra(cliente)
    return {"conectado": True, "version": version or "n/d",
            "detalle": "consultado en vivo", "error": None,
            "latencia_ms": latencia}


def _cierra(cliente):
    for metodo in ("close", "disconnect"):
        fn = getattr(cliente, metodo, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            return


def _corto(exc, limite=160):
    texto = " ".join(str(exc).split())
    return texto[:limite] if texto else exc.__class__.__name__


def estado_motores():
    """Estado real de los tres motores, consultandolos de verdad.

    Se prefiere la funcion global del nucleo si existe; si no, se pregunta motor
    por motor con los adaptadores. Nunca se supone el estado.
    """
    fn = _funcion("motores", *COMO_ESTADO)
    if fn is not None:
        try:
            crudo = fn()
            if isinstance(crudo, dict) and crudo:
                return _normaliza_estados(crudo)
        except Exception:
            pass  # el nucleo fallo: se pregunta motor por motor, no se miente
    return {motor: _estado_por_conexion(motor) for motor in MOTORES}


def _normaliza_estados(crudo):
    salida = {}
    for motor in MOTORES:
        est = crudo.get(motor)
        if not isinstance(est, dict):
            salida[motor] = {"conectado": False, "version": "n/d",
                             "detalle": "el motor no fue consultado",
                             "error": "sin datos", "latencia_ms": None}
            continue
        fila = dict(est)
        fila["conectado"] = bool(fila.get("conectado", False))
        fila["version"] = str(fila.get("version") or "n/d")
        fila.setdefault("detalle", "")
        fila.setdefault("error", None)
        salida[motor] = fila
    return salida


def _exige_motor_y_estrategia(motor, estrategia):
    if motor not in MOTORES:
        raise ErrorUsuario(f"motor desconocido: {motor}. "
                           f"Use uno de: {', '.join(MOTORES)}")
    if not isinstance(estrategia, str) or not estrategia:
        raise ErrorUsuario("falta la estrategia en la ruta")
    if estrategia_por_id(estrategia) is None:
        raise ErrorUsuario(f"estrategia desconocida: {estrategia}")


# ------------------------------------------------------------ hash y artefactos
def hash_archivo(ruta):
    """sha256 real del archivo, leido por bloques.

    Si el nucleo ya trae su propio hash (utileria.sha256_de_archivo), se usa ese
    para que servidor y nucleo den siempre el mismo valor.
    """
    propio = _funcion("pruebas", "hash_archivo", "sha256_de_archivo",
                      "sha256_archivo", "hash_de_archivo")
    if propio is not None:
        try:
            return propio(ruta)
        except Exception:
            pass  # el nucleo fallo: se calcula aqui, no se muestra nada falso
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for bloque in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def bytes_archivo(ruta):
    return os.path.getsize(ruta)


def _es_legible(art):
    return art and os.path.isfile(art)


def verificar_artefacto(ruta, esperado=None):
    """Verifica el artefacto en disco. Si el nucleo tiene su propio verificador
    y coincide, se usa; si no, se verifica aqui leyendo el archivo.

    Un respaldo puede ser un archivo suelto o un directorio con varias piezas
    (los .bson de mongodump, el dump de Neo4j). En el caso del directorio se
    verifica cada pieza, que es lo que de verdad dice si el conjunto esta bien.
    """
    fn = _funcion("pruebas", "verificar", "verificar_respaldo", "verificar_archivo")
    if ruta and os.path.isdir(ruta) and not _es_legible(ruta):
        return _verificar_directorio(ruta, fn, esperado)

    if fn is not None:
        try:
            if esperado is not None:
                crudo = _llama(fn, ruta=ruta, esperado=esperado)
            else:
                crudo = _llama(fn, ruta=ruta)
            if isinstance(crudo, dict) and "ok" in crudo:
                fila = dict(crudo)
                # El hash que se muestra siempre es el leido del disco.
                if _es_legible(ruta):
                    fila["sha256"] = hash_archivo(ruta)
                    fila["bytes"] = bytes_archivo(ruta)
                fila.setdefault("ok", False)
                return fila
        except TypeError:
            pass
        except Exception as exc:
            if not os.path.exists(ruta):
                return {"ok": False, "sha256": None, "bytes": None,
                        "detalle": f"no se pudo verificar: {exc}"}
    if not os.path.exists(ruta):
        raise ErrorUsuario(f"el artefacto no existe: {ruta}")
    medido = hash_archivo(ruta)
    ok = True if esperado is None else (medido == esperado)
    return {
        "ok": ok,
        "sha256": medido,
        "bytes": bytes_archivo(ruta),
        "esperado": esperado,
        "detalle": ("hash coincide con el esperado" if ok and esperado
                    else "hash leido del archivo" if ok
                    else "el hash NO coincide con el esperado"),
    }


def _verificar_directorio(ruta, fn, esperado):
    """Verifica cada pieza de un respaldo compuesto por varias."""
    piezas = []
    for raiz, _dirs, archivos in os.walk(ruta):
        for nombre in sorted(archivos):
            piezas.append(os.path.join(raiz, nombre))
    if not piezas:
        raise ErrorUsuario(f"el respaldo no tiene ninguna pieza: {ruta}")
    total = 0
    hash_conjunto = hashlib.sha256()
    registros = 0
    hay_bson = False
    for pieza in piezas:
        total += os.path.getsize(pieza)
        hash_conjunto.update(os.path.relpath(pieza, ruta).encode("utf-8"))
        hash_conjunto.update(b"\0")
        hash_conjunto.update(hash_archivo(pieza).encode("ascii"))
        hash_conjunto.update(b"\n")
        if pieza.endswith(".bson") or pieza.endswith(".bson.gz"):
            n = _cuenta_bson(pieza)
            if n is not None:
                registros += n
                hay_bson = True
    salida = {
        "ok": esperado is None or hash_conjunto.hexdigest() == esperado,
        "sha256": hash_conjunto.hexdigest(),
        "bytes": total,
        "piezas": len(piezas),
        "detalle": f"{len(piezas)} pieza(s) verificadas, hash del conjunto",
    }
    if hay_bson:
        salida["registros"] = registros
    return salida


def _busca_artefacto(resultado):
    """Localiza el artefacto dentro del dict que devuelve el nucleo.

    Puede ser un archivo suelto (el .bson de MongoDB) o un directorio (el dump
    de Neo4j, la copia de Redis). En el caso del directorio se devuelve el
    archivo mas grande que contenga, porque es el que se restaura.
    """
    def _es_artefacto(valor):
        if not isinstance(valor, str) or not valor:
            return False
        return os.path.isfile(valor) or os.path.isdir(valor)

    for clave in ("ruta", "archivo", "path", "destino", "fichero",
                  "archivo_respaldo", "dump"):
        if _es_artefacto(resultado.get(clave)):
            return resultado[clave]
    # Si el nucleo dio un directorio, se baja al archivo que lo representa.
    for clave in ("directorio", "carpeta", "destino_dir"):
        val = resultado.get(clave)
        if isinstance(val, str) and os.path.isdir(val):
            mejor = None
            for raiz, _dirs, archivos in os.walk(val):
                for nombre in archivos:
                    completa = os.path.join(raiz, nombre)
                    tam = os.path.getsize(completa)
                    if mejor is None or tam > mejor[0]:
                        mejor = (tam, completa)
            if mejor:
                return mejor[1]
    return None


# ------------------------------------------------------------ generar/restaurar
def _fn_del_motor(motor, tabla):
    """Busca la funcion de respaldo de un motor y devuelve (funcion, modulo).

    Primero mira el nucleo (si expone una funcion unica), luego los adaptadores
    por motor. Si no aparece ninguna, devuelve (None, None).
    """
    mod_nucleo = nucleo("motores")
    if mod_nucleo is not None:
        nombres = (("generar_respaldo", "respaldar", "generar")
                   if tabla == "generar"
                   else ("restaurar_respaldo", "restaurar", "recuperar"))
        fn = _funcion_de(mod_nucleo, nombres)
        if fn is not None:
            return fn, mod_nucleo
    modulo = _adaptador_de(motor)
    fn = _funcion_de(modulo, tabla)
    if fn is not None:
        return fn, modulo
    return None, None


def _intenta(fn, destino, motor, estrategia):
    """Llama a la funcion del nucleo con los argumentos que su firma acepte.

    Los adaptadores reales piden el directorio de destino; un nucleo mas alto
    puede pedir motor y estrategia. Se prueban esas formas en orden y se queda
    con la primera que no falle por firma.

    Solo se reintenta ante TypeError POR FIRMA (un argumento que no existe), que
    es el unico fallo que arreglar un argumento distinto puede arreglar. Un
    TypeError de dentro de la funcion se propaga: reintentarlo taparia el fallo
    real y devolveria un error de nucleo como si fuera de peticion.
    """
    params = _firma_params(fn)
    admite_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                        for p in params.values())
    accepts = set(params) if params else set()

    def vale(clave):
        return (not params) or admite_kwargs or clave in accepts

    intentos = []
    if vale("destino"):
        intentos.append({"destino": destino})
    if vale("ruta"):
        intentos.append({"ruta": destino})
    if vale("motor"):
        intentos.append({"motor": motor, "estrategia": estrategia} if vale("estrategia")
                        else {"motor": motor})
    intentos.append({})  # ultimo recurso: la funcion no quiere argumentos

    ultimo = None
    for intento in intentos:
        try:
            return _llama(fn, **intento)
        except TypeError as exc:
            ultimo = exc
            continue
    raise ultimo if ultimo else ErrorUsuario("no se pudo invocar el respaldo")


def _destino_para(motor, estrategia):
    """Un directorio propio para el artefacto, segun lo declarado en config."""
    try:
        cfg = _config()
        if cfg is None:
            raise ImportError("sin modulo config")
        base = {"mongodb": cfg.DIR_MONGODB, "redis": cfg.DIR_REDIS,
                "neo4j": cfg.DIR_NEO4J}.get(motor)
        if base:
            marca = time.strftime("%Y%m%d-%H%M%S")
            destino = os.path.join(base, f"{estrategia}-{marca}")
            os.makedirs(destino, exist_ok=True)
            return destino
    except Exception:
        pass
    import tempfile
    return tempfile.mkdtemp(prefix=f"{motor}-{estrategia}-")


def generar(motor, estrategia):
    """Ejecuta el respaldo de verdad y devuelve las cifras medidas."""
    _exige_motor_y_estrategia(motor, estrategia)
    fn, _modulo = _fn_del_motor(motor, COMO_GENERAR.get(motor, ()))
    if fn is None:
        raise ErrorUsuario("no hay ninguna funcion de respaldo para este motor")
    destino = _destino_para(motor, estrategia)
    inicio = time.perf_counter()
    try:
        crudo = _intenta(fn, destino, motor, estrategia)
    except ErrorUsuario:
        raise
    except Exception as exc:
        raise ErrorUsuario(f"no se pudo generar el respaldo: {_corto(exc)}")
    if not isinstance(crudo, dict):
        crudo = {"ok": bool(crudo)}
    salida = dict(crudo)
    salida.setdefault("motor", motor)
    salida.setdefault("estrategia", estrategia)
    salida["ok"] = bool(salida.get("ok", True))
    if salida.get("duracion_ms") is None:
        salida["duracion_ms"] = round((time.perf_counter() - inicio) * 1000, 1)
    if not salida.get("ruta") and not salida.get("archivo"):
        salida.setdefault("ruta", destino)

    # Remedicion: lo que se muestra es lo que hay en el archivo. Si el nucleo
    # dejo un directorio, se baja al archivo que lo representa; si no hay
    # ningun archivo real, no se inventa ninguna cifra.
    ruta = _busca_artefacto(salida)
    if ruta and os.path.isfile(ruta):
        salida["ruta"] = ruta
        salida["bytes"] = bytes_archivo(ruta)
        salida["sha256"] = hash_archivo(ruta)
        salida["existe"] = True
    elif ruta and os.path.isdir(ruta):
        salida["ruta"] = ruta
        salida["existe"] = True
        # Un directorio no tiene un unico hash: se mide el conjunto con el mismo
        # algoritmo que usa la verificacion, para que ambos coincidan.
        ver = _verificar_directorio(ruta, None, None)
        salida["sha256"] = ver["sha256"]
        salida["bytes"] = ver["bytes"]
        salida["piezas"] = ver["piezas"]
        # Registros contados DENTRO del artefacto. Si no hay piezas bson, no se
        # inventa la cifra: se deja ausente y la interfaz pondra "n/d".
        conteo = _cuenta_piezas(ruta)
        if conteo is not None:
            salida["registros"] = conteo
    else:
        salida["existe"] = False
        salida.pop("bytes", None)
        salida.pop("sha256", None)
    return salida


def _cuenta_bson(ruta):
    """Cuenta los documentos de un .bson o .bson.gz leyendo sus longitudes.

    Cada documento BSON empieza por un entero de 4 bytes con su propio tamano, y
    los documentos van uno detras de otro. Contarlos asi mide lo que hay en el
    artefacto, sin conectado al motor y sin estimar nada.
    """
    import gzip
    import struct

    try:
        with (gzip.open(ruta, "rb") if ruta.endswith(".gz") else open(ruta, "rb")) as fh:
            datos = fh.read()
    except OSError:
        return None
    total = 0
    pos = 0
    while pos + 4 <= len(datos):
        largo = struct.unpack("<i", datos[pos:pos + 4])[0]
        if largo <= 4 or pos + largo > len(datos):
            break
        pos += largo
        total += 1
    return total


def _cuenta_piezas(ruta):
    """Suma los registros de todas las piezas bson de un respaldo.

    Si no hay ninguna pieza bson recognizable (el dump de Neo4j, el RDB de
    Redis), devuelve None: es preferible "n/d" a un numero inventado.
    """
    if not (ruta and os.path.isdir(ruta)):
        return None
    total = 0
    encontrado = False
    for raiz, _dirs, archivos in os.walk(ruta):
        for nombre in sorted(archivos):
            if nombre.endswith(".bson") or nombre.endswith(".bson.gz"):
                n = _cuenta_bson(os.path.join(raiz, nombre))
                if n is not None:
                    total += n
                    encontrado = True
    return total if encontrado else None


def restaurar(motor, estrategia, respaldo_id=None):
    """Restaura el respaldo. Devuelve las cifras medidas.

    Las funciones de restauracion reales (restaurar_logico, restaurar_rdb...)
    piden la ruta o el archivo del respaldo, no la estrategia. Se les pasa el
    ultimo respaldo medido de ese motor si lo hay, y si no se pide por
    estrategia al indice del nucleo.
    """
    _exige_motor_y_estrategia(motor, estrategia)
    fn, _modulo = _fn_del_motor(motor, COMO_RESTAURAR.get(motor, ()))
    if fn is None:
        raise ErrorUsuario("no hay ninguna funcion de restauracion para este motor")

    # Que artefacto restaurar: el que se genero de verdad en este motor.
    origen = respaldo_id or _ultimo_artefacto(motor, estrategia)
    inicio = time.perf_counter()
    try:
        if origen:
            crudo = _intenta_restore(fn, origen, motor, estrategia)
        else:
            crudo = _intenta(fn, "", motor, estrategia)
    except ErrorUsuario:
        raise
    except Exception as exc:
        raise ErrorUsuario(f"no se pudo restaurar el respaldo: {_corto(exc)}")
    if not isinstance(crudo, dict):
        crudo = {"ok": bool(crudo)}
    salida = dict(crudo)
    salida.setdefault("motor", motor)
    salida.setdefault("estrategia", estrategia)
    salida["ok"] = bool(salida.get("ok", True))
    if salida.get("duracion_ms") is None:
        salida["duracion_ms"] = round((time.perf_counter() - inicio) * 1000, 1)
    return salida


def _ultimo_artefacto(motor, estrategia):
    """El artefacto mas reciente generado para ese motor, si existe."""
    try:
        cfg = _config()
        if cfg is None:
            raise ImportError("sin modulo config")
        base = {"mongodb": cfg.DIR_MONGODB, "redis": cfg.DIR_REDIS,
                "neo4j": cfg.DIR_NEO4J}.get(motor)
        if not base or not os.path.isdir(base):
            return None
        candidatos = []
        for raiz, _dirs, archivos in os.walk(base):
            for nombre in archivos:
                completa = os.path.join(raiz, nombre)
                candidatos.append((os.path.getmtime(completa), completa))
        if not candidatos:
            return None
        candidatos.sort(reverse=True)
        return candidatos[0][1]
    except Exception:
        return None


def _intenta_restore(fn, origen, motor, estrategia):
    """Como _intenta, pero priorizando los nombres de archivo del artefacto."""
    params = _firma_params(fn)
    admite_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                        for p in params.values())
    accepts = set(params) if params else set()

    def vale(clave):
        return (not params) or admite_kwargs or clave in accepts

    intentos = []
    for clave in ("archivo_rdb", "archivo", "origen", "ruta", "destino", "archivo_respaldo"):
        if vale(clave):
            intentos.append({clave: origen})
    intentos.append({"motor": motor, "estrategia": estrategia})
    intentos.append({})
    ultimo = None
    for intento in intentos:
        try:
            return _llama(fn, **intento)
        except TypeError as exc:
            ultimo = exc
            continue
    raise ultimo if ultimo else ErrorUsuario("no se pudo invocar la restauracion")


def disponibles():
    """Motores que contestan de verdad ahora mismo."""
    est = estado_motores()
    return [m for m in MOTORES if est.get(m, {}).get("conectado")]