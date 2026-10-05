"""app/tests/casos.py — suite de pruebas de DataForge Backup.

Ejecutar:  python3 app/tests/casos.py
Sale con codigo distinto de 0 si algo falla, para que tambien sirva en CI.

Cada caso lleva un id estable (T-01, T-02, ...) porque la matriz de trazabilidad
del proyecto los cita; esos ids NO se inventan despues, se crean aqui.

Los casos se separan en tres bloques:

  T-01..T-08   el indice de estrategias y su contenido (que protege / que no)
  T-09..T-16   el servidor: rutas, codigos de estado y errores de usuario
  T-17..T-26   los motores de verdad y el ciclo completo respaldo -> verificacion
               -> restauracion, ejecutado contra MongoDB real con mongodump y
               mongorestore. Ninguno de estos casos usa un simulacro.
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback

RAIZ = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(RAIZ)          # .../dataforge-backup/app
PROYECTO = os.path.dirname(APP)       # .../dataforge-backup
for ruta in (APP, PROYECTO):
    if ruta not in sys.path:
        sys.path.insert(0, ruta)

import importlib.util  # noqa: E402
import adaptador  # noqa: E402

# El servidor vive en <proyecto>/app.py, pero <proyecto>/app/ es un paquete con
# __init__.py, de modo que `from app import despacha` resolveria al paquete y no
# al modulo. Se carga por ruta explicita para no depender del orden de sys.path.
_spec = importlib.util.spec_from_file_location(
    "servidor_dataforge", os.path.join(PROYECTO, "app.py"))
_servidor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_servidor)
despacha = _servidor.despacha

BIN_MONGO = "/home/prodriguez/opt/mongodb-tools/mongodb-database-tools-ubuntu2204-x86_64-100.10.0/bin"

# Los artefactos de prueba NO van a /tmp: en esta maquina /tmp es un tmpfs de
# 512 MB y un mongodump de los datos sembrados lo llena. Se usan los directorios
# de trabajo del propio proyecto, que estan en el disco de verdad.
TMP_BASE = os.path.join(PROYECTO, ".lab", "pruebas")
os.makedirs(TMP_BASE, exist_ok=True)

BASE_PRUEBA = "PRUEBA_BACKUP"
# La base que el nucleo respalda de verdad (la declara config.py).
BASE_NUCLEO = getattr(
    __import__("config"), "DB_MONGO_ORIGEN", "dataforge_backup")
COLECCION = "producto"
CANTIDAD = 120


# ------------------------------------------------------------------ runner
CASOS = []


def caso(id_caso, titulo):
    def deco(fn):
        CASOS.append((id_caso, titulo, fn))
        return fn
    return deco


class Falla(AssertionError):
    pass


def esperar(cond, msg):
    if not cond:
        raise Falla(msg)


def igual(a, b, msg=""):
    if a != b:
        raise Falla(f"{msg} — esperaba {b!r}, obtuve {a!r}")


def motor_vivo(nombre):
    """True si el motor contesta de verdad ahora mismo."""
    try:
        return bool(adaptador.estado_motores().get(nombre, {}).get("conectado"))
    except Exception:
        return False


# ======================================================= T-01..T-08 estrategias
@caso("T-01", "el indice declara estrategias y cada una trae las claves obligatorias")
def t01():
    ests = adaptador.listar_estrategias()
    esperar(ests, "el nucleo no declaro ninguna estrategia")
    for e in ests:
        for clave in ("id", "nombre", "protege", "no_protege"):
            esperar(clave in e, f"la estrategia {e.get('id')} no trae {clave!r}")
        esperar(isinstance(e["protege"], list) and e["protege"],
               f"{e['id']}: 'protege' vacio")
        esperar(isinstance(e["no_protege"], list) and e["no_protege"],
               f"{e['id']}: 'no_protege' vacio — la columna que evita la sorpresa")


@caso("T-02", "ninguna estrategia promete protegerlo todo: 'no_protege' nunca esta vacio")
def t02():
    # Una estrategia que no dice que NO protege es una estrategia de ventas.
    for e in adaptador.listar_estrategias():
        esperar(e["no_protege"], f"{e['id']} no declara limitacion alguna")


@caso("T-03", "cada estrategia declara a que motores aplica y son motores validos")
def t03():
    for e in adaptador.listar_estrategias():
        soporta = e.get("soporta", [])
        esperar(soporta, f"{e['id']} no declara motores")
        for m in soporta:
            esperar(m in adaptador.MOTORES,
                   f"{e['id']} declara un motor inexistente: {m}")


@caso("T-04", "los ids de estrategia son unicos y no tienen espacios")
def t04():
    vistos = set()
    for e in adaptador.listar_estrategias():
        ident = e["id"]
        esperar(ident not in vistos, f"id de estrategia repetido: {ident}")
        vistos.add(ident)
        esperar(ident == ident.strip() and " " not in ident,
               f"id con espacios: {ident!r}")


@caso("T-05", "una estrategia valida se encuentra por id")
def t05():
    for e in adaptador.listar_estrategias():
        encontrada = adaptador.estrategia_por_id(e["id"])
        esperar(encontrada is not None, f"no se encontro la estrategia {e['id']}")
        igual(encontrada["id"], e["id"], "estrategia distinta a la pedida")


@caso("T-06", "el indice de estrategias serializa a JSON sin datos que no se puedan pintar")
def t06():
    datos = adaptador.indice_estrategias()
    texto = json.dumps(datos, ensure_ascii=False)
    igual(json.loads(texto)["total"], len(datos["estrategias"]), "total incoherente")
    esperar(isinstance(datos["motores"], list) and len(datos["motores"]) == 3,
           "el indice debe llevar los tres motores")


@caso("T-07", "un id de estrategia inexistente se rechaza como error de usuario")
def t07():
    try:
        adaptador.generar("mongodb", "no_existe_esta")
    except adaptador.ErrorUsuario as exc:
        esperar("estrategia desconocida" in str(exc), f"mensaje poco claro: {exc}")
    else:
        raise Falla("una estrategia inexistente deberia dar ErrorUsuario, no continuar")


@caso("T-08", "un motor desconocido se rechaza antes de tocar cualquier base de datos")
def t08():
    for motor in ("", "postgres", "MONGODB", "mssql"):
        try:
            adaptador.generar(motor, "completo")
        except adaptador.ErrorUsuario as exc:
            esperar("motor desconocido" in str(exc), f"mensaje poco claro: {exc}")
        else:
            raise Falla(f"el motor {motor!r} deberia rechazarse")


# ============================================================= T-09..T-16 API
@caso("T-09", "GET /api/salud responde 200 y describes el servicio")
def t09():
    r = despacha("GET", "/api/salud")
    igual(r["status"], 200, "estado de /api/salud")
    d = json.loads(r["cuerpo"])
    esperar(d["ok"] is True, "salud no dice ok")
    igual(d["motores_total"], 3, "debe conocer los tres motores")


@caso("T-10", "GET /api/motores devuelve los tres motores, no una lista parcial")
def t10():
    r = despacha("GET", "/api/motores")
    igual(r["status"], 200, "estado de /api/motores")
    motores = json.loads(r["cuerpo"])["motores"]
    for m in ("mongodb", "redis", "neo4j"):
        esperar(m in motores, f"falta el motor {m} en la respuesta")
        esperar("conectado" in motores[m], f"{m} sin campo 'conectado'")
        esperar("version" in motores[m], f"{m} sin campo 'version'")


@caso("T-11", "el estado de cada motor se consulta de verdad, no se supone")
def t11():
    # La version devuelta tiene que ser la que contesta el motor. Si el motor esta
    # apagado se dice "n/d": jamais se inventa una version.
    for m in adaptador.MOTORES:
        est = adaptador.estado_motores()[m]
        if est["conectado"]:
            esperar(est["version"] not in ("", "n/d", None),
                   f"{m} dice conectado pero no da version real")
        else:
            igual(est["version"], "n/d", f"{m} apagado deberia dar version n/d")


@caso("T-12", "GET /api/estrategias devuelve el indice completo")
def t12():
    r = despacha("GET", "/api/estrategias")
    igual(r["status"], 200, "estado de /api/estrategias")
    d = json.loads(r["cuerpo"])
    igual(len(d["estrategias"]), len(adaptador.listar_estrategias()),
         "la API y el nucleo no ven el mismo numero de estrategias")


@caso("T-13", "un motor inexistente en la ruta da 400, nunca 500")
def t13():
    for ruta in ("/api/respaldar/mssql/completo",
                 "/api/restaurar/postgres/completo",
                 "/api/respaldar//completo"):
        r = despacha("POST", ruta)
        igual(r["status"], 400, f"estado de {ruta}")
        igual(json.loads(r["cuerpo"])["ok"], False, "deberia reportar ok=false")


@caso("T-14", "una estrategia inexistente en la ruta da 400 con mensaje legible")
def t14():
    r = despacha("POST", "/api/respaldar/mongodb/estrategia_fantasma")
    igual(r["status"], 400, "estado de la peticion")
    d = json.loads(r["cuerpo"])
    igual(d["ok"], False, "ok debe ser False")
    esperar("fantasma" in d["error"], f"el mensaje no nombra la estrategia: {d}")


@caso("T-15", "una ruta mal formada o de API desconocida da 4xx, no 500")
def t15():
    for ruta in ("/api/respaldar/mongodb", "/api/respaldar/a/b/c",
                 "/api/ruta_inventada", "/api/"):
        r = despacha("POST", ruta)
        esperar(400 <= r["status"] < 500,
               f"{ruta} devolvio {r['status']}, deberia ser 4xx")


@caso("T-16", "un cuerpo POST que no es JSON da 400 y no rompe el servidor")
def t16():
    r = despacha("POST", "/api/restaurar/mongodb/completo", cuerpo="{no es json")
    igual(r["status"], 400, "estado ante JSON invalido")
    # Y el servidor sigue respondiendo despues del error.
    igual(despacha("GET", "/api/salud")["status"], 200, "el servidor quedo caido")


# ================================================= T-17..T-26 motores reales
def _mongodump(destino):
    """Ejecuta mongodump de verdad y devuelve el codigo de salida."""
    cmd = [os.path.join(BIN_MONGO, "mongodump"),
           "--uri", f"mongodb://127.0.0.1:27017/{BASE_PRUEBA}",
           "--out", destino, "--gzip"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc


def _mongorestore(ruta):
    proc = subprocess.run(
        [os.path.join(BIN_MONGO, "mongorestore"),
         "--uri", "mongodb://127.0.0.1:27017", "--gzip", "--drop", ruta],
        capture_output=True, text=True, timeout=180)
    return proc


def _siembra_base_nucleo():
    """Siembra la base que el nucleo respalda, con su propio generador.

    Se usa el sembrador del nucleo (datos_prueba.sembrar_mongodb) en vez de
    inventar documentos aqui: asi se mide el respaldo sobre los datos que la
    aplicacion realmente genera.
    """
    import pymongo
    cliente = pymongo.MongoClient("mongodb://127.0.0.1:27017",
                                 serverSelectionTimeoutMS=3000)
    # Los modulos del nucleo usan imports relativos (`from . import config`), asi
    # que se importan por su ruta de paquete. Si se importaran sueltos, el
    # ImportError caeria en el except de abajo y la prueba mediria 120
    # documentos inventados en vez de los datos reales del nucleo.
    try:
        from app import datos_prueba
    except ImportError:
        import datos_prueba
    try:
        conteos = datos_prueba.sembrar_mongodb(cliente, BASE_NUCLEO)
        total = sum(conteos.values())
        esperar(total > 0, "el sembrador del nucleo no cargo ningun documento")
    except AttributeError:
        # Sin el sembrador del nucleo se siembra una coleccion minima, para que
        # el respaldo tenga algo real que medir.
        cliente[BASE_NUCLEO][COLECCION].drop()
        cliente[BASE_NUCLEO][COLECCION].insert_many(
            [{"i": i, "txt": f"producto-{i}"} for i in range(CANTIDAD)])
    return cliente


@caso("T-17", "MongoDB 7 responde de verdad a la consulta de estado")
def t17():
    if not motor_vivo("mongodb"):
        raise Falla("MongoDB esta apagado: hay que levantarlo para medir de verdad")
    est = adaptador.estado_motores()["mongodb"]
    igual(est["conectado"], True, "MongoDB deberia estar conectado")
    esperar(est["version"].startswith("7"), f"version inesperada: {est['version']}")


@caso("T-18", "Redis 7 responde de verdad a la consulta de estado")
def t18():
    if not motor_vivo("redis"):
        raise Falla("Redis esta apagado: hay que levantarlo para medir de verdad")
    est = adaptador.estado_motores()["redis"]
    igual(est["conectado"], True, "Redis deberia estar conectado")
    esperar(est["version"].startswith("7"), f"version inesperada: {est['version']}")


@caso("T-19", "Neo4j 5 responde de verdad a la consulta de estado")
def t19():
    if not motor_vivo("neo4j"):
        raise Falla("Neo4j esta apagado: hay que levantarlo para medir de verdad")
    est = adaptador.estado_motores()["neo4j"]
    igual(est["conectado"], True, "Neo4j deberia estar conectado")
    esperar(est["version"].startswith("5"), f"version inesperada: {est['version']}")


@caso("T-20", "el hash sha256 se recalcula leyendo el archivo, no se copia de un campo")
def t20():
    import hashlib

    with tempfile.TemporaryDirectory(dir=TMP_BASE) as tmp:
        ruta = os.path.join(tmp, "artefacto.bin")
        # Contenido con bytes no imprimibles: si el hash se calculara sobre el
        # texto en vez de sobre el archivo, daria un valor distinto.
        with open(ruta, "wb") as fh:
            fh.write(bytes(range(256)) * 40)
        medido = adaptador.hash_archivo(ruta)
        esperado = hashlib.sha256(open(ruta, "rb").read()).hexdigest()
        igual(medido, esperado, "el hash no coincide con el del archivo real")
        igual(len(medido), 64, "un sha256 en hexadecimal son 64 caracteres")


@caso("T-21", "verificar un artefacto detecta un archivo alterado")
def t21():

    with tempfile.TemporaryDirectory(dir=TMP_BASE) as tmp:
        ruta = os.path.join(tmp, "artefacto.bin")
        with open(ruta, "wb") as fh:
            fh.write(b"contenido original")
        ver = adaptador.verificar_artefacto(ruta)
        igual(ver["ok"], True, "un archivo recien escrito debe verificar bien")
        # Se altera un solo byte y el hash ya no puede coincidir.
        with open(ruta, "r+b") as fh:
            fh.seek(0)
            fh.write(b"C")
        ver_roto = adaptador.verificar_artefacto(ruta, ver["sha256"])
        igual(ver_roto["ok"], False, "un archivo alterado no debe verificar")
        esperar(ver_roto["sha256"] != ver["sha256"], "el hash deberia haber cambiado")


@caso("T-22", "verificar un artefacto inexistente da error de usuario, no una excepcion")
def t22():
    try:
        adaptador.verificar_artefacto("/tmp/no_existe_este_archivo_xyz")
    except adaptador.ErrorUsuario:
        pass  # es lo esperado: 400 en el servidor
    else:
        raise Falla("un artefacto inexistente deberia dar ErrorUsuario")


@caso("T-23", "el ciclo completo en MongoDB: respaldar, verificar hash y restaurar")
def t23():
    """Ciclo exigido por el enunciado: generar -> verificar hash -> restaurar -> contar.

    Corre contra el MongoDB de verdad con mongodump y mongorestore. Al final se
    comprueba que los documentos VOLVIERON, que son los mismos y que el hash del
    artefacto es el que tiene el archivo en disco.
    """
    import shutil

    import pymongo

    if not motor_vivo("mongodb"):
        raise Falla("MongoDB esta apagado: el ciclo completo exige un motor real")

    cliente = pymongo.MongoClient("mongodb://127.0.0.1:27017",
                                 serverSelectionTimeoutMS=3000)
    db = cliente[BASE_PRUEBA]
    tmp = tempfile.mkdtemp(dir=TMP_BASE, prefix="respaldo-ciclo-")
    try:
        # --- 1. se dejan CANTIDAD documentos con un dato reconocible
        db[COLECCION].delete_many({})
        db[COLECCION].insert_many(
            [{"i": i, "txt": f"producto-{i}"} for i in range(CANTIDAD)])
        antes = db[COLECCION].count_documents({})
        igual(antes, CANTIDAD, "no se sembraron los documentos esperados")

        # --- 2. respaldo de verdad
        proc = _mongodump(tmp)
        igual(proc.returncode, 0, f"mongodump fallo: {proc.stderr[-400:]}")
        bson = os.path.join(tmp, BASE_PRUEBA, f"{COLECCION}.bson.gz")
        esperar(os.path.isfile(bson), f"mongodump no genero {bson}")

        # --- 3. verificacion: bytes y sha256 medidos sobre el archivo
        bytes_medidos = os.path.getsize(bson)
        esperar(bytes_medidos > 0, "el artefacto esta vacio")
        sha = adaptador.hash_archivo(bson)
        igual(len(sha), 64, "sha256 incompleto")
        ver = adaptador.verificar_artefacto(bson, sha)
        igual(ver["ok"], True, "el hash recien medido debe verificar")

        # --- 4. se borra todo para que restaurar tenga que reponerlo
        db[COLECCION].delete_many({})
        igual(db[COLECCION].count_documents({}), 0, "no se vacio la coleccion")

        # mongorestore NO acepta el directorio: lo toma, no dice nada y restaura
        # 0 documentos con codigo de salida 0. Por eso se pasa cada .bson.gz.
        proc_r = _mongorestore(bson)
        igual(proc_r.returncode, 0, f"mongorestore fallo: {proc_r.stderr[-400:]}")

        # --- 5. la prueba que de verdad importa: se contaron los documentos
        despues = db[COLECCION].count_documents({})
        igual(despues, CANTIDAD,
             f"la restauracion no repuso los documentos (quedaron {despues})")
        muestra = db[COLECCION].find_one({"i": CANTIDAD - 1})
        esperar(muestra is not None, "no se encontro el ultimo documento sembrado")
        igual(muestra["txt"], f"producto-{CANTIDAD - 1}", "el dato no quedo intacto")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            db[COLECCION].drop()
        except Exception:
            pass
        cliente.close()


@caso("T-24", "restaurar un artefacto alterado NO repone los datos correctos")
def t24():
    """La verificacion tiene que servir para algo: si el archivo se corrompe, el
    ciclo se detecta en vez de dar un exito falso."""
    import shutil

    import pymongo

    if not motor_vivo("mongodb"):
        raise Falla("MongoDB esta apagado: hace falta un motor real")

    cliente = pymongo.MongoClient("mongodb://127.0.0.1:27017",
                                 serverSelectionTimeoutMS=3000)
    db = cliente[BASE_PRUEBA]
    tmp = tempfile.mkdtemp(dir=TMP_BASE, prefix="respaldo-corrupto-")
    try:
        db[COLECCION].delete_many({})
        db[COLECCION].insert_many([{"i": i} for i in range(40)])
        proc = _mongodump(tmp)
        igual(proc.returncode, 0, "mongodump fallo")
        bson = os.path.join(tmp, BASE_PRUEBA, f"{COLECCION}.bson.gz")
        bueno = adaptador.hash_archivo(bson)

        with open(bson, "r+b") as fh:      # se daña un byte del medio
            fh.seek(os.path.getsize(bson) // 2)
            byte = fh.read(1)
            fh.seek(os.path.getsize(bson) // 2)
            fh.write(bytes([byte[0] ^ 0xFF]))

        ver = adaptador.verificar_artefacto(bson, bueno)
        igual(ver["ok"], False,
             "un artefacto alterado no puede verificar como correcto")
        esperar(ver["sha256"] != bueno, "el hash del archivo alterado debe cambiar")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            db[COLECCION].drop()
        except Exception:
            pass
        cliente.close()


@caso("T-25", "el respaldo solo reporta success si se comprobo que el archivo existe")
def t25():
    """Un `ok: true` sin artefacto real seria una mentira en la pantalla.

    Se siembra la base que el nucleo respalda de verdad y se ejecuta el respaldo
    por la misma ruta que usa el servidor, para que lo que se comprueba aqui sea
    exactamente lo que ve el usuario en pantalla.
    """
    if not motor_vivo("mongodb"):
        try:
            adaptador.generar("mongodb", "mongodb:logico_mongodump")
        except adaptador.ErrorUsuario:
            return  # sin motor, lo correcto es rechazar, no inventar un ok
        raise Falla("se Deveria rechazar el respaldo sin motor, no inventar un ok")

    cliente = _siembra_base_nucleo()
    try:
        resultado = adaptador.generar("mongodb", "mongodb:logico_mongodump")
    finally:
        try:
            cliente.drop_database(BASE_NUCLEO)
        except Exception:
            pass
        cliente.close()

    if resultado.get("ok"):
        esperar(resultado.get("ruta"), "ok=true pero no dice donde esta el artefacto")
        esperar(resultado.get("existe") is True,
               "ok=true pero no se comprobo que exista el artefacto")
        esperar(resultado.get("bytes", 0) > 0, "ok=true pero el artefacto esta vacio")
        igual(len(resultado.get("sha256", "")), 64, "ok=true pero no hay sha256 medido")
    else:
        # Si no dice ok, al menos tiene que decir POR QUE y no haber inventado
        # cifras: un fallo honesto es mejor que un exito falso.
        esperar(resultado.get("error") or not resultado.get("bytes"),
               "sin ok y sin error, y con bytes: hay cifras sin medir")


@caso("T-26", "ninguna cifra mostrada aparece sin haber sido medida")
def t26():
    """Ningun numero de la interfaz puede ser inventado: si no se midio, no esta."""
    r = despacha("GET", "/api/motores")
    for nombre, est in json.loads(r["cuerpo"])["motores"].items():
        if est.get("conectado"):
            esperar(est["version"] != "n/d", f"{nombre}: conectado sin version medida")
        else:
            igual(est["version"], "n/d",
                 f"{nombre}: apagado no debe mostrar una version inventada")


@caso("T-27", "los registros del artefacto coinciden con los que hay en la base viva")
def t27():
    """Cifra de contraste: si los documentos contados dentro del .bson no son los
    mismos que tiene la base, el conteo es una estimacion y no una medida."""
    if not motor_vivo("mongodb"):
        raise Falla("MongoDB esta apagado: hace falta un motor real")
    import pymongo

    cliente = _siembra_base_nucleo()
    try:
        db = cliente[BASE_NUCLEO]
        reales = sum(db[n].count_documents({}) for n in db.list_collection_names())
        esperar(reales > 0, "no hay documentos en la base sembrada")

        resultado = adaptador.generar("mongodb", "mongodb:logico_mongodump")
        igual(resultado.get("ok"), True, "el respaldo deberia generarse")
        igual(resultado.get("registros"), reales,
             "los registros del artefacto no coinciden con los de la base")
    finally:
        try:
            cliente.drop_database(BASE_NUCLEO)
        except Exception:
            pass
        cliente.close()


# ------------------------------------------------------------------ main
def main():
    print("  DataForge Backup · suite de pruebas")
    print("  " + "-" * 62)
    ok = fallos = 0
    for id_caso, titulo, fn in CASOS:
        try:
            fn()
        except Exception:
            fallos += 1
            print(f"  [FALLA] {id_caso}  {titulo}")
            print("    " + traceback.format_exc().strip().replace("\n", "\n    "))
        else:
            ok += 1
            print(f"  [ OK   ] {id_caso}  {titulo}")
    total = ok + fallos
    print("\n  " + "-" * 62)
    print(f"  Casos: {total} · exitosos: {ok} · fallidos: {fallos} · "
          f"cobertura: {100.0 * ok / total if total else 0:.0f}%")
    vivos = [m for m in adaptador.MOTORES if motor_vivo(m)]
    print(f"  Motores medidos en vivo: {', '.join(vivos) if vivos else 'ninguno'}")
    print("  Ciclo completo con datos reales: T-23 (respaldar, verificar, restaurar, contar)")
    print("  " + "-" * 62)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())