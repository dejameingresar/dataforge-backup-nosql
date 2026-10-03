#!/usr/bin/env python3
"""Datos de prueba para los tres motores.

Se siembran con una semilla fija para que los conteos sean reproducibles: si
el generador usara azar sin semilla, cada corrida daria numeros distintos y
el round-trip no seria comparable entre ejecuciones.
"""
from __future__ import annotations

import datetime
import random
from typing import Any

from . import config
from .adaptadores.redis import conectar_redis_db

CLIENTES = config.CANTIDAD_CLIENTES
PRODUCTOS = config.CANTIDAD_PRODUCTOS
PEDIDOS = config.CANTIDAD_PEDIDOS
EVENTOS = config.CANTIDAD_EVENTOS
NODOS_GRAFO = config.CANTIDAD_NODOS_GRAFO

ZONAS = ["Lima", "Arequipa", "Cusco", "Trujillo", "Piura", "Chiclayo"]
CATEGORIAS = ["alimentos", "tecnologia", "ropa", "hogar", "deportes"]
ESTADOS_PEDIDO = ["pendiente", "enviado", "entregado", "cancelado"]
TIPOS_EVENTO = ["alta", "modificacion", "baja"]


def _aleatorio(semilla: int) -> random.Random:
    return random.Random(semilla)


def _fecha_aleatoria(rng: random.Random) -> str:
    base = datetime.date(2026, 1, 1)
    return (base + datetime.timedelta(days=rng.randint(0, 300))).isoformat()


# --- MongoDB -----------------------------------------------------------------


def generar_documentos_mongo() -> dict:
    """Arma los documentos de las cuatro colecciones de MongoDB."""
    rng = _aleatorio(config.SEMILLA_DATOS)

    clientes = [
        {
            "codigo": f"CLI-{i:05d}",
            "nombre": f"Cliente {i:05d}",
            "documento": f"{70000000 + i}",
            "zona": rng.choice(ZONAS),
            "activo": i % 7 != 0,
            "puntos": rng.randint(0, 5000),
        }
        for i in range(CLIENTES)
    ]

    productos = [
        {
            "codigo": f"PRO-{i:05d}",
            "nombre": f"Producto {i:05d}",
            "categoria": rng.choice(CATEGORIAS),
            "precio": round(rng.uniform(5.0, 500.0), 2),
            "stock": rng.randint(0, 800),
            "vigente": i % 11 != 0,
        }
        for i in range(PRODUCTOS)
    ]

    pedidos = []
    for i in range(PEDIDOS):
        cliente = rng.choice(clientes)
        lineas = rng.randint(1, 5)
        productos_pedido = [rng.choice(productos) for _ in range(lineas)]
        total = round(sum(p["precio"] for p in productos_pedido), 2)
        pedidos.append(
            {
                "codigo": f"PED-{i:06d}",
                "cliente": cliente["codigo"],
                "productos": [p["codigo"] for p in productos_pedido],
                "total": total,
                "estado": rng.choice(ESTADOS_PEDIDO),
                "fecha": _fecha_aleatoria(rng),
            }
        )

    eventos = [
        {
            "secuencia": i,
            "entidad": f"PRO-{rng.randint(0, PRODUCTOS - 1):05d}",
            "tipo": rng.choice(TIPOS_EVENTO),
            "marca": _fecha_aleatoria(rng),
            "detalle": f"evento de control numero {i}",
        }
        for i in range(EVENTOS)
    ]

    return {
        "clientes": clientes,
        "productos": productos,
        "pedidos": pedidos,
        "eventos": eventos,
    }


def sembrar_mongodb(cliente, base: str = config.DB_MONGO_ORIGEN) -> dict:
    """Vacia la base y carga los documentos de prueba.

    Los indices se crean DESPUES de insertar: insertar primero y crear el
    indice al final hace que el backup mida el costo de construir indices y no
    el de la data.
    """
    datos = generar_documentos_mongo()
    db = cliente[base]
    cliente.drop_database(base)

    counts = {}
    for coleccion, documentos in datos.items():
        if documentos:
            db[coleccion].insert_many(documentos)
            counts[coleccion] = len(documentos)

    db.clientes.create_index("documento", unique=True)
    db.productos.create_index("categoria")
    db.pedidos.create_index([("cliente", 1), ("fecha", -1)])
    db.eventos.create_index("secuencia", unique=True)

    return counts


# --- Redis -------------------------------------------------------------------


def generar_datos_redis() -> dict:
    """Arma claves de los cinco tipos de dato de Redis.

    Se cubre a proposito mas de un tipo: un respaldo que solo sabe contar
    claves puede devolver el numero correcto y aun asi perder la estructura.
    """
    rng = _aleatorio(config.SEMILLA_DATOS)

    cadenas = {f"cadena:{i:04d}": f"valor-{i:04d}-{rng.randint(0, 9999)}" for i in range(120)}
    mapas = {
        f"mapa:{i:04d}": {f"campo{j}": rng.randint(0, 999) for j in range(5)}
        for i in range(40)
    }
    listas = {
        f"lista:{i:04d}": [f"elemento-{i}-{j}" for j in range(rng.randint(3, 9))]
        for i in range(30)
    }
    conjuntos = {f"conjunto:{i:04d}": {f"miembro{j}" for j in range(6)} for i in range(25)}
    ordenados = {
        f"ordenado:{i:04d}": {f"miembro{j}": rng.randint(1, 100) for j in range(6)}
        for i in range(25)
    }

    return {
        "cadenas": cadenas,
        "mapas": mapas,
        "listas": listas,
        "conjuntos": conjuntos,
        "ordenados": ordenados,
    }


