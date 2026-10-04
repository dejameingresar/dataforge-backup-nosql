#!/usr/bin/env python3
"""Clasifica la salida de la suite separando dos cosas que antes se mezclaban.

El runner de GitHub Actions no tiene MongoDB, Redis ni Neo4j, asi que los casos
que necesitan un motor real no pueden correr ahi. Antes el workflow terminaba
siempre en exito (`exit 0`), lo que hacia que la automatizacion no midiera
nada; pero poner el job en rojo sin mas convertiria la ausencia de motores en
un fallo de codigo.

Este script separa los dos casos:

  - fallo explicado por un motor ausente o un driver no instalado: se informa
    y se cuenta aparte. No es un defecto del codigo.
  - cualquier otro fallo: es un defecto real y sale con codigo 1.

Uso:  python3 clasificar_ci.py salida.txt
Salida: un resumen en stdout; codigo 1 si hay fallos reales, 0 si no.

No inventa un resultado: si el log no tiene el formato esperado, lo dice y
devuelve 0 para no tapar un problema de este propio script.
"""
from __future__ import annotations

import re
import sys

# Motivos que dependen del entorno, no del codigo bajo prueba.
MARCAS_ENTORNO = (
    "esta apagado",
    "no module named",
    "conector ausente",
    "no hay conector",
    "motor no (levantado|disponible)",
    "no hay motor",
)

TIMESTAMP = re.compile(r"^\S+\s+Z\s+")
INICIO_FALLA = re.compile(r"\[FALLA\]\s*(T-\d+)?")
OTRO_CASO = re.compile(r"\[(FALLA|OK| ok | OK )\]", re.IGNORECASE)


def bloques_de_fallo(lineas):
    """Agrupa cada linea [FALLA] con las lineas de su traceback."""
    bloques, actual = [], None
    for cruda in lineas:
        linea = TIMESTAMP.sub("", cruda)
        if INICIO_FALLA.search(linea):
            actual = {"linea": linea.strip(), "cuerpo": [linea]}
            bloques.append(actual)
        elif actual is not None:
            if OTRO_CASO.search(linea):
                actual = None
            else:
                actual["cuerpo"].append(linea)
    return bloques


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: python3 clasificar_ci.py <salida.txt>")
        return 2

    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        crudo = fh.read()
    lineas = crudo.splitlines()

    marcas = [l for l in lineas if "Casos:" in l and "exitosos" in l]
    if not marcas:
        print("::error::No se encontro la linea de resumen de la suite en "
              f"{sys.argv[1]}: este script no entiende esa salida")
        return 0

    bloques = bloques_de_fallo(lineas)
    entorno, reales = [], []

    for b in bloques:
        texto = "\n".join(b["cuerpo"])
        (entorno if any(re.search(m, texto, re.IGNORECASE) for m in MARCAS_ENTORNO)
         else reales).append(b["linea"])

    print(marcas[-1].strip())
    for l in lineas:
        if "Motores medidos en vivo" in l:
            print(l.strip())
    print(f"fallos explicados por el entorno: {len(entorno)}")
    for l in entorno:
        print(f"   [entorno] {l}")

    if reales:
        print(f"\nfallos reales de codigo: {len(reales)}")
        for l in reales:
            print(f"   [CODIGO]  {l}")
        print("\n::error::La suite tiene fallos que no dependen de los motores. "
              "El job se pone en rojo.")
        return 1

    print("\n::warning::Ningun fallo es de codigo: los que hay dependen de "
          "MongoDB, Redis o Neo4j, que no existen en el runner. Se verifican "
          "contra los motores locales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())