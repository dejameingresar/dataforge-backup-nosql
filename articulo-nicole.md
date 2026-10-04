# Por qué no usamos MS SQL Server: seis estrategias de respaldo que sí se pueden verificar

El enunciado pedía estrategias de copia de seguridad **excluyendo MS SQL Server**.
En nuestro equipo la exclusión seßeumplió en el código, no solo en el texto: la API
**rechaza** cualquier motor que no sea MongoDB, Redis o Neo4j, y hay un caso de
prueba que lo comprueba.

Pero dejar SQL Server fuera es la parte fácil. Lo difícil resultó ser la otra
pregunta: **¿qué estrategia de respaldo puedes verificar de verdad?**

<!-- more -->

## Los enlaces del proyecto

- **Repositorio público con el código, la suite y el índice de respaldos:**
  <https://github.com/dejameingresar/dataforge-backup-nosql>
- **Aplicación desplegada en la nube (Render, creada desde `render.yaml`):**
  <https://dataforge-backup-si783.onrender.com>
- **Artículo complementario del equipo:** [*Seis estrategias de respaldo para
  bases de datos NoSQL, y qué falla de verdad*](https://dev.to/dejameingresar/seis-estrategias-de-respaldo-para-bases-de-datos-nosql-y-que-falla-de-verdad-5hce)

## La exclusión, aplicada de verdad

SQL Server no aparece en ninguna decisión de diseño del proyecto. Aparece en dos
lugares, y en los dos para rechazarlo:

```python
# app/tests/casos.py — el motor prohibido tiene que dar 400, no 500
for motor in ("", "postgres", "MONGODB", "mssql"):
    ...
# smoke.py — "un motor prohibido (MS SQL Server) se rechaza con 400"
estado, cuerpo = post(f"{url}api/respaldar/mssql/completo")
```

Eso no es simbólico. Significa que la API tiene una noción de "motor no
soportado" separada de "motor caído", y que un error de usuario siempre devuelve
400 en lugar de romper el servidor.

## Por qué no MS SQL Server: cuatro razones que se comprobaron

No lo excluimos por gusto ni por mods. Lo excluimos porque su modelo de respaldo
no encaja con lo que este proyecto tenía que demostrar.

**1. El respaldo de SQL Server depende de una instancia viva, y su artefacto
depende de la versión que lo creó.** Un `.bak` se genera con `BACKUP DATABASE` y
se restaura con `RESTORE`, pero el archivo lleva la versión del motor que lo
produjo: restaurarlo exige una edición compatible. En un proyecto cuyo objetivo
es que el respaldo sobreviva a la caída del motor, depender de que el motor
volvió con la misma edición es una restricción que no se puede verificar desde
fuera.

**2. Su automatización exige más superficie que un solo binario.** Un respaldo
programado de SQL Server necesita la instancia, las credenciales, un destino
escribible y la tarea configurada. Las seis estrategias que implementamos se
ejecutan con un comando cada una:

```bash
mongodump --uri mongodb://127.0.0.1:27017/dataforge_backup --out DIR --gzip
redis-cli -p 6379 --rdb dump.rdb
neo4j-admin database dump neo4j --to-path=DESTINO
```

Una estrategia que necesita cuatro piezas coordinadas no se puede probar en un
`for` loop. Una que necesita un comando, sí.

**3. No tiene equivalente conceptual de "copia en frío del almacenamiento".**
Las estrategias físicas que sí implementamos —copiar los archivos del motor con
el servicio detenido— no existen como operación propia en SQL Server: el
`.mdf`, los archivos de log y el backup son cosas distintas y la foto del
almacenamiento no es una operación respaldada por el motor. La distinción
lógico/físico es justamente la que queríamos poder comparar.

**4. Los seis compromisos que medimos no aplican a un motor relacional.** Cada
restricción que encontramos es del motor NoSQL, y son las que hacen un respaldo
interesante de estudiar:

| Compromiso | Dónde aparece |
| --- | --- |
| `mongorestore` **no acepta un directorio**: con la carpeta restaura 0 documentos y devuelve código `0` | MongoDB |
| `CONFIG SET dir` falla con `can't set protected config`; hay que usar `redis-cli --rdb` | Redis 7 |
| `SET n:Etiqueta` es error de sintaxis sin el plugin APOC | Neo4j 5 |
| `MERGE` **colapsa aristas repetidas** entre el mismo par de nodos | Neo4j |
| Community **exige el motor detenido** para `database dump` | Neo4j |
| `database backup` no existe en Community (es de Enterprise) | Neo4j |

Ninguno de esos compromisos tiene equivalente en el modelo relacional. Son
precisamente los que hacen que "verificar un respaldo" no sea sin automático.

## El hallazgo que ningún manual avisa: el respaldo que "tiene éxito" y no restaura nada

Este es el resultado que más aporta del proyecto, y no aparece en la
documentación.

`mongorestore` **no acepta el directorio del dump**. Si se le pasa la carpeta
completa, imprime un mensaje apodbable:

```
don't know what to do with file ... skipping...
```

…restaura **cero documentos** y **sale con código de salida `0`**.

Es el peor fallo posible en un respaldo: parece un éxito. Un pipeline que
comprueba el código de salida registra un respaldo íntegro que no contiene nada.

La única forma de detectarlo es **contar lo que vuelve**. Por eso el ciclo de
prueba del proyecto es siempre el mismo, sin excepción:

1. se genera el respaldo;
2. se mide su SHA-256;
3. se destruyen los datos;
4. se restaura;
5. **se cuenta lo que volvió**.

Ese quinto paso es el que separa un respaldo de una intención.

## Seis estrategias, medidas

Cada número de esta tabla salió de una corrida real. El artefacto se vuelve a
leer desde disco para medirlo y para hashearlo.

| Motor | Estrategia | Artefacto | Tamaño | Round-trip medido |
| --- | --- | --- | ---: | --- |
| MongoDB | `logico_mongodump` | `*.bson.gz` | 30.8 KiB | 1400 → 1400 docs |
| MongoDB | `fisico_wiredtiger` | copia del `dbpath` | 1.11 MiB | 1400 → 1400 docs |
| Redis | `rdb_dump` | `dump.rdb` | 11.3 KiB | 240 → 240 claves |
| Redis | `aof_append` | `appendonlydir/` | 11.4 KiB | 240 → 240 claves |
| Neo4j | `dump_binario` | `neo4j.dump` | 509.1 KiB | 200/594 → 200/594 |
| Neo4j | `exportacion_cypher` | JSON Lines | 104.0 KiB | 200/594 → 200/594 |

**6 de 6** restauraron exactamente lo que había.

Una advertencia sobre esas cifras: son de una sola ejecución en una máquina de
escritorio (Linux 6.8, Python 3.11.15). Sirven para **comparar estrategias entre
sí**, no como referencia de un servidor en producción. El propio índice de
respaldos lo declara.

### La physical es más rápida de escribir y te quita el servicio

Las dos estrategias físicas —`fisico_wiredtiger` y `dump_binario`— exigen el
motor **detenido**. En producción eso no es un detalle de implementación: es una
decisión de arquitectura, porque significa una ventana sin servicio.

Para la misma base de grafos, la exportación lógica corría **en caliente**:

| Estrategia Neo4j | Duración | ¿Motor encendido? |
| --- | ---: | :- |
| `exportacion_cypher` (JSON Lines) | 420 ms | Sí |
| `dump_binario` (`.dump`) | 3.48 s | No |

Ocho veces más rápida, pero con el servicio caído. Elijes según qué te duele
más.

## La tentación que casi arruinó el proyecto

`MERGE` funde en una sola las aristas repetidas entre el mismo par de nodos con
propiedades distintas. Al medir el round-trip faltaban 4 relaciones: **590 de
594**.

590 de 594 parece "casi bien". Ese es el momento exacto en que un respaldo mal
verificado se convierte en un respaldo mal documentado: bastaba relajar la
comparación un 0.7 % para que el número cuadrara y el defecto quedara oculto.

La tentación era relajar el umbral. Convertir un defecto real en un falso
positivo es exactamente lo que hace que un sistema de respaldo pierda
credibilidad. Con `CREATE` en lugar de `MERGE`, las 594 se conservan.

## Qué no protege cada estrategia

Esta tabla es la que más útil resulta y la que menos se documenta. Una estrategia
que solo declara ventajas no está documentada, está vendida.

| Estrategia | Qué protege | Qué NO protege |
| --- | --- | :- |
| Completo | Todo el contenido, sin depender de nada anterior | La caída del disco que lo contiene; un borrado posterior |
| Incremental | Lo que cambió desde el último completo | Depende encadenadamente de todos los completos: falta uno y se rompe la cadena |
| Diferencial | Lo que cambió desde el último de su misma estrategia | Los cambios acumulados desde el completo |
| Instantánea | El estado en un instante, sin detener el motor | Corrupción posterior del archivo; exige almacenamiento transaccional |
| Replicación | Disponibilidad continua: otra copia siempre viva | **No protege contra el borrado**: un `DROP` se replica al instante |

La última fila es la más contraintuitiva. Replication se suele vender como
respaldo y no lo es: multiplica la disponibilidad y **duplica el error**.

## Verificación y despliegue automatizado

La suite (`app/tests/casos.py`, casos T-01..T-27) corre en cada `push` mediante
`.github/workflows/pruebas.yml`, y la aplicación se crea sola en Render leyendo
[`render.yaml`](https://github.com/dejameingresar/dataforge-backup-nosql/blob/main/render.yaml),
con `autoDeploy` y health check en `/api/salud`.

Hay un punto que conviene decir con claridad: **los casos que necesitan motores
reales no pueden correr en el runner de GitHub Actions.** El runner no tiene
MongoDB, Redis ni Neo4j. La suite no inventa un resultado: esos casos fallan de
forma explícita y la verificación completa se hace contra los motores locales.

Es la misma lección del otro proyecto del equipo: un `n/d` en la pantalla es
correcto; un test verde sin motor detrás no lo sería.

En el despliegue público los tres motores aparecen como `n/d`, porque un servicio
público no expone puertos de base de datos. El despliegue demuestra que la
aplicación arranca y responde; las cifras medidas son del laboratorio.

## Conclusiones

1. **Un respaldo no es un archivo: es una cadena.** Backup, verificación por hash,
   restauración y conteo. Se salta un eslabón y el resultado no sirve.
2. **El código de salida no es evidencia.** Los dos fallos más caros de este
   proyecto salían con `0`. Contar lo que volvió sí lo es.
3. **La verificación tiene que ser estricta o no vale nada.** Relajar el umbral
   de comparación convierte un defecto real en un falso positivo.
4. **Cada familia de motor tiene su propia restricción**, y documentar **qué no
   protege** una estrategia es más útil que enumerar qué sí.
5. **Excluir una tecnología se demuestra en el código.** En este proyecto, SQL
   Server está en la lista de motores rechazados y hay una prueba que lo verifica,
   no una nota en el README.

Y el punto que resume todo lo demás: **elegir una tecnología obliga a aceptar sus
compromisos**. Elegimos estos tres motores porque sus límites son comprobables
con un comando y un `count`. Ahí es donde se puede demostrar que un respaldo
funciona, en vez de suponerlo.