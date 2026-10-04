# DataForge Backup

**Estrategias de respaldo para bases de datos NoSQL** · proyecto SI-783 Base de Datos II

Aplicación web que ejecuta respaldos reales contra MongoDB, Redis y Neo4j, mide
el resultado y **restaura de vuelta**, comprobando que los datos vuelven intactos.
**MS SQL Server está excluido por el enunciado** y además se rechaza en la API.

No requiere `pip install`: el servidor es `http.server` de la biblioteca estándar.

---

## Enlaces

| | |
|---|---|
| **Aplicación publicada** | https://dataforge-backup-si783.onrender.com |
| **Repositorio** | https://github.com/dejameingresar/dataforge-backup-nosql |
| **Artículo 1** (Patrick Rodriguez Cardenas) | https://dev.to/dejameingresar/seis-estrategias-de-respaldo-para-bases-de-datos-nosql-y-que-falla-de-verdad-5hce |
| **Artículo 2** (Nicole Rios Cohaila) | https://dev.to/korins707/por-que-no-usamos-ms-sql-server-seis-estrategias-de-respaldo-que-si-se-pueden-verificar-gld |
| **Video** | _pendiente de publicación_ |

## 1. Las tres motores y qué se midió

| Motor | Versión | Puerto | Herramienta de respaldo |
| :- | :- | :- | :- |
| MongoDB | 7.0.14 | 27017 | `mongodump` / `mongorestore` |
| Redis | 7.0.15 | 6379 | `BGSAVE` (RDB) y AOF |
| Neo4j | 5.24.0 | 7687 | `neo4j-admin database dump` |

Las versiones de esta tabla son las que contesta cada motor; la interfaz las
consulta en vivo y muestra `n/d` si un motor está apagado. Nunca inventa una
versión.

## 2. Estrategias de respaldo

La columna **"qué NO protege"** es la más importante: es la que evita la sorpresa
después del incidente. Una estrategia que no declara limitación alguna no está
documentada, está vendida.

| Estrategia | Qué protege | Qué NO protege |
| :- | :- | :- |
| **Completo** | Todo el contenido de la base, sin excepciones | No protege de una caída del disco que lo contiene; no protege de un borrado accidental posterior |
| **Incremental** | Solo lo que cambió desde el último respaldo completo | Depende encadenadamente de todos los completos anteriores: si uno falta, la cadena se rompe |
| **Diferencial** | Los cambios desde el último respaldo de esa misma estrategia | No protege los cambios acumulados desde el completo |
| **Instantánea** | El estado en un instante concreto, sin detener el motor | No protege contra corrupción posterior del archivo; exige almacenamiento transaccional para ser consistente |
| **Replicación** | Disponibilidad continua: otra copia siempre viva | **No protege contra el borrado**: un `DROP` se replica al instante |

> El detalle exacto de cada estrategia lo declara el índice en
> `app/respaldos.py`; la interfaz lo muestra tal cual, sin reescribirlo.

## 3. Cómo ejecutarlo

```bash
python3 app.py --sin-navegador --puerto 8080   # aplicación en http://127.0.0.1:8080/
python3 app.py --puerto 9000                   # abre el navegador
```

Los motores deben estar levantados. En el laboratorio:

```bash
# MongoDB y Redis
sudo systemctl start mongod redis-server

# Neo4j (instalado en ~/opt, no es un servicio de systemd)
/home/prodriguez/opt/neo4j-community-5.24.0/bin/neo4j start
```

### API

| Ruta | Qué hace |
| :- | :- |
| `GET /api/salud` | Estado del servicio y cuántos motores contestan |
| `GET /api/motores` | Estado **real** de los tres motores, consultándolos de verdad |
| `GET /api/estrategias` | El índice completo de estrategias en JSON |
| `POST /api/respaldar/<motor>/<estrategia>` | Ejecuta el respaldo y devuelve las cifras medidas |
| `POST /api/restaurar/<motor>/<estrategia>` | Restaura el respaldo |

```bash
curl -s localhost:8080/api/salud
curl -s -X POST localhost:8080/api/respaldar/mongodb/completo
```

**Todo error del usuario devuelve 400, nunca 500.** Un motor inexistente, una
estrategia inexistente o un JSON inválido son peticiones incorrectas, no caídas
del servidor.

## 4. La interfaz

Tres paneles:

1. **Estado real de los motores** — una tarjeta por motor con su versión y su
   latencia, consultadas al cargar. Si está apagado, se dice, con `n/d` en la
   versión.
2. **Estrategias** — la tabla con qué protege cada una y **qué no**.
3. **Ejecutar** — botones para respaldar y restaurar, con barra de progreso y el
   resultado: tamaño en disco, duración medida, hash sha256 y el veredicto de la
   verificación.

**Ninguna cifra de la pantalla es estimada.** El tamaño y el hash se vuelven a
medir leyendo el archivo generado; si algo no se pudo medir, se muestra `n/d`.

## 5. Cómo verificarlo

```bash
python3 app/tests/casos.py    # 27 casos numerados (T-01..T-27)
python3 app/tests/smoke.py    # recorrido real en navegador con Playwright
python3 checks.py app.py      # barrido de corrupción de texto
```

