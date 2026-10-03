# Contrato del nucleo (interfaz entre carriles)

> **Estado real:** el nucleo de motores termino en `app/adaptadores/<motor>.py`
> (no en un paquete `nucleo/`), y el indice de estrategias quedo en
> `app/respaldos.py`. `app/adaptador.py` se escribio para no depender de una
> disposicion concreta: busca las funciones por nombre en varios sitios y se
> adapta a la firma que encuentre. Este documento conserva el contrato que se
> pidio y anota donde acabo cada pieza.

Este documento fija la interfaz que el servidor (`app.py`) y las pruebas
(`app/tests/casos.py`) consumen del nucleo. El nucleo lo escribe otro carril;
este archivo existe para que ambas partes hablen el mismo idioma y no haya que
adivinar firmas.

Donde acabo cada pieza:

| Archivo | Responsabilidad | Estado |
| :- | :- | :- |
| `app/respaldos.py` | Indice de estrategias y formato del artefacto | en este carril |
| `app/adaptadores/mongodb.py` | Conectar a MongoDB, respaldar y restaurar | nucleo de motores |
| `app/adaptadores/redis.py` | Conectar a Redis, RDB y AOF | nucleo de motores |
| `app/adaptadores/neo4j.py` | Conectar a Neo4j, dump y exportacion | nucleo de motores |
| `app/indice.py` | Indice JSON de los respaldos | nucleo |
| `app/utileria.py` | Hash sha256, cronometro, JSON atomico | nucleo |
| `app/adaptador.py` | Puente que usa el servidor (este carril) | en este carril |

## 1. `respaldos.py`

```python
ESTRATEGIAS = {
    "<id>": {
        "id": "<id>",                  # clave del diccionario
        "nombre": "Respaldo completo", # texto para la interfaz
        "resumen": "una frase",
        "protege": ["...", "..."],     # que protege
        "no_protege": ["...", "..."],  # que NO protege
        "soporta": ["mongodb", "redis", "neo4j"],
    },
    ...
}

def listar_estrategias() -> list[dict]
```

`listar_estrategias()` devuelve una lista de dicts con las claves de arriba. Si no
existe, el adaptador usa `ESTRATEGIAS` directamente.

Ids esperados (los usa la interfaz y las pruebas): `completo`, `incremental`,
`diferencial`, `instantanea`, `replicacion`.

## 2. `motores.py`

```python
MOTORES = ["mongodb", "redis", "neo4j"]

def conectar_mongodb(uri) -> client
def conectar_redis(url) -> client
def conectar_neo4j(uri, usuario=None, clave=None) -> driver

def estado_motores() -> dict
# {motor: {"conectado": bool, "version": str, "detalle": str,
#          "error": str | None, "latencia_ms": float}}

def generar_respaldo(motor: str, estrategia: str) -> dict
def restaurar_respaldo(motor: str, estrategia: str, respaldo_id=None) -> dict
```

`generar_respaldo` ejecuta el respaldo de verdad y devuelve las cifras medidas.
Claves que el servidor espera (las que falten se completan midiendo el artefacto):

| Clave | Tipo | Que es |
| :- | :- | :- |
| `ok` | bool | El respaldo se genero |
| `motor` / `estrategia` | str | Eco de lo pedido |
| `ruta` | str | Ruta del artefacto en disco |
| `bytes` | int | Tamano real del artefacto |
| `duracion_ms` | float | Tiempo medido |
| `sha256` | str | Hash sha256 del artefacto |
| `registros` | int | Cuantos registros entran |
| `detalle` | str | Texto libre para la interfaz |

`restaurar_respaldo` devuelve `ok`, `registros`, `duracion_ms` y `detalle`.

El adaptador llama a estas funciones **dejando pasar solo los argumentos que la
firma real acepta**, asi que un parametro opcional distinto no rompe el servidor.

## 3. `pruebas.py`

```python
def hash_archivo(ruta: str) -> str          # sha256 en hexadecimal
def verificar(ruta: str, esperado=None) -> dict
# {"ok": bool, "sha256": str, "bytes": int, "detalle": str}
```

## 4. Regla de oro

Ninguna cifra devuelta puede ser inventada: si no se pudo medir, la clave se
omite. La interfaz muestra `n/d` en ese caso. El hash que se muestra es el que
el servidor recalcula leyendo el archivo, no el que el nucleo afirma.