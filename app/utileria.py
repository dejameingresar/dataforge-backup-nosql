#!/usr/bin/env python3
"""Utilerias compartidas: hash, cronometro, JSON atomico y JSON Lines.

El hash sha256 es la pieza que hace util el indice: sin el no se puede
detectar que un artefacto se corrompio o se sustituyo en el destino.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

TAMANO_BLOQUE = 1024 * 1024  # 1 MiB: leer por bloques evita cargar dumps grandes en RAM


# --- Hash e integridad -------------------------------------------------------


def sha256_de_archivo(ruta: str) -> str:
    """Calcula el sha256 de un archivo leyendolo por bloques."""
    digest = hashlib.sha256()
    with open(ruta, "rb") as archivo:
        while True:
            bloque = archivo.read(TAMANO_BLOQUE)
            if not bloque:
                break
            digest.update(bloque)
    return digest.hexdigest()


def sha256_de_texto(texto: str) -> str:
    """sha256 de una cadena utf-8."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def sha256_de_arbol(ruta_raiz: str) -> str:
    """sha256 estable de un directorio completo.

    Se recorre en orden alfabetico y se mezcla el nombre relativo de cada
    archivo con su contenido, de forma que un cambio de nombre tambien
    rompe el hash. Sin esto, mover un archivo de lugar pasaria desapercibido.
    """
    digest = hashlib.sha256()
    base = os.path.abspath(ruta_raiz)
    for carpeta, subcarpetas, archivos in os.walk(base):
        subcarpetas.sort()
        for nombre in sorted(archivos):
            completa = os.path.join(carpeta, nombre)
            relativo = os.path.relpath(completa, base)
            digest.update(relativo.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha256_de_archivo(completa).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def sha256_de_manifiesto(ruta_manifiesto: str) -> str:
    """sha256 de un directorio a partir de su archivo manifiesto.

    Los respaldos que copian arboles de archivos (el snapshot de WiredTiger
    y el AOF de Redis) escriben un manifiesto con el hash de cada pieza.
    El hash del conjunto sale del manifiesto, no de recorrer de nuevo.
    """
    with open(ruta_manifiesto, encoding="utf-8") as archivo:
        datos = json.load(archivo)
    digest = hashlib.sha256()
    for entrada in datos["archivos"]:
        digest.update(entrada["ruta"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entrada["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verificar_manifiesto(ruta_manifiesto: str) -> dict:
    """Revisa pieza por pieza los archivos listados en un manifiesto.

    Devuelve cuantos archivos estan, cuantos rompieron el hash y cuales.
    """
    with open(ruta_manifiesto, encoding="utf-8") as archivo:
        datos = json.load(archivo)
    base = os.path.dirname(os.path.abspath(ruta_manifiesto))
    archivos_ok = 0
    archivos_fallidos = []
    for entrada in datos["archivos"]:
        completa = os.path.join(base, entrada["ruta"])
        if not os.path.isfile(completa):
            archivos_fallidos.append({"ruta": entrada["ruta"], "motivo": "falta"})
            continue
        actual = sha256_de_archivo(completa)
        if actual != entrada["sha256"]:
            archivos_fallidos.append(
                {"ruta": entrada["ruta"], "motivo": "hash_distinto"}
            )
        else:
            archivos_ok += 1
    return {
        "archivos_revisados": len(datos["archivos"]),
        "archivos_ok": archivos_ok,
        "archivos_fallidos": archivos_fallidos,
        "integro": not archivos_fallidos,
    }


# --- Cronometro ---------------------------------------------------------------


class Cronometro:
    """Mide duracion en milisegundos con perf_counter.

    Se usa como contexto: `with Cronometro() as c: ...` y luego `c.ms`.
    """

    def __init__(self) -> None:
        self.inicio = 0.0
        self.fin = 0.0

    def __enter__(self) -> "Cronometro":
        self.inicio = time.perf_counter()
        return self

    def __exit__(self, *_excepcion) -> None:
        self.fin = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((self.fin - self.inicio) * 1000.0, 3)


@contextmanager
def cronometro() -> Iterator[Cronometro]:
    """Atajo: `with cronometro() as c:` en lugar de instanciar a mano."""
    marca = Cronometro()
    with marca:
        yield marca


# --- Escritura de archivos ---------------------------------------------------


def escribir_json_atomico(ruta: str, datos: Any) -> None:
    """Escribe JSON a un archivo temporal y luego lo renombra.

    El renombrado es atomico en el mismo sistema de archivos: si el proceso
    muere a mitad de escritura, el indice anterior sigue intacto en vez de
    quedar un JSON truncado que no se puede leer.
    """
    os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
    temporal = ruta + ".parcial"
    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2, sort_keys=False)
        archivo.write("\n")
    os.replace(temporal, ruta)


def leer_json(ruta: str) -> Any:
    with open(ruta, encoding="utf-8") as archivo:
        return json.load(archivo)


def escribir_jsonl(ruta: str, filas: Any) -> int:
    """Escribe una lista de objetos como JSON Lines. Devuelve cuantas lineas."""
    os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
    total = 0
    with open(ruta, "w", encoding="utf-8") as archivo:
        for fila in filas:
            archivo.write(json.dumps(fila, ensure_ascii=False, default=str))
            archivo.write("\n")
            total += 1
    return total


def leer_jsonl(ruta: str) -> list:
    """Lee un JSON Lines completo en memoria."""
    filas = []
    with open(ruta, encoding="utf-8") as archivo:
        for linea in archivo:
            linea = linea.strip()
            if linea:
                filas.append(json.loads(linea))
    return filas


def escribir_manifiesto(ruta: str, base: str) -> str:
    """Genera el manifiesto con el sha256 de cada archivo bajo `base`.

    Devuelve la ruta del manifiesto escrito. El manifiesto excluye a si
    mismo del listado, porque su hash no puede contenerse.
    """
    archivos = []
    for carpeta, subcarpetas, nombres in os.walk(base):
        subcarpetas.sort()
        for nombre in sorted(nombres):
            completa = os.path.join(carpeta, nombre)
            relativo = os.path.relpath(completa, base)
            if relativo == os.path.basename(ruta):
                continue
            archivos.append(
                {
                    "ruta": relativo,
                    "bytes": os.path.getsize(completa),
                    "sha256": sha256_de_archivo(completa),
                }
            )
    datos = {
        "generado_en":ahora_iso(),
        "base": os.path.abspath(base),
        "total_archivos": len(archivos),
        "total_bytes": sum(a["bytes"] for a in archivos),
        "archivos": archivos,
    }
    escribir_json_atomico(ruta, datos)
    return ruta


def tamano_de_ruta(ruta: str) -> int:
    """Bytes de un archivo o de un directorio completo."""
    if os.path.isfile(ruta):
        return os.path.getsize(ruta)
    total = 0
    for carpeta, _sub, archivos in os.walk(ruta):
        for nombre in archivos:
            total += os.path.getsize(os.path.join(carpeta, nombre))
    return total


def listar_rutas(raiz: str) -> list:
    """Lista relativa y ordenada de todo lo que hay bajo una raiz."""
    base = os.path.abspath(raiz)
    encontradas = []
    for carpeta, subcarpetas, archivos in os.walk(base):
        subcarpetas.sort()
        for nombre in sorted(archivos):
            completa = os.path.join(carpeta, nombre)
            encontradas.append(os.path.relpath(completa, base))
    return sorted(encontradas)


# --- Utilidades varias -------------------------------------------------------


def ahora_iso() -> str:
    """Marca de tiempo local en ISO 8601, con segundos."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def copiar_arbol(origen: str, destino: str, excluir: tuple = ()) -> int:
    """Copia un arbol de archivos devolviendo los bytes copiados."""
    os.makedirs(destino, exist_ok=True)
    total = 0
    for carpeta, subcarpetas, archivos in os.walk(origen):
        subcarpetas.sort()
        rel = os.path.relpath(carpeta, origen)
        destino_carpeta = destino if rel == "." else os.path.join(destino, rel)
        os.makedirs(destino_carpeta, exist_ok=True)
        for nombre in sorted(archivos):
            if any(patron in nombre for patron in excluir):
                continue
            origen_completo = os.path.join(carpeta, nombre)
            destino_completo = os.path.join(destino_carpeta, nombre)
            shutil.copy2(origen_completo, destino_completo)
            total += os.path.getsize(destino_completo)
    return total


def ejecutar(comando: list, timeout: int = 300) -> subprocess.CompletedProcess:
    """Ejecuta un comando externo capturando stdout y stderr."""
    return subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def esperar_hasta(condicion: Callable[[], bool], segundos: float, intervalo: float = 0.05) -> bool:
    """Espera activa hasta que la condicion sea verdadera o se agote el tiempo."""
    limite = time.time() + segundos
    while time.time() < limite:
        if condicion():
            return True
        time.sleep(intervalo)
    return condicion()


def bytes_a_legible(total: int) -> str:
    """Convierte bytes a una cadena corta tipo 1.4 MiB."""
    if total < 1024:
        return f"{total} B"
    if total < 1024 * 1024:
        return f"{total / 1024:.1f} KiB"
    if total < 1024 * 1024 * 1024:
        return f"{total / (1024 * 1024):.2f} MiB"
    return f"{total / (1024 * 1024 * 1024):.2f} GiB"
