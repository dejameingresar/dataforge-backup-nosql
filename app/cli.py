#!/usr/bin/env python3
"""Interfaz de linea de ordenes del nucleo de respaldos.

    python3 -m app.cli listar              # muestra el indice
    python3 -m app.cli ejecutar            # corre los seis respaldos
    python3 -m app.cli verificar           # revalida los hashes del indice
    python3 -m app.cli comparar            # imprime la comparacion honesta

Lo que hace el nucleo, en una linea por motor:

  MongoDB  mongodump/mongorestore (logico, en caliente) y copia en frio del
           dbpath de WiredTiger (fisico, motor detenido).
  Redis    BGSAVE al dump.rdb y BGREWRITEAOF al AOF multipart, los dos sin
           parar el servidor.
  Neo4j    dump binario con neo4j-admin (Community exige la base desmontada) y
           exportacion de nodos y aristas a JSON Lines con Cypher.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, indice


def comando_listar(argumentos) -> int:
    try:
        print(indice.imprimir_indice())
    except FileNotFoundError:
        print(
            "Todavia no hay indice. Corré 'python3 -m app.cli ejecutar' "
            "para generar los respaldos."
        )
        return 1
    return 0


def comando_verificar(argumentos) -> int:
    resultado = indice.verificar_indice()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
    if resultado["alterados"]:
        print(
            f"\nFALLAN {resultado['alterados']} de {resultado['revisados']} "
            "artefactos: el indice ya no describe lo que hay en disco."
        )
        return 1
    print(f"\nLos {resultado['intactos']} artefactos coinciden con su hash.")
    return 0


def comando_ejecutar(argumentos) -> int:
    from . import ejecucion

    return ejecucion.main() if argumentos.sin_corte is False else _ejecutar_con(argumentos)


def _ejecutar_con(argumentos) -> int:
    sys.argv = ["ejecucion"] + (["--sin-corte"] if argumentos.sin_corte else [])
    from . import ejecucion

    return ejecucion.main()


def comando_comparar(argumentos) -> int:
    try:
        documento = indice.leer_indice()
    except FileNotFoundError:
        print("No hay indice todavia.")
        return 1

    print("=" * 100)
    print("COMPARACION HONESTA: rendimiento medido")
    print("=" * 100)
    print(f"{'motor':<10} {'estrategia':<22} {'tamano':>10} {'respaldo':>12} {'restore':>12}  round-trip")
    print("-" * 100)
    for entrada in documento["respaldos"]:
        arte = entrada["artefacto"]
        rend = entrada["rendimiento"]
        rt = entrada["round_trip"]
        print(
            f"{entrada['motor']:<10} {entrada['estrategia']:<22} "
            f"{arte['bytes_legible']:>10} {rend['duracion_legible']:>12} "
            f"{_ms(rt.get('duracion_restore_ms')):>12}  {rt.get('resultado', '-')}"
        )

    print()
    print("-" * 100)
    print("LIMITACIONES DEL ENTORNO (medidas, no supuestas)")
    print("-" * 100)
    for nota in documento.get("restricciones_del_entorno", []):
        print(f"  * {nota}")

    print()
    print("-" * 100)
    print("QUE FALLA PRIMERO SI EL MOTOR SE APAGA A MITAD")
    print("-" * 100)
    for hallazgo in documento.get("comparacion_honesta", []):
        print(f"  [{hallazgo['motor']} / {hallazgo.get('estrategia', '-')}]")
        for clave, valor in hallazgo.items():
            if clave in ("motor", "estrategia", "veredicto"):
                continue
            print(f"      {clave}: {valor}")
        print(f"      veredicto: {hallazgo['veredicto']}")
        print()
    return 0


def _ms(valor) -> str:
    if valor is None:
        return "-"
    if valor < 1000:
        return f"{valor:.0f} ms"
    return f"{valor / 1000:.2f} s"


def main() -> int:
    analizador = argparse.ArgumentParser(
        prog="app.cli",
        description="Nucleo de estrategias de respaldo para motores NoSQL",
    )
    sub = analizador.add_subparsers(dest="comando", required=True)

    p_listar = sub.add_parser("listar", help="muestra el indice de respaldos")
    p_listar.set_defaults(funcion=comando_listar)

    p_verificar = sub.add_parser("verificar", help="revalida los hashes del indice")
    p_verificar.set_defaults(funcion=comando_verificar)

    p_ejecutar = sub.add_parser("ejecutar", help="corre los respaldos y los restaura")
    p_ejecutar.add_argument(
        "--sin-corte", action="store_true", help="omite la prueba de corte a mitad"
    )
    p_ejecutar.set_defaults(funcion=comando_ejecutar)

    p_comparar = sub.add_parser("comparar", help="imprime la comparacion de estrategias")
    p_comparar.set_defaults(funcion=comando_comparar)

    argumentos = analizador.parse_args()
    return argumentos.funcion(argumentos)


if __name__ == "__main__":
    sys.exit(main())
