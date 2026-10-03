# Despliegue — DataForge Backup

La consigna pide dos cosas que aquí están resueltas: **desplegar mediante
automatizaciones** y **publicar en nube pública**. Este documento explica qué se
despliega, cómo se verifica y qué se ve una vez arriba.

---

## 1. Qué se despliega

DataForge Backup es un servidor Python **sin dependencias externas**: la capa
HTTP es `http.server` de la biblioteca estándar y la interfaz es una página
estática que consume la API. No hay `requirements.txt` porque no hay nada que
`pip install`.

| Pieza | Dónde vive | Dependencias |
| :- | :- | :- |
| Servidor HTTP | `app.py` | biblioteca estándar |
| API de respaldos | `app/nucleo/` | drivers de los motores |
| Interfaz | `app/web/` | HTML, CSS y JS sin framework |
| Pruebas | `app/tests/` | estándar + Playwright |

## 2. Por qué Render

Los alojamientos de archivos estáticos (GitHub Pages, Surge) **no ejecutan
Python**: solo sirven archivos. Esta aplicación es un proceso que escucha en un
puerto, así que necesita un hosting que corra procesos. Render es el equivalente
correcto, y además lee `render.yaml` del repositorio y crea el servicio solo.

| Servicio | Qué habría que hacer | Costo |
| :- | :- | :- |
| **Render** (elegido) | Crear cuenta y apuntar al repo; lee `render.yaml` | Plan gratuito |
| Railway | Conectar el repo, `command: python3 app.py` | Plan gratuito limitado |
| Fly.io | `fly.toml` con `command = "python3 app.py"` | Plan gratuito limitado |

**Lo único que requiere una cuenta es la cuenta de Render.** El repositorio ya
está en GitHub y la configuración de despliegue ya está en el repositorio, así
que después de crear la cuenta el despliegue es un clic.

## 3. Cómo se despliega

1. Crear una cuenta en <https://render.com> (plan gratuito).
2. En el panel: **New → Blueprint**, y apuntar a este repositorio.
3. Render lee [`render.yaml`](render.yaml) y crea el servicio:
   - **no instala nada** (`buildCommand` solo comprueba la versión de Python);
   - **arranca** con `python3 app.py --host 0.0.0.0 --puerto $PORT --sin-navegador`;
   - **health check** en `/api/salud`.
4. En unos minutos queda en `https://dataforge-backup-si783.onrender.com`.

Dos detalles que no son opcionales:

- `--host 0.0.0.0` es obligatorio. Con `127.0.0.1` el proceso escucha solo en la
  máquina de Render y el servicio se declara caído.
- El puerto se lee de `$PORT`, que es quien lo asigna Render; escribir un puerto
  fijo lo deja conflictivo con lo que Render abre.

## 4. Cómo se verifica el despliegue

```bash
curl -s https://dataforge-backup-si783.onrender.com/api/salud
```

Debe responder HTTP 200 con esta forma:

```json
{"ok": true, "servicio": "dataforge-backup", "version": "1.0",
 "motores_total": 3, "estrategias": 5, "activo_desde": 3.2}
```

`/api/salud` **no consulta los motores**: es el health check y se llama cada pocos
segundos, así que responde en milisegundos sin abrir ni un socket. Que el
servidor esté vivo no significa que las bases de datos estén encendidas.

Para el estado real de los motores está `/api/motores`, que sí los consulta uno a
uno. En la nube pública los tres saldrán **apagados con `n/d`**: MongoDB, Redis
y Neo4j corren en la máquina del laboratorio, no en Render, y Render no expone
puertos de entrada a bases de datos. La interfaz lo dice en pantalla en vez de
fingir lo contrario: por eso cada motor muestra su estado real y su versión, o
`n/d` cuando no se pudo consultar.

Lo que **sí** demuestra el despliegue en la nube es que la aplicación arranca,
sirve la interfaz y responde la API. Los respaldos con datos reales se miden en
la máquina del laboratorio, donde sí están los motores.

## 5. El requisito de automatización

La consigna pide automatizar. Además del despliegue con *Blueprint* (que es
automatización: Render crea el servicio leyendo el repositorio, sin consola),
el repositorio incluye:

| Archivo | Qué automatiza |
| :- | :- |
| `render.yaml` | La creación del servicio completo: build, arranque y health check |
| `.github/workflows/pruebas.yml` | Corre la suite de pruebas en cada `push` |
| `checks.py` | Barrido de corrupción de texto sobre cada archivo |

La automatización **falla si el código falla**: la suite sale con código distinto
de 0 y el workflow se queda en rojo. No es decorativa.

## 6. Comandos

```bash
python3 app.py --sin-navegador --puerto 8080   # aplicación en local
python3 app/tests/casos.py                     # 26 casos numerados
python3 app/tests/smoke.py                     # recorrido real en navegador
python3 checks.py app.py                        # barrido de un archivo
```

## 7. Nota sobre el plan gratuito

El servicio se duerme tras unos minutos sin uso y tarda unos segundos en
despertar en la primera petición. Para la presentación, abrir la URL un par de
minutos antes. Si el despliegue se paga, el servicio no se duerme y el
health check de `/api/salud` lo confirma en cada despliegue.