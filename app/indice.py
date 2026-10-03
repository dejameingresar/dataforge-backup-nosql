#!/usr/bin/env python3
"""Indice de respaldos en JSON.

Una entrada por cada estrategia ejecutada. Es la pieza que hace util la app:
cada registro lleva el hash sha256 del artefacto, de modo que se puede
comprobar meses despues si el archivo que se quiere restaurar sigue siendo
el mismo, sin restaurarlo para saberlo.

Se escribe de forma atomica (temporal mas renombrado) para que un corte de
luz a mitad de la escritura no deje un indice truncado que no se pueda leer.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from typing import Any

from . import config
from .utileria import (
    ahora_iso,
    bytes_a_legible,
    escribir_json_atomico,
    leer_json,
    sha256_de_arbol,
    sha256_de_archivo,
    sha256_de_manifiesto,
)

RUTA_INDICE = os.path.join(config.DIR_INDICE, "indice_respaldos.json")

VERSION_INDICE = "1.0"


def _hash_de_artefacto(ruta: str) -> dict:
    """Elige como calcular el hash segun lo que sea la ruta.

    Tres casos: un archivo suelto, un arbol de archivos (el dump logico) o un
    directorio con manifiesto (el snapshot fisico y el AOF).
    """
    if os.path.isfile(ruta):
        return {
            "tipo": "archivo",
            "sha256": sha256_de_archivo(ruta),
            "bytes": os.path.getsize(ruta),
        }
    manifiesto = os.path.join(ruta, "manifiesto.json")
    if os.path.isfile(manifiesto):
        return {
            "tipo": "arbol_con_manifiesto",
            "sha256": sha256_de_manifiesto(manifiesto),
            "manifiesto": manifiesto,
            "bytes": _bytes_manifiesto(manifiesto),
        }
    return {
        "tipo": "arbol",
        "sha256": sha256_de_arbol(ruta),
        "bytes": _bytes_arbol(ruta),
    }


def _bytes_manifiesto(ruta: str) -> int:
    with open(ruta, encoding="utf-8") as archivo:
        return json.load(archivo)["total_bytes"]


def _bytes_arbol(ruta: str) -> int:
    total = 0
    for carpeta, _sub, archivos in os.walk(ruta):
        for nombre in archivos:
            total += os.path.getsize(os.path.join(carpeta, nombre))
    return total


def nueva_entrada(
    motor: str,
    version_motor: str,
    clave_estrategia: str,
    descripcion_estrategia: dict,
    ruta_artefacto: str,
    duracion_ms: float,
    unidades: dict,
    round_trip: dict,
    notas: list = None,
) -> dict:
    """Arma una entrada completa del indice a partir del resultado de un respaldo.

    `unidades` es lo que se midio (documentos, claves, nodos) y `round_trip`
    es lo que se comprobó al restaurar. Se guardan los dos por separado: el
    tamano del archivo y la duracion describen el RESPALDO, y el round-trip es
    lo que demuestra que el RESPALDO sirve.
    """
    huella = _hash_de_artefacto(ruta_artefacto)
    return {
        "motor": motor,
        "version_motor": version_motor,
        "estrategia": clave_estrategia,
        "nombre": descripcion_estrategia["nombre"],
        "descripcion": descripcion_estrategia["descripcion"],
        "cuando_usarla": descripcion_estrategia["cuando_usarla"],
        "protege": descripcion_estrategia["protege"],
        "no_protege": descripcion_estrategia["no_protege"],
        "requiere_parar_servidor": descripcion_estrategia["requiere_parar"],
        "generado_en": ahora_iso(),
        "artefacto": {
            "ruta": os.path.relpath(ruta_artefacto, config.RAIZ_PROYECTO),
            "tipo": huella["tipo"],
            "bytes": huella["bytes"],
            "bytes_legible": bytes_a_legible(huella["bytes"]),
            "sha256": huella["sha256"],
        },
        "rendimiento": {
            "duracion_ms": duracion_ms,
            "duracion_legible": _ms_a_legible(duracion_ms),
            "unidades": unidades,
        },
        "round_trip": round_trip,
        "notas": notas or [],
    }


def _ms_a_legible(ms: float) -> str:
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.2f} s"


def escribir_indice(entradas: list, extra: dict = None) -> str:
    """Guarda el indice completo, reemplazando el anterior."""
    config.asegurar_directorios()
    documento = {
        "version_indice": VERSION_INDICE,
        "generado_en": ahora_iso(),
        "total_estrategias": len(entradas),
        "motores": _resumen_por_motor(entradas),
        "comparacion_honesta": (extra or {}).get("comparacion_honesta", []),
        "restricciones_del_entorno": (extra or {}).get("restricciones_del_entorno", []),
        "ambiente": _ambiente(),
        "respaldos": entradas,
    }
    escribir_json_atomico(RUTA_INDICE, documento)
    return RUTA_INDICE


def _resumen_por_motor(entradas: list) -> dict:
    """Agrupa las estrategias por motor para leerlo de un vistazo."""
    resumen: dict = {}
    for entrada in entradas:
        motor = entrada["motor"]
        if motor not in resumen:
            resumen[motor] = {"estrategias": 0, "nombres": []}
        resumen[motor]["estrategias"] += 1
        resumen[motor]["nombres"].append(entrada["estrategia"])
    return resumen


def _ambiente() -> dict:
    """Datos de la maquina que afectan a las mediciones."""
    return {
        "sistema": platform.system(),
        "lanzamiento": platform.release(),
        "python": sys.version.split()[0],
        "cpu_logicos": os.cpu_count(),
        "nota": (
            "Las duraciones son de una sola ejecucion en una maquina de "
            "escritorio; sirven para comparar estrategias entre si, no como "
            "cifras de referencia de un servidor."
        ),
    }


def leer_indice() -> dict:
    return leer_json(RUTA_INDICE)


def verificar_indice(ruta_indice: str = RUTA_INDICE) -> dict:
    """Recorre el indice y vuelve a calcular el hash de cada artefacto.

    Esta es la comprobacion que da valor al indice: si algo se borro, se
    cambio o se sustituyo en el destino, aqui sale como MANTA.
    """
    documento = leer_json(ruta_indice)
    resultados = []
    intactos = 0
    alterados = 0

    for entrada in documento["respaldos"]:
        ruta = os.path.join(config.RAIZ_PROYECTO, entrada["artefacto"]["ruta"])
        if not os.path.exists(ruta):
            resultados.append(
                {
                    "motor": entrada["motor"],
                    "estrategia": entrada["estrategia"],
                    "estado": "FALTA",
                    "motivo": "el archivo ya no esta en disco",
                }
            )
            alterados += 1
            continue

        huella = _hash_de_artefacto(ruta)
        coincide = huella["sha256"] == entrada["artefacto"]["sha256"]
        if coincide:
            intactos += 1
        else:
            alterados += 1
        resultados.append(
            {
                "motor": entrada["motor"],
                "estrategia": entrada["estrategia"],
                "estado": "MANTA" if coincide else "ALTERADO",
                "hash_esperado": entrada["artefacto"]["sha256"],
                "hash_actual": huella["sha256"],
                "bytes_actual": huella["bytes"],
            }
        )

    return {
        "revisados": len(resultados),
        "intactos": intactos,
        "alterados": alterados,
        "detalle": resultados,
    }


def imprimir_indice(ruta_indice: str = RUTA_INDICE) -> str:
    """Muestra el indice como tabla de texto para la terminal."""
    documento = leer_json(ruta_indice)
    lineas = []
    lineas.append("=" * 100)
    lineas.append(f"INDICE DE RESPALDOS  ({documento['total_estrategias']} estrategias)")
    lineas.append(f"generado: {documento['generado_en']}")
    lineas.append("=" * 100)

    for entrada in documento["respaldos"]:
        arte = entrada["artefacto"]
        rend = entrada["rendimiento"]
        linea = (
            f"[{entrada['motor']} {entrada['version_motor']}] {entrada['nombre']}\n"
            f"    estrategia : {entrada['estrategia']}\n"
            f"    artefacto  : {arte['ruta']} ({arte['bytes_legible']})\n"
            f"    sha256     : {arte['sha256']}\n"
            f"    duracion   : {rend['duracion_legible']}\n"
            f"    measured   : {rend['unidades']}\n"
            f"    round-trip : {entrada['round_trip'].get('resultado', 'sin medir')}\n"
        )
        lineas.append(linea)

    for nota in documento.get("comparacion_honesta", []):
        lineas.append("-" * 100)
        lineas.append(f"  {nota}")

    lineas.append("=" * 100)
    return "\n".join(lineas)
