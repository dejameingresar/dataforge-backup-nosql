#!/usr/bin/env python3
"""Barrido de corrupcion: CJK, cirilico, y splices de forma.

Se ejecuta sobre cada archivo recien escrito, en el mismo paso que la escritura,
porque la corrupcion de glifos no lanza error: el archivo simplemente lleva una
palabra rara entre hundreds correctas.

    python3 checks.py <archivo> [<archivo> ...]
"""
import re
import sys

CJK = re.compile(r'[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]')
CYR = re.compile(r'[\u0400-\u04ff]')
# Palido: espacios de otros alfabetos, comillas exoticas, guiones raros
RARO = re.compile(r'[\u00a0\u2000-\u200f\u2028-\u202f\u205f\u3000]')
MIXTO = re.compile(r'^[a-záéíóúüñ]+[A-ZÁÉÍÓÚÜÑ][a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+$')
CAMEL = re.compile(r'[a-záéíóúüñ]{2}[A-ZÁÉÍÓÚÜÑ]')
ALFA_NUM = re.compile(r'^[a-záéíóúüñ]+\d[a-záéíóúüñ]+$|^[a-záéíóúüñ]{3,}\d$')

# Nombres propios y tecnicismos que el barrido debe tolerar
PERMITIDOS = {
    'DataForge', 'NoSQL', 'PostgreSQL', 'GitHub', 'MongoDB', 'Redis',
    'OpenAPI', 'JSONSchema', 'JavaScript', 'TypeScript', 'Python',
    'FastAPI', 'WebSocket', 'NewSQL', 'ProyectoFinal', 'PropuestaProyecto',
    'Proyecto', 'Factibilidad', 'Especificacion', 'Requerimientos', 'Vision',
    'Arquitectura', 'Software', 'Informe', 'python3',
    # Proyecto de respaldos: motores, drivers y clases de la biblioteca estandar.
    # Sin estas entradas el barrido marca codigo legitimo como corrupcion: el
    # guion de este script es distinguir glifos raros, no reescribir Python.
    'Neo4j', 'Render', 'Playwright', 'sha256', 'sha', 'gzip', 'bson',
    'mongodump', 'mongorestore', 'mongoexport', 'mongoimport',
    'BaseHTTPRequestHandler', 'ThreadingHTTPServer', 'BaseHTTPRequest',
    'HTTPStatus', 'KeyboardInterrupt', 'ValueError', 'TypeError',
    'ArgumentParser', 'JSONDecodeError', 'ErrorUsuario', 'json',
    'ImportError', 'KeyError', 'IndexError', 'AttributeError',
    'neo4j', 'sqlite', 'sqlite3', 'psycopg', 'pgpass',
    # Funciones de la interfaz: dos palabras inglesas juntas en camel case.
    'cargaMotores', 'cargaEstrategias',
    # Nombres propios del almacenamiento y banderas de CLI que aparecen en el
    # codigo de los adaptadores: son identificadores reales, no texto en
    # ingles pegado por error dentro del espanol.
    'WiredTiger', 'PyMongoError', 'authSchema', '--nsFrom', '--nsTo',
    'RuntimeError', 'GraphDatabase', 'ServiceUnavailable', 'MongoClient',
    'sizeStorer', 'FileNotFoundError',
    # Claves oficiales de la especificacion Blueprint de Render y de GitHub
    # Actions: son nombres de un estandar publico, no prosa mal formada.
    'buildCommand', 'startCommand', 'healthCheckPath', 'autoDeploy', 'envVars',
}


def revisa(ruta):
    with open(ruta, encoding='utf-8', errors='replace') as fh:
        texto = fh.read()
    problemas = []

    for n, linea in enumerate(texto.split('\n'), 1):
        for c in CJK.finditer(linea):
            problemas.append((n, 'CJK/CIRILICO', linea.strip()[max(0, c.start() - 25):c.end() + 25]))
        for c in CYR.finditer(linea):
            problemas.append((n, 'CJK/CIRILICO', linea.strip()[max(0, c.start() - 25):c.end() + 25]))
        for c in RARO.finditer(linea):
            problemas.append((n, 'ESPACIO_RARO', repr(c.group())))

    for w in re.findall(r'[^\s]+', texto):
        wk = w.strip('.,;:!?()«»""\'—–·[]()|*_`')
        if not wk or wk in PERMITIDOS:
            continue
        # Acceso a propiedad (algo.textContent, div.innerHTML): en JS y Python es
        # sintaxis legitima y no puede ser una palabra pegada por error.
        if '.' in wk and not CJK.search(wk) and not CYR.search(wk):
            continue
        if MIXTO.match(wk):
            problemas.append((0, 'MIXTO', wk))
        elif CAMEL.search(wk) and not any(wk.startswith(p) or p in wk for p in PERMITIDOS):
            # Ignorar tecnicismos con digito o parentesis: KeyError(f, etc.
            if re.search(r'[0-9(]', wk):
                continue
            problemas.append((0, 'CAMEL', wk))
        elif ALFA_NUM.match(wk):
            problemas.append((0, 'ALFA_NUM', wk))

    # Deduplicar y contar
    unicos, vistos = [], set()
    for p in problemas:
        if p[2] not in vistos:
            vistos.add(p[2])
            unicos.append(p)
    return unicos


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    total = 0
    for ruta in argv[1:]:
        problemas = revisa(ruta)
        if problemas:
            total += len(problemas)
            print(f"  {ruta}: {len(problemas)} hallazgos")
            for n, tipo, frag in problemas[:12]:
                where = f"linea {n}" if n else "token"
                print(f"     [{tipo}] {where}: {frag[:90]}")
    if total:
        print(f"BARRIDO: {total} hallazgos. Corregir antes de seguir.")
        return 1
    print("BARRIDO: limpio")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))