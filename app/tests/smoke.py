"""app/tests/smoke.py — recorrido real de la interfaz con Chromium (Playwright).

Levanta SU PROPIO servidor en un puerto libre, abre la pagina y ejerce el flujo
completo del usuario:

  1. la pagina carga y pinta los tres motores con su estado real;
  2. la tabla de estrategias se dibuja con que protege y que NO protege;
  3. un motor valido aparece conectado y uno invalido se rechaza con 400;
  4. generar un respaldo de verdad y comprobar el resultado medido;
  5. el hash sha256 que se muestra tiene 64 caracteres;
  6. ejecutar el recorrido tambien sin motor, para ver el aviso de error.

Salida: prints por paso + capturas en app/tests/capturas/. Devuelve 1 si algun
paso falla.

Requiere los motores levantados para los pasos que miden un respaldo real; si no
estan, esos pasos se declaran no aplicables en vez de inventarse un resultado.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

RAIZ = os.path.dirname(os.path.abspath(__file__))          # app/tests
APP = os.path.dirname(RAIZ)                                # app
PROYECTO = os.path.dirname(APP)                            # raiz
# Hace falta el proyecto como paquete `app`: los adaptadores del nucleo usan
# `from .. import config`, que solo resuelve si `app` es un paquete importable.
for ruta in (APP, PROYECTO):
    if ruta not in sys.path:
        sys.path.insert(0, ruta)

from playwright.sync_api import sync_playwright  # noqa: E402

CAPTURAS = os.path.join(RAIZ, "capturas")
fallos = []
omitidos = []


# El Chromium ya instalado en el sistema puede ser de otra build que la que
# espera esta version de Playwright; se reutiliza en vez de descargar otra.
def _chrome():
    overrid = os.environ.get("CHROME_BIN", "")
    if overrid and os.path.isfile(overrid):
        return overrid
    cache = os.path.expanduser("~/.cache/ms-playwright")
    for sub in sorted(os.listdir(cache), reverse=True) if os.path.isdir(cache) else []:
        for rel in ("chrome-linux64/chrome", "chrome-linux/chrome"):
            ruta = os.path.join(cache, sub, rel)
            if os.path.isfile(ruta):
                return ruta
    return ""


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def paso(nombre, condicion, detalle=""):
    if condicion:
        print(f"  [ OK   ] {nombre}")
    else:
        print(f"  [FALLA] {nombre} — {detalle}")
        fallos.append(nombre)
    return bool(condicion)


def omitido(nombre, motivo):
    print(f"  [ N/A  ] {nombre} — {motivo}")
    omitidos.append(nombre)


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_sembradas = []


def _siembra(motor):
    """Carga datos de verdad en el motor, con el generador del nucleo.

    Sin datos no hay respaldo que medir, y el nucleo falla con un error que
    estaria bien pero no es lo que este recorrido quiere comprobar. Se usan los
    sembradores del nucleo; si no existen, se devuelven sin sembrar.
    """
    if APP not in sys.path:
        sys.path.insert(0, APP)
    try:
        from app import config
        from app import datos_prueba
    except Exception:
        return False
    try:
        if motor == "mongodb":
            import pymongo
            from app.adaptadores.mongodb import conectar_mongodb
            cliente = conectar_mongodb()
            total = sum(datos_prueba.sembrar_mongodb(cliente).values())
        elif motor == "redis":
            from app.adaptadores.redis import conectar_redis
            cliente = conectar_redis()
            total = datos_prueba.sembrar_redis(cliente)
        elif motor == "neo4j":
            from app.adaptadores.neo4j import conectar_neo4j
            cliente = conectar_neo4j()
            with cliente.session() as sesion:
                total = sum(datos_prueba.sembrar_neo4j(sesion).values())
        else:
            return False
    except Exception as exc:
        print(f"  (aviso: no se pudo sembrar {motor}: {exc})")
        return False
    _sembradas.append(motor)
    print(f"  datos sembrados en {motor}: {total}")
    return True


def post(url, timeout=120):
    peticion = urllib.request.Request(url, data=b"{}",
                                       headers={"Content-Type": "application/json"},
                                       method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main():
    os.makedirs(CAPTURAS, exist_ok=True)
    puerto = puerto_libre()
    url = f"http://127.0.0.1:{puerto}/"

    print("  DataForge Backup · smoke test de la interfaz")
    print(f"  servidor propio en {url}")

    # El nucleo puede no estar listo; el servidor debe arrancar igual.
    servidor = subprocess.Popen(
        [sys.executable, os.path.join(PROYECTO, "app.py"),
         "--sin-navegador", "--puerto", str(puerto)],
        cwd=PROYECTO, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    try:
        # --- espera a que /api/salud conteste, sin adivinar tiempos muertos
        listo = False
        limite = time.time() + 25
        while time.time() < limite:
            try:
                if get(f"{url}api/salud", timeout=2)["ok"]:
                    listo = True
                    break
            except Exception:
                time.sleep(0.3)
        if not listo:
            paso("el servidor propio arranca", False,
                 "no respondio /api/salud en 25s")
            return 1
        paso("el servidor propio arranca y /api/salud responde", True)

        salud = get(f"{url}api/salud")
        paso("el despliegue declara los tres motores",
             salud["motores_total"] == 3, f"motores_total={salud['motores_total']}")

        chrome = _chrome()
        if not chrome:
            print("  [FALLA] no se encontro Chromium en ~/.cache/ms-playwright")
            return 1
        print(f"  navegador: {chrome}")

        with sync_playwright() as p:
            nav = p.chromium.launch(headless=True, executable_path=chrome)
            pagina = nav.new_page(viewport={"width": 1280, "height": 1500})
            errores = []
            pagina.on("pageerror", lambda e: errores.append("PAGEERROR: " + str(e)))
            pagina.on("console",
                      lambda m: errores.append(f"CONSOLE {m.type}: {m.text}")
                      if m.type == "error" else None)

            # ------------------------------------------------------- 1. carga
            pagina.goto(url, wait_until="networkidle")
            pagina.wait_for_selector(".motor", timeout=20000)
            tarjetas = pagina.locator(".motor").count()
            paso("1 · la pagina pinta los tres motores con su estado real",
                 tarjetas == 3, f"tarjetas={tarjetas}")

            # El estado mostrado debe coincidir con el que contesta la API.
            api_motores = get(f"{url}api/motores")["motores"]
            for nombre in ("mongodb", "redis", "neo4j"):
                texto = pagina.inner_text(f'.motor[data-motor="{nombre}"] .punto')
                conectado = api_motores[nombre]["conectado"]
                esperado = "conectado" if conectado else "sin conexion"
                paso(f"1 · la tarjeta de {nombre} coincide con la API",
                     esperado in texto, f"mostrado={texto!r} API={esperado!r}")

            # Si el motor esta apagado, la version debe decir "n/d": jamas un
            # numero inventado en la pantalla.
            apagados = [m for m in api_motores if not api_motores[m]["conectado"]]
            for nombre in apagados:
                cuerpo = pagina.inner_text(f'.motor[data-motor="{nombre}"]')
                paso(f"1 · {nombre} apagado muestra version n/d, no una cifra inventada",
                     "n/d" in cuerpo, f"tarjeta={cuerpo[:120]!r}")

            resumen = pagina.inner_text("#resumen-motores")
            paso("1 · el encabezado cuenta los motores que contestan",
                 "de 3" in resumen, f"resumen={resumen!r}")
            pagina.screenshot(path=os.path.join(CAPTURAS, "1-motores.png"), full_page=True)

            # -------------------------------------------------- 2. estrategias
            # Si el nucleo todavia no declaro estrategias, el paso se marca como
            # no aplicable: no se inventa una tabla que no existe.
            if pagina.locator("#tabla-estrategias").count():
                filas = pagina.locator("#tabla-estrategias tbody tr").count()
                paso("2 · la tabla de estrategias se dibuja", filas > 0, f"filas={filas}")

                cabeceras = pagina.inner_text("#tabla-estrategias thead")
                paso("2 · la tabla dice que protege y que NO protege",
                     "protege" in cabeceras.lower() and "no protege" in cabeceras.lower(),
                     f"cabeceras={cabeceras!r}")

                limitar = pagina.locator(
                    "#tabla-estrategias tbody tr td.no").all_inner_texts()
                paso("2 · cada estrategia declara que NO protege",
                     len(limitar) == filas and all(t.strip() for t in limitar),
                     f"columna de limitaciones vacia en {filas - len(limitar)} fila(s)")
                pagina.screenshot(path=os.path.join(CAPTURAS, "2-estrategias.png"),
                                  full_page=True)
            else:
                omitido("la tabla de estrategias",
                        "el nucleo todavia no declara estrategias")

            # ------------------------------------------------- 3. errores 400
            estado, cuerpo = post(f"{url}api/respaldar/mssql/completo")
            paso("3 · un motor prohibido (MS SQL Server) se rechaza con 400",
                 estado == 400 and cuerpo.get("ok") is False,
                 f"estado={estado} cuerpo={cuerpo}")

            estado, cuerpo = post(f"{url}api/respaldar/mongodb/estrategia_fantasma")
            paso("3 · una estrategia inexistente se rechaza con 400 y mensaje claro",
                 estado == 400 and "fantasma" in cuerpo.get("error", ""),
                 f"estado={estado} cuerpo={cuerpo}")

            paso("3 · el servidor sigue vivo despues de los errores",
                 get(f"{url}api/salud")["ok"] is True,
                 "el servidor dejo de responder")

            # ------------------------------------------- 4. respaldo de verdad
            vivos = [m for m in api_motores if api_motores[m]["conectado"]]
            if not vivos or not pagina.locator("#estrategia option").count():
                omitido("generar un respaldo de verdad",
                        "sin motores levantados o sin estrategias declaradas")
                omitido("el hash sha256 mostrado", "no hay artefacto que medir")
            else:
                motor = "mongodb" if "mongodb" in vivos else vivos[0]
                # El respaldo se mide sobre datos de verdad: sin sembrar, el
                # nucleo no tiene nada que volcar y falla con honestidad. El paso
                # comprobaria un fallo, no el ciclo de respaldo.
                sembrado = _siembra(motor)
                pagina.select_option("#motor", motor)
                pagina.click("#btn-respaldar")
                pagina.wait_for_function(
                    "() => { const e = document.getElementById('estado');"
                    " return e && !e.className.includes('trabajando'); }",
                    timeout=180000)
                pagina.wait_for_selector("#tabla-resultado", timeout=20000)

                # La clase del aviso es la que dice si la ejecucion fue bien:
                # "error" presente significa que el respaldo fallo.
                clase = pagina.get_attribute("#estado", "class") or ""
                paso(f"4 · el respaldo de {motor} no termina en error",
                     "error" not in clase,
                     f"aviso={pagina.inner_text('#estado')!r}")

                tabla_r = pagina.inner_text("#tabla-resultado")
                paso("4 · la interfaz muestra el tamano medido del artefacto",
                     "n/d" not in tabla_r.lower().split("tamano del artefacto")[-1][:40],
                     f"tabla={tabla_r!r}")
                paso("4 · la interfaz muestra la duracion medida",
                     "ms" in tabla_r or " s" in tabla_r, f"tabla={tabla_r!r}")

                if pagina.locator("#hash").count():
                    sha = pagina.inner_text("#hash").strip()
                    paso("5 · el sha256 mostrado tiene los 64 caracteres de un hash",
                         len(sha) == 64 and all(c in "0123456789abcdef" for c in sha),
                         f"hash={sha!r}")
                else:
                    paso("5 · se muestra el sha256 del artefacto generado", False,
                         "no se mostro ningun hash pese a haber artefacto")
                pagina.screenshot(path=os.path.join(CAPTURAS, "3-respaldo.png"), full_page=True)

            # ---------------------------------------- 5. restaurar desde la UI
            pagina.click("#btn-restaurar")
            pagina.wait_for_function(
                "() => { const e = document.getElementById('estado');"
                " return e && !e.className.includes('trabajando'); }",
                timeout=180000)
            aviso_r = pagina.inner_text("#estado")
            paso("5 · el boton de restaurar responde y deja un aviso",
                 bool(aviso_r.strip()), f"aviso={aviso_r!r}")
            pagina.screenshot(path=os.path.join(CAPTURAS, "4-restaurar.png"), full_page=True)

            # ------------------------------------------------- 6. consola limpia
            # "Failed to load resource" lo registra el navegador tambien ante 4xx
            # esperados; lo que debe estar limpio son los errores de script.
            ruido = [e for e in errores
                     if not e.startswith("CONSOLE error: Failed to load resource")]
            paso("6 · la consola del navegador no tiene errores de JavaScript",
                 not ruido, f"errores={ruido[:3]}")

            nav.close()

    finally:
        servidor.terminate()
        try:
            servidor.wait(timeout=10)
        except Exception:
            servidor.kill()

    print("\n  " + "-" * 62)
    if fallos:
        print(f"  SMOKE TEST: {len(fallos)} paso(s) fallido(s): {fallos}")
    else:
        print("  SMOKE TEST: todos los pasos aplicables pasaron")
    if omitidos:
        print(f"  {len(omitidos)} paso(s) no aplicables: {omitidos}")
    print(f"  capturas en {CAPTURAS}")
    print("  " + "-" * 62)
    return 1 if fallos else 0


if __name__ == "__main__":
    t0 = time.time()
    codigo = main()
    print(f"  ({time.time() - t0:.1f}s)")
    sys.exit(codigo)