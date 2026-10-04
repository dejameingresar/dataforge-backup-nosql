# Guion del video · Actividad Grupal · Base de Datos II · máximo 5 minutos

**Duración objetivo: 4 min 30 s.** Margen bajo el límite.

Grabación de pantalla con voz en off. Todo lo de este guion está verificado
contra el repositorio y contra la aplicación desplegada.

> **Dato importante para grabar:** la aplicación en la nube
> (<https://dataforge-backup-si783.onrender.com>) **arranca y responde, pero los
> tres motores aparecen apagados**, porque un servicio público no expone puertos
> de base de datos. Los respaldos con datos reales se miden en el laboratorio.
> Si en el video se ven los motores en `n/d`, **eso es lo correcto**, y es
> justo lo que hay que explicar.

---

## 0:00 — Apertura

**Voz:**
> El enunciado pedía estrategias de copia de seguridad excluyendo MS SQL
> Server. En este proyecto la exclusión se cumple en el código, no solo en el
> texto: la API rechaza cualquier motor que no sea MongoDB, Redis o Neo4j, y
> hay un caso de prueba que lo verifica.

**Pantalla:** el repositorio en GitHub.

---

## 0:25 — La aplicación y por qué no tiene dependencias

**Voz:**
> La aplicación es un servidor HTTP que usa solo la biblioteca estándar de
> Python. No hay `requirements.txt` porque no hay nada que instalar. Eso no es
> una simplificación: es lo que permite desplegar sin una cadena de
> dependencias que pueda romperse.

**Pantalla:** `app.py` y `render.yaml`.

```bash
# el servicio arranca con un solo comando
python3 app.py --host 0.0.0.0 --puerto $PORT --sin-navegador
```

**Voz:**
> Y el despliegue es automático: Render lee el `render.yaml` del repositorio y
> se crea solo, con `autoDeploy` y health check en `/api/salud`.

---

## 0:55 — Comprobación en vivo de la API

**Voz:**
> Antes de hablar de estrategias, comprobamos que la aplicación responde de
> verdad. Esto no es una captura: es la API pública.

**Pantalla:** terminal.

```bash
curl -s https://dataforge-backup-si783.onrender.com/api/salud
```

**Voz:**
> Dice tres motores, cinco estrategias. Ahora el endpoint que consulta los
> motores de verdad, uno por uno.

```bash
curl -s https://dataforge-backup-si783.onrender.com/api/motores
```

**Pantalla:** la respuesta, con los tres en `n/d`.

> Salen apagados, y **no vamos a disimularlo**: un servicio público no expone
> puertos de base de datos, así que no hay motor al que conectarse. Lo que esta
> aplicación demuestra en la nube es que arranca y responde; las cifras medidas
> son del laboratorio. Un `n/d` en la pantalla es correcto; un test verde sin
> motor detrás no lo sería.

---

## 1:30 — Los tres motores y las seis estrategias

**Voz:**
> En el laboratorio trabajamos con tres motores reales, y con seis estrategias
> de respaldo, dos por motor.

**Pantalla:** el índice de respaldos en el repositorio
(`indice/indice_respaldos.json`).

| Motor | Estrategia | Artefacto | Round-trip medido |
| --- | --- | --- | --- |
| MongoDB | `logico_mongodump` | `*.bson.gz` | 1400 → 1400 docs |
| MongoDB | `fisico_wiredtiger` | copia del `dbpath` | 1400 → 1400 docs |
| Redis | `rdb_dump` | `dump.rdb` | 240 → 240 claves |
| Redis | `aof_append` | `appendonlydir/` | 240 → 240 claves |
| Neo4j | `dump_binario` | `neo4j.dump` | 200/594 → 200/594 |
| Neo4j | `exportacion_cypher` | JSON Lines | 200/594 → 200/594 |

**Voz:**
> Seis de seis restauraron exactamente lo que había.

**Voz (matizando):**
> Y una advertencia que está escrita en el propio índice: son duraciones de una
> sola ejecución en una máquina de escritorio. Sirven para comparar estrategias
> entre sí, no como referencia de un servidor en producción.

---

## 2:05 — El hallazgo que ningún manual avisa

**Voz:**
> Pero lo que más aporta de este proyecto no está en la tabla. Es un fallo que
> no aparece en la documentación.

**Pantalla:** terminal, el mensaje real de `mongorestore`.

```
don't know what to do with file ... skipping...
```

**Voz:**
> `mongorestore` **no acepta el directorio del dump**. Si le pasas la carpeta
> completa, salta esa línea, **restaura cero documentos** y **sale con código de
> salida cero**.

> Es el peor fallo posible en un respaldo: parece un éxito. Un pipeline que
> comprueba el código de salida registra un respaldo íntegro que no contiene
> nada.

**Voz:**
> Por eso el ciclo de prueba del proyecto es siempre el mismo: se genera el
> respaldo, se mide su hash, se destruyen los datos, se restaura… y **se cuenta
> lo que volvió**. Ese último paso es el que separa un respaldo de una intención.

---

## 2:50 — El fallo que casi arruina el proyecto

**Voz:**
> Hay un segundo fallo, y este lo cometimos nosotros.

**Pantalla:** la tabla del índice, la fila de `exportacion_cypher`.

**Voz:**
> En un grafo puede haber varias aristas del mismo tipo entre el mismo par de
> nodos, con propiedades distintas. `MERGE` las funde en una sola. Al medir el
> round-trip faltaban cuatro relaciones: **590 de 594**.

> 590 de 594 parece "casi bien". Ese es el momento exacto en que un respaldo mal
> verificado se convierte en un respaldo mal documentado: bastaba relajar la
> comparación un 0,7 % para que el número cuadrara y el defecto quedara oculto.

**Voz:**
> La tentación era relajar el umbral. Con `CREATE` en lugar de `MERGE`, las 594
> se conservan. Relajar la comparación habría convertido un defecto real en un
> falso positivo.

---

## 3:30 — Qué no protege cada estrategia

**Voz:**
> La columna más útil de una estrategia de respaldo no es la que dice qué
> protege. Es la que dice **qué no**.

**Pantalla:** la interfaz, pestaña de estrategias.

| Estrategia | Qué NO protege |
| --- | --- |
| Completo | La caída del disco que lo contiene; un borrado posterior |
| Incremental | Depende de todos los completos anteriores: falta uno y se rompe la cadena |
| Diferencial | Los cambios acumulados desde el completo |
| Instantánea | Corrupción posterior del archivo |
| Replicación | **No protege contra el borrado**: un `DROP` se replica al instante |

**Voz:**
> La última fila es la más contraintuitiva. La replicación se suele vender como
> respaldo y no lo es: multiplica la disponibilidad y **duplica el error**.

---

## 4:00 — La automatización que sí puede ponerse en rojo

**Voz:**
> Y como en todo el trabajo del equipo, la automatización tenía un problema
> escondido.

**Pantalla:** `pruebas.yml`, el paso de la suite.

**Voz:**
> Ese paso terminaba en `exit 0`. O sea: **ningún fallo podía poner el workflow
> en rojo**, aunque el comentario tres líneas más arriba afirmara exactamente
> lo contrario. Cinco ejecuciones, todas en verde, sin comprobar nada.

**Pantalla:** `clasificar_ci.py`.

> Ahora la clasificación vive en un script aparte, que separa los dos casos que
> se estaban mezclando: los fallos que dependen de un motor —que el runner de
> GitHub Actions no tiene— y los fallos de código, que sí ponen el job en rojo.

**Pantalla:** la última ejecución en Actions, en verde, con el resumen.

> 27 casos, 21 exitosos, 6 fallidos, 78 % de cobertura. Los seis fallos están
> atribuidos a los motores ausentes, y eso está escrito en el log, no escondido.

---

## 4:30 — Cierre

**Voz:**
> Resultado: seis estrategias medidas sobre tres motores reales, seis de seis
> restaurando lo que tenían.

> Tres conclusiones. Primero: un respaldo no es un archivo, es una cadena —
> backup, hash, restauración y conteo. Segundo: **el código de salida no es
> evidencia**; los dos fallos más caros de este proyecto salían con cero.
> Tercero: elegir una tecnología obliga a aceptar sus compromisos, y lo que
> aquí se estudió fue justo eso: qué es lo que cada motor no te deja hacer.

**Pantalla:** el repositorio, la aplicación y el artículo del equipo.

---

## Notas de producción

- **Duración estimada:** 4 min 30 s. Margen bajo el límite de 5.
- **Si los motores no están levantados**, no pasa nada: el guion asume que en la
  nube aparecen apagados y eso es lo que hay que enseñar.
- **No grabes la pantalla de los motores en `n/d` sin explicar.** Parecería un
  fallo; es el comportamiento correcto.
- **Las cifras de la tabla (1400 docs, 590/594, 78 %)** están en
  `indice/indice_respaldos.json` y en el log de Actions. Si alguno cambió,
  actualiza el guion antes de grabar.
- **Terminal en 1080p** escalada al 80 %.
- **Sin audio de fondo** en los segmentos de terminal.
- **Comprobación antes de grabar:**
  ```bash
  curl -s https://dataforge-backup-si783.onrender.com/api/salud
  ```
  Si no responde `ok: true`, espera a que Render levante el servicio: en plan
  gratuito se apaga por inactividad y tarda unos segundos en despertar.