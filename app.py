#!/usr/bin/env python3
"""DataForge Backup — servidor de la aplicacion (solo biblioteca estandar).

    python3 app.py --sin-navegador --puerto 8080
    python3 app.py --puerto 9000
    python3 app.py --sin-navegador --host 0.0.0.0

No requiere `pip install`: el servidor es `http.server`, la interfaz es una
pagina estatica y toda la logica de respaldos vive en `app/nucleo/`.

Rutas:

    GET  /api/salud                          estado del servicio
    GET  /api/motores                        estado real de los tres motores
    GET  /api/estrategias                    indice completo de estrategias
    POST /api/respaldar/<motor>/<estrategia> ejecuta el respaldo de verdad
    POST /api/restaurar/<motor>/<estrategia> restaura el respaldo

Todo error provocado por quien llama se responde con 400, nunca con 500: un
motor o una estrategia inexistentes son una peticion incorrecta, no una caida.
"""

import argparse
import json
import os
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.abspath(__file__))   # raiz del proyecto
APP = os.path.join(RAIZ, "app")                     # codigo de la aplicacion
if APP not in sys.path:
    sys.path.insert(0, APP)

from adaptador import (  # noqa: E402
    MOTORES, ErrorUsuario, disponibles, estado_motores, generar,
    indice_estrategias, listar_estrategias, restaurar, verificar_artefacto,
)

RAIZ_WEB = os.path.join(APP, "web")
INICIO = time.time()

TIPOS = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _json(datos, status=200):
    return {"status": status, "cuerpo": json.dumps(datos, ensure_ascii=False)}


def _error(mensaje, status=400):
    return _json({"ok": False, "error": mensaje}, status)


# ------------------------------------------------------------------- rutas API
def ruta_salud():
    """Estado del servicio. NO consulta los motores a proposito.

    Este es el health check de Render: se llama cada pocos segundos, asi que
    tiene que responder rapido y sin depender de que los motores esten
    encendidos. El estado real de cada motor vive en /api/motores, que si lo
    consulta. Aqui todo se lee de memoria: nada de sockets.
    """
    return _json({
        "ok": True,
        "servicio": "dataforge-backup",
        "version": "1.0",
        "motores_total": len(MOTORES),
        "estrategias": len(listar_estrategias()),
        "activo_desde": round(time.time() - INICIO, 1),
    })


def ruta_motores():
    mot = estado_motores()
    return _json({"ok": True, "motores": mot,
                  "conectados": disponibles(),
                  "consulta": "real"})


def ruta_estrategias():
    return _json({"ok": True, **indice_estrategias()})


def _parte(camino, esperado):
    """Extrae `<motor>/<estrategia>` de una ruta POST."""
    resto = camino[len(esperado):].strip("/")
    partes = [p for p in resto.split("/") if p]
    if len(partes) != 2:
        raise ErrorUsuario(
            f"ruta invalida: se esperaba /{esperado.strip('/')}/<motor>/<estrategia>")
    return partes[0], partes[1]


def ruta_respaldar(camino, cuerpo=""):
    motor, estrategia = _parte(camino, "/api/respaldar/")
    datos = _cuerpo_json(cuerpo)
    resultado = generar(motor, estrategia)
    # Si el artefacto existe, se verifica su hash aqui mismo antes de devolverlo.
    if resultado.get("existe") and resultado.get("ruta"):
        ver = verificar_artefacto(resultado["ruta"], resultado.get("sha256"))
        resultado["verificado"] = ver.get("ok", False)
        resultado["sha256"] = ver.get("sha256") or resultado.get("sha256")
        resultado["bytes"] = ver.get("bytes", resultado.get("bytes"))
    resultado.setdefault("accion", "respaldar")
    return _json(resultado, 200 if resultado.get("ok") else 400)


def ruta_restaurar(camino, cuerpo=""):
    motor, estrategia = _parte(camino, "/api/restaurar/")
    datos = _cuerpo_json(cuerpo)
    respaldo_id = datos.get("respaldo_id") or datos.get("id")
    resultado = restaurar(motor, estrategia, respaldo_id)
    resultado.setdefault("accion", "restaurar")
    return _json(resultado, 200 if resultado.get("ok") else 400)