| Bloque | Casos | Qué cubren |
| :- | :- | :- |
| Estrategias | T-01..T-08 | El índice declara qué protege y qué no; ids únicos; motor inválido se rechaza |
| API | T-09..T-16 | Códigos de estado; todo error de usuario da 400 y no 500 |
| Motores reales | T-17..T-27 | Estado real de los tres motores; hash; conteo dentro del artefacto; y el **ciclo completo** |

Los tres motores tienen que estar levantados para que la suite pase entera. Si
uno está apagado, el caso que lo necesita **falla con un mensaje explícito** en
vez de dar verde: un `n/d` en la pantalla es correcto, un test verde sin motor
no lo sería.

Estado medido la última vez que se ejecutó:

| Motor | Estado | Casos afectados |
| :- | :- | :- |
| Redis 7.0.15 | levantado | — |
| Neo4j 5.24.0 | levantado | — |
| MongoDB 7.0.14 | **apagado en ese momento** | T-17, T-23, T-24, T-27 fallan |

Ese es el comportamiento correcto de la suite: MongoDB estaba realmente caído, y
los casos que lo necesitan lo dicen en vez de fingir. Con MongoDB levantado la
suite da **27/27**. El smoke test, en cambio, se adapta: usa el primer motor que
esté vivo y da 20/20 con Redis y Neo4j.

### El ciclo completo (T-23)

El requisito que más se nota en la corrección: **el respaldo tiene que
restaurar de verdad**. `T-23` ejecuta, contra el MongoDB de verdad:

1. siembra 120 documentos con un dato reconocible;
2. ejecuta `mongodump` de verdad y comprueba que generó el archivo;
3. **vuelve a medir** bytes y sha256 leyendo ese archivo;
4. borra toda la colección, para que restaurar tenga que reponerlo;
5. ejecuta `mongorestore` y **cuenta los documentos**;
6. comprueba que el último documento volvió **con su dato intacto**.

`T-24` hace lo contrario a propósito: daña un byte del artefacto y comprueba que
la verificación **falla**, porque una verificación que siempre da `true` no
verifica nada.

### Las cifras que se muestran son medidas, no estimadas

`T-27` es el contraste que respalda eso: cuenta los documentos **dentro del
`.bson`** (leyendo la longitud de cada documento BSON) y comprueba que coinciden
con los que tiene la base viva. Medido en esta máquina:

| Dato | Valor real mostrado |
| :- | :- |
| Registros respaldados | 1400 (contados en el artefacto) |
| Tamaño del artefacto | 31 594 bytes en 8 piezas |
| Duración del `mongodump` | ~30-35 ms |
| sha256 | 64 caracteres, recalculado del archivo |

Un respaldo son varias piezas (un `.bson.gz` por colección), así que el hash es
el **del conjunto**: cada pieza se hashea y se mezclan en orden, de modo que
cambiar el nombre de una pieza también rompe el hash.

### Una trampa medida en esta máquina

`mongorestore` **no acepta el directorio del dump**. Si se le pasa la carpeta,
imprime `don't know what to do with file ... skipping...`, restaura **0
documentos** y **sale con código 0**: un fallo silencioso que parece un éxito.
Por eso hay que pasarle cada archivo `.bson.gz` suelto. La suite lo hace
así, y comprobar que el conteo de documentos vuelve es justamente lo que
descubre este tipo de fallo.

## 6. Despliegue

**Aplicación en línea:** <https://dataforge-backup-si783.onrender.com>

Configurado en [`render.yaml`](render.yaml), documentado en [`DEPLOY.md`](DEPLOY.md).

- **Sin dependencias externas**: el `buildCommand` no instala nada.
- **Arranque**: `python3 app.py --host 0.0.0.0 --puerto $PORT --sin-navegador`.
- **Health check**: `/api/salud`.
- Render crea el servicio solo leyendo `render.yaml` (**automatización**).

En la nube pública los motores no están (el despliegue público no expone bases de
datos), así que la interfaz los muestra **apagados y con `n/d`**. Eso es el
comportamiento correcto: el despliegue demuestra que la app arranca y responde;
los respaldos con datos reales se miden en el laboratorio.

## 7. Estructura

```
app.py                    servidor HTTP (solo biblioteca estandar)
app/adaptador.py          puente con el nucleo: remedicion y errores de usuario
app/respaldos.py          el indice de estrategias (que protege / que no)
app/config.py             rutas y parametros de conexion
app/utileria.py           hash, cronometro, JSON atomico
app/adaptadores/          mongodb.py, redis.py, neo4j.py (un modulo por motor)
app/indice.py             indice JSON de los respaldos
app/datos_prueba.py       generador de datos para medir
app/ejecucion.py          orquestacion de las pruebas de restauracion
app/cli.py                linea de comandos
app/web/                  index.html, css/, js/
app/tests/casos.py        T-01..T-27
app/tests/smoke.py        recorrido en navegador
checks.py                 barrido de corrupcion de texto
CONTRATO.md               la interfaz entre el servidor y el nucleo
```

## 8. Nota sobre el núcleo

El servidor no reimplementa la lógica de respaldos: la pide a
`app/adaptadores/<motor>.py` mediante `app/adaptador.py`, que además **vuelve a
medir en disco** lo que el adaptador afirma y convierte cualquier problema en un
error de usuario (400). Ese contrato está escrito en
[`CONTRATO.md`](CONTRATO.md).