def sembrar_redis(cliente, indice: int = config.DB_REDIS_ORIGEN) -> dict:
    """Vacia el indice y carga las claves de prueba."""
    # El numero de base va en el constructor, no en el comando:
    # flushdb() solo acepta palabras clave.
    propia = conectar_redis_db(indice)
    propia.flushdb()
    propia.close()
    datos = generar_datos_redis()

    if datos["cadenas"]:
        cliente.mset(datos["cadenas"])
    for clave, campos in datos["mapas"].items():
        cliente.hset(clave, mapping={k: str(v) for k, v in campos.items()})
    for clave, elementos in datos["listas"].items():
        cliente.rpush(clave, *elementos)
    for clave, miembros in datos["conjuntos"].items():
        cliente.sadd(clave, *miembros)
    for clave, miembros in datos["ordenados"].items():
        # Los ordenados se guardan como {miembro: puntuacion}, asi que el
        # bucle tiene que recorrer los pares, no solo las claves.
        cliente.zadd(clave, miembros)

    conteos = {
        etiqueta: len(coleccion) for etiqueta, coleccion in datos.items()
    }
    return {
        "claves": sum(conteos.values()),
        "por_tipo": conteos,
    }


# --- Neo4j -------------------------------------------------------------------


def generar_grafo_neo4j(nodos: int = NODOS_GRAFO) -> dict:
    """Arma una lista de nodos y otra de relaciones para el grafo de prueba.

    Los nodos llevan la propiedad "codigo", que es la clave de negocio que usa
    el restore para volver a armarlos. Sin ella el grafo no se podria
    reconstruir.
    """
    rng = _aleatorio(config.SEMILLA_DATOS)

    lista_nodos = []
    for i in range(nodos):
        lista_nodos.append(
            {
                "codigo": f"ENT-{i:05d}",
                "nombre": f"Entidad {i:05d}",
                "tipo": rng.choice(CATEGORIAS),
                "valor": rng.randint(1, 10000),
                "activo": i % 5 != 0,
            }
        )

    lista_aristas = []
    # Cada nodo se enlaza con hasta tres nodos anteriores: asi el grafo queda
    # conexo y con una estructura de red real, no una lista.
    for i in range(1, nodos):
        for _ in range(min(3, i)):
            destino = rng.randint(0, i - 1)
            lista_aristas.append(
                {
                    "origen": f"ENT-{i:05d}",
                    "destino": f"ENT-{destino:05d}",
                    "peso": rng.randint(1, 100),
                    "tipo": rng.choice(["dependency", "containment", "reference"]),
                }
            )

    return {"nodos": lista_nodos, "aristas": lista_aristas}


def sembrar_neo4j(sesion, base: str = config.DB_NEO4J_ORIGEN) -> dict:
    """Vacia la base del grafo y carga los nodos y relaciones de prueba."""
    sesion.run("MATCH (n) DETACH DELETE n").consume()

    grafo = generar_grafo_neo4j()
    nodos = [
        {
            "clave": n["codigo"],
            "tipo": n["tipo"],
            "nombre": n["nombre"],
            "valor": n["valor"],
            "activo": n["activo"],
        }
        for n in grafo["nodos"]
    ]
    sesion.run(
        "UNWIND $filas AS fila "
        "CREATE (n:Entidad {codigo: fila.clave}) "
        "SET n += fila.propiedades "
        "RETURN count(n) AS c",
        filas=[
            {
                "clave": n["clave"],
                "propiedades": {
                    "nombre": n["nombre"],
                    "valor": n["valor"],
                    "activo": n["activo"],
                    "categoria": n["tipo"],
                },
            }
            for n in nodos
        ],
    ).consume()

    for etiqueta in sorted({n["tipo"] for n in nodos}):
        sesion.run(
            f"MATCH (n:Entidad) WHERE n.categoria = $categoria SET n:{etiqueta} RETURN count(n) AS c",
            categoria=etiqueta,
        ).consume()

    # Las relaciones se agrupan por tipo: en Cypher el tipo es literal.
    por_tipo: dict = {}
    for arista in grafo["aristas"]:
        por_tipo.setdefault(arista["tipo"], []).append(arista)
    for tipo, grupo in por_tipo.items():
        sesion.run(
            "UNWIND $filas AS fila "
            "MATCH (a:Entidad {codigo: fila.origen}) "
            "MATCH (b:Entidad {codigo: fila.destino}) "
            f"CREATE (a)-[r:{tipo}]->(b) "
            "SET r.peso = fila.peso "
            "RETURN count(r) AS c",
            filas=grupo,
        ).consume()

    return {
        "nodos": len(nodos),
        "relaciones": len(grafo["aristas"]),
        "tipos_relacion": sorted(por_tipo),
    }