def _cuerpo_json(cuerpo):
    if not cuerpo:
        return {}
    try:
        datos = json.loads(cuerpo)
    except ValueError as exc:
        raise ErrorUsuario(f"el cuerpo no es JSON valido: {exc}")
    if not isinstance(datos, dict):
        raise ErrorUsuario("el cuerpo debe ser un objeto JSON")
    return datos


def despacha(metodo, camino, cuerpo=""):
    """Punto unico de entrada de la API. Devuelve {status, cuerpo}."""
    camino = (camino or "/").split("?")[0].rstrip("/") or "/"
    try:
        if camino == "/api/salud":
            return ruta_salud()
        if camino == "/api/motores":
            return ruta_motores()
        if camino == "/api/estrategias":
            return ruta_estrategias()
        if camino.startswith("/api/respaldar"):
            if metodo != "POST":
                return _error("este recurso solo admite POST", 405)
            return ruta_respaldar(camino, cuerpo)
        if camino.startswith("/api/restaurar"):
            if metodo != "POST":
                return _error("este recurso solo admite POST", 405)
            return ruta_restaurar(camino, cuerpo)
        if camino.startswith("/api/"):
            return _error(f"ruta de API desconocida: {camino}", 404)
    except ErrorUsuario as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # pragma: no cover - red de seguridad
        return _error(f"fallo interno del servidor: {exc}", 500)
    return _error(f"ruta desconocida: {camino}", 404)


# ---------------------------------------------------------------- servidor HTTP
class Manejador(BaseHTTPRequestHandler):
    server_version = "DataForgeBackup/1.0"

    def log_message(self, formato, *args):  # salida limpia en la demo
        sys.stderr.write(f"  {formato % args}\n")

    def _envia(self, status, cuerpo, tipo="application/json; charset=utf-8"):
        if isinstance(cuerpo, str):
            cuerpo = cuerpo.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _estatico(self, camino_rel):
        # Sin traversal: se resuelve dentro de app/web y se comprueba.
        limpio = os.path.normpath(camino_rel).lstrip("./")
        destino = os.path.join(RAIZ_WEB, limpio)
        if not os.path.abspath(destino).startswith(os.path.abspath(RAIZ_WEB) + os.sep):
            return self._envia(403, "403", "text/plain; charset=utf-8")
        if os.path.isdir(destino):
            destino = os.path.join(destino, "index.html")
        if not os.path.isfile(destino):
            return self._envia(404, f"no existe: {limpio}", "text/plain; charset=utf-8")
        ext = os.path.splitext(destino)[1]
        with open(destino, "rb") as fh:
            self._envia(200, fh.read(), TIPOS.get(ext, "application/octet-stream"))

    def do_GET(self):
        camino = self.path.split("?")[0]
        if camino.startswith("/api/"):
            r = despacha("GET", self.path)
            return self._envia(r["status"], r["cuerpo"])
        if camino in ("/", "/index.html"):
            return self._estatico("index.html")
        return self._estatico(camino.lstrip("/"))

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        cuerpo = self.rfile.read(largo).decode("utf-8") if largo else ""
        if not self.path.startswith("/api/"):
            return self._envia(404, "404", "text/plain; charset=utf-8")
        r = despacha("POST", self.path, cuerpo=cuerpo)
        self._envia(r["status"], r["cuerpo"])


def main():
    ap = argparse.ArgumentParser(
        description="DataForge Backup · estrategias de respaldo para NoSQL")
    ap.add_argument("--puerto", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--sin-navegador", action="store_true",
                    help="no abrir el navegador automaticamente")
    args = ap.parse_args()

    if not os.path.isdir(RAIZ_WEB):
        print(f"ERROR: falta la interfaz en {RAIZ_WEB}", file=sys.stderr)
        return 1

    try:
        serv = ThreadingHTTPServer((args.host, args.puerto), Manejador)
    except OSError as exc:
        print(f"ERROR: no se pudo escuchar en {args.host}:{args.puerto}: {exc}",
              file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.puerto}/"
    print("=" * 64)
    print("  DataForge Backup · estrategias de respaldo para bases NoSQL")
    print("  Motores: MongoDB · Redis · Neo4j   (sin MS SQL Server)")
    print(f"  Estrategias declaradas: {len(listar_estrategias())}")
    print(f"  Abrir:   {url}")
    print("  Ctrl+C para detener")
    print("=" * 64)
    if not args.sin_navegador:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        serv.serve_forever()
    except KeyboardInterrupt:
        print("\n  servidor detenido")
    finally:
        serv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())