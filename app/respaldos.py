#!/usr/bin/env python3
"""respaldos — el indice de estrategias de respaldo.

Este modulo es/documentacion declarativa: describe QUE protege y QUE NO protege
cada estrategia. No ejecuta nada; los motores los ejecuta `adaptadores/`, y la
restauracion la dirige `ejecucion.py`.

La columna de "no protege" es la que importa: una estrategia que no dice que
limita no esta documentada, esta vendida.
"""

ESTRATEGIAS = {
    "completo": {
        "id": "completo",
        "nombre": "Respaldo completo",
        "resumen": "Copia entera de la base en un solo artefacto.",
        "protege": [
            "Todos los documentos, colecciones, indices y usuarios",
            "Restauracion sin depender de ningun respaldo anterior",
            "La base completa ante corrupcion o borrado accidental",
        ],
        "no_protege": [
            "No protege de la falla del disco que contiene el artefacto",
            "No protege de un borrado posterior al respaldo",
            "Su costo crece con el tamano de la base: en temporada alta es inviable",
        ],
        "soporta": ["mongodb", "redis", "neo4j"],
    },
    "incremental": {
        "id": "incremental",
        "nombre": "Respaldo incremental",
        "resumen": "Solo lo que cambio desde el ultimo respaldo completo.",
        "protege": [
            "Los cambios posteriores al ultimo completo",
            "Ventana de perdida acotada al ultimo ciclo completo",
            "Costo y espacio mucho menores que un completo",
        ],
        "no_protege": [
            "Depende de todos los completos anteriores: si falta uno, se rompe la cadena",
            "Restaurar exige encadenar completos e incrementales en orden",
            "No protege de la corrupcion del propio archivo incremental",
        ],
        "soporta": ["mongodb", "redis", "neo4j"],
    },
    "diferencial": {
        "id": "diferencial",
        "nombre": "Respaldo diferencial",
        "resumen": "Los cambios desde el ultimo respaldo de la misma estrategia.",
        "protege": [
            "Los cambios desde la ultima diferencial, con restauracion corta",
            "Ventana de perdida acotada sin multiplicar el numero de cadenas",
            "Evita repetir la base entera en cada ciclo",
        ],
        "no_protege": [
            "No protege los cambios acumulados desde el ultimo completo",
            "Igual que el incremental, depende de una base anterior intacta",
            "Si se pierde la diferencial vigente, se pierde toda su ventana",
        ],
        "soporta": ["mongodb", "redis", "neo4j"],
    },
    "instantanea": {
        "id": "instantanea",
        "nombre": "Instantanea (snapshot)",
        "resumen": "El estado en un instante concreto, sin detener el motor.",
        "protege": [
            "Consistencia puntual sin detener el servicio",
            "Mide el estado de las estructuras de almacenamiento reales",
            "Restauracion muy rapida: basta con montar el archivo",
        ],
        "no_protege": [
            "No protege de la corrupcion posterior del archivo de la instantanea",
            "Requiere almacenamiento transaccional (WiredTiger, AOF) para ser consistente",
            "Solo sirve en el motor que la produjo: no es portable",
        ],
        "soporta": ["mongodb", "redis"],
    },
    "replicacion": {
        "id": "replicacion",
        "nombre": "Replicacion",
        "resumen": "Una segunda copia siempre viva, sincronizada.",
        "protege": [
            "Disponibilidad continua ante caida del nodo principal",
            "Lecturas y failover sin ventana de perdida si es sincrona",
            "Recuperacion rapida sin restaurar nada",
        ],
        "no_protege": [
            "NO protege frente a operaciones destructivas: un DROP se replica al instante",
            "No protege ante corrupcion logica replicada",
            "No es un respaldo historico: guarda el presente, no el pasado",
        ],
        "soporta": ["mongodb", "redis", "neo4j"],
    },
}


def listar_estrategias() -> list:
    """El indice completo como lista de dicts, en orden de declaracion."""
    return [dict(estrategia) for estrategia in ESTRATEGIAS.values()]