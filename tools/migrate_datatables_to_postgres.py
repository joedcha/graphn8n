# -*- coding: utf-8 -*-
"""Migra las Data Tables de un proyecto n8n a tablas Postgres.

Motivo: las Data Tables se anclan al proyecto unicamente por `projectId` y no
tienen tabla de comparticion por entidad, asi que su control de acceso depende
del rol de proyecto (`project:admin/editor/viewer`), que en Community Edition
esta bloqueado por licencia (`feat:projectRole:admin`). Resultado: en un
proyecto de equipo el runtime sigue leyendo/escribiendo por id, pero la UI no
puede listarlas ni administrarlas. Pasarlas a Postgres quita la dependencia.

Que hace (todo de solo lectura contra n8n):
  1. Lista las data tables del proyecto y sus columnas.
  2. Pagina TODAS las filas de cada una via /api/v1/data-tables/{id}/rows.
  3. Genera SQL revisable: DDL + INSERTs + rollback.
  4. Opcional (--execute): lo aplica con psycopg2 si esta instalado.

NO escribe nada en n8n y NO borra las data tables originales: deja los datos
duplicados a proposito para que se puedan comparar antes de cortar.

Uso:
    python migrate_datatables_to_postgres.py --project SAlAMHOX1Vh672nQ --out ./migracion
    python migrate_datatables_to_postgres.py --project SAlAMHOX1Vh672nQ --out ./migracion \
        --execute --pg-dsn "host=... dbname=... user=... password=..."
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# n8n solo expone estos tipos de columna en data tables.
TIPOS_SQL = {
    'string': 'text',
    'number': 'double precision',
    'boolean': 'boolean',
    'date': 'timestamptz',
}
# Columnas de sistema que n8n agrega a cada fila y que conviene conservar.
COLS_SISTEMA = ('id', 'createdAt', 'updatedAt')
PALABRAS_RESERVADAS = {'user', 'order', 'group', 'table', 'select', 'from', 'where',
                       'default', 'check', 'column', 'constraint', 'references'}

# Los valores traen acentos y secuencias '\n' literales dentro de blobs JSON.
# Sin esto, un psql con standard_conforming_strings=off reinterpretaria los
# backslashes y un client_encoding distinto rompería los acentos.
CABECERA_SQL = """
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

"""


# ==============================================================================
# n8n (solo lectura)
# ==============================================================================
def cargar_env(ruta):
    if ruta and os.path.exists(ruta):
        with open(ruta, encoding='utf-8') as fh:
            for linea in fh:
                linea = linea.strip()
                if linea and not linea.startswith('#') and '=' in linea:
                    k, v = linea.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _ctx():
    if os.environ.get('N8N_VERIFY_TLS', 'true').strip().lower() == 'false':
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    return ssl.create_default_context()


def api_get(path):
    base = os.environ['N8N_BASE_URL'].rstrip('/')
    req = urllib.request.Request(
        base + path,
        headers={'X-N8N-API-KEY': os.environ['N8N_API_KEY'], 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=120) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode('utf-8', 'replace')[:300]
        raise RuntimeError('n8n {} -> HTTP {}: {}'.format(path, e.code, cuerpo)) from e


def listar_data_tables(project_id):
    out, cursor = [], None
    while True:
        q = '/api/v1/data-tables?limit=100' + ('&cursor=' + cursor if cursor else '')
        pag = api_get(q)
        out.extend(pag.get('data') or [])
        cursor = pag.get('nextCursor')
        if not cursor:
            break
    return [t for t in out if t.get('projectId') == project_id]


def traer_filas(table_id, pagina=250):
    """Pagina por cursor. El endpoint rechaza limit > 250 y no acepta skip/offset."""
    filas, cursor, vistos = [], None, set()
    while True:
        q_ = '/api/v1/data-tables/{}/rows?limit={}'.format(table_id, pagina)
        if cursor:
            q_ += '&cursor=' + urllib.parse.quote(cursor, safe='')
        pag = api_get(q_)
        lote = pag.get('data') or []
        for f in lote:
            # el cursor podria repetir filas si la tabla se escribe mientras se lee
            if f.get('id') not in vistos:
                vistos.add(f.get('id'))
                filas.append(f)
        cursor = pag.get('nextCursor')
        if not cursor or not lote:
            break
    return filas


# ==============================================================================
# SQL
# ==============================================================================
def ident(nombre):
    """Convierte un nombre de n8n ('SHERPA - MESSAGE TIPS') en identificador SQL."""
    s = re.sub(r'[^0-9a-zA-Z]+', '_', nombre).strip('_').lower()
    s = re.sub(r'_+', '_', s) or 'tabla'
    if s[0].isdigit():
        s = 't_' + s
    if s in PALABRAS_RESERVADAS:
        s += '_'
    return s[:63]                                # limite de Postgres


def q(nombre):
    return '"{}"'.format(nombre.replace('"', '""'))


def literal(valor, tipo):
    if valor is None or valor == '':
        # cadena vacia y NULL se distinguen: solo None es NULL
        if valor is None:
            return 'NULL'
    if tipo == 'boolean':
        return 'true' if valor in (True, 'true', 1, '1') else 'false'
    if tipo == 'number':
        try:
            return repr(float(valor))
        except (TypeError, ValueError):
            return 'NULL'
    return "'" + str(valor).replace("'", "''") + "'"


def ddl_de_tabla(tabla, esquema, nombre_sql, cols):
    lineas = ['CREATE TABLE IF NOT EXISTS {}.{} ('.format(q(esquema), q(nombre_sql))]
    campos = ['    {:<34} bigint PRIMARY KEY'.format(q('id'))]
    for c in cols:
        campos.append('    {:<34} {}'.format(q(ident(c['name'])), TIPOS_SQL.get(c['type'], 'text')))
    campos.append('    {:<34} timestamptz NOT NULL DEFAULT now()'.format(q('created_at')))
    campos.append('    {:<34} timestamptz NOT NULL DEFAULT now()'.format(q('updated_at')))
    lineas.append(',\n'.join(campos))
    lineas.append(');')
    lineas.append("COMMENT ON TABLE {}.{} IS 'Migrada desde n8n data table \"{}\" (id {})';".format(
        q(esquema), q(nombre_sql), tabla['name'].replace("'", "''"), tabla['id']))
    # secuencia para que los inserts nuevos sigan despues del ultimo id de n8n
    seq = ident(nombre_sql + '_id_seq')
    lineas.append('CREATE SEQUENCE IF NOT EXISTS {}.{};'.format(q(esquema), q(seq)))
    lineas.append("ALTER TABLE {}.{} ALTER COLUMN {} SET DEFAULT nextval('{}.{}');".format(
        q(esquema), q(nombre_sql), q('id'), q(esquema), q(seq)))
    return '\n'.join(lineas)


def inserts_de_tabla(esquema, nombre_sql, cols, filas, lote=200):
    if not filas:
        return '-- (sin filas)\n'
    destino = [q('id')] + [q(ident(c['name'])) for c in cols] + [q('created_at'), q('updated_at')]
    partes = []
    for i in range(0, len(filas), lote):
        vals = []
        for f in filas[i:i + lote]:
            v = [str(int(f['id']))]              # PK bigint: entero, no 2.0
            for c in cols:
                v.append(literal(f.get(c['name']), c['type']))
            v.append(literal(f.get('createdAt'), 'date'))
            v.append(literal(f.get('updatedAt'), 'date'))
            vals.append('  (' + ', '.join(v) + ')')
        partes.append('INSERT INTO {}.{} ({})\nVALUES\n{}\nON CONFLICT ({}) DO NOTHING;'.format(
            q(esquema), q(nombre_sql), ', '.join(destino), ',\n'.join(vals), q('id')))
    return '\n\n'.join(partes) + '\n'


# ==============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True, help='projectId de n8n')
    ap.add_argument('--schema', default='sherpa', help='esquema Postgres destino')
    ap.add_argument('--out', default='./migracion_datatables')
    ap.add_argument('--tables', help='filtro por nombre, separado por comas')
    ap.add_argument('--skip-empty', action='store_true',
                    help='omite las data tables sin filas (suelen ser intentos fallidos)')
    ap.add_argument('--env', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'bridge-python', '.env'))
    ap.add_argument('--execute', action='store_true', help='aplica el SQL (requiere psycopg2)')
    ap.add_argument('--pg-dsn', help='DSN de Postgres para --execute')
    args = ap.parse_args()

    cargar_env(args.env)
    if not os.environ.get('N8N_BASE_URL') or not os.environ.get('N8N_API_KEY'):
        sys.exit('Faltan N8N_BASE_URL / N8N_API_KEY')

    tablas = listar_data_tables(args.project)
    if args.tables:
        filtro = {t.strip().lower() for t in args.tables.split(',')}
        tablas = [t for t in tablas if t['name'].lower() in filtro]
    if not tablas:
        sys.exit('El proyecto {} no tiene data tables (o el filtro no coincide)'.format(args.project))

    os.makedirs(args.out, exist_ok=True)
    esquema = args.schema
    ddl = ['-- Migracion de n8n Data Tables -> Postgres',
           '-- Proyecto n8n: {}'.format(args.project),
           '-- Generado: {}'.format(datetime.now().isoformat(timespec='seconds')),
           '',
           CABECERA_SQL.strip(), '',
           'CREATE SCHEMA IF NOT EXISTS {};'.format(q(esquema)), '']
    rollback = ['-- Deshace la migracion. Solo borra el esquema creado; n8n queda intacto.']
    mapeo, total_filas, generados = [], 0, []

    for t in sorted(tablas, key=lambda x: x['name']):
        cols = sorted(t.get('columns') or [], key=lambda c: c.get('index', 0))
        filas = traer_filas(t['id'])
        if args.skip_empty and not filas:
            print('  omitida (vacia): {}'.format(t['name']))
            mapeo.append((t['name'], '(omitida: 0 filas)', t['id'], 0, len(cols)))
            continue
        nombre_sql = ident(t['name'])
        ddl.append(ddl_de_tabla(t, esquema, nombre_sql, cols))
        ddl.append('')
        rollback.append('DROP TABLE IF EXISTS {}.{};'.format(q(esquema), q(nombre_sql)))

        archivo = os.path.join(args.out, '02_datos_{}.sql'.format(nombre_sql))
        with open(archivo, 'w', encoding='utf-8') as fh:
            fh.write('-- {} filas desde la data table "{}" (id {})\n'.format(
                len(filas), t['name'], t['id']))
            fh.write(CABECERA_SQL)
            fh.write(inserts_de_tabla(esquema, nombre_sql, cols, filas))
        generados.append(os.path.basename(archivo))

        ultimo = max([f.get('id') or 0 for f in filas] or [0])
        ddl.append("SELECT setval('{}.{}', {}, true);  -- continua despues del ultimo id de n8n".format(
            q(esquema), q(ident(nombre_sql + '_id_seq')), max(ultimo, 1)))
        ddl.append('')
        mapeo.append((t['name'], '{}.{}'.format(esquema, nombre_sql), t['id'], len(filas), len(cols)))
        total_filas += len(filas)
        print('  {:<38} -> {}.{:<34} {} filas'.format(
            t['name'][:38], esquema, nombre_sql, len(filas)))

    with open(os.path.join(args.out, '01_ddl.sql'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(ddl))
    with open(os.path.join(args.out, '99_rollback.sql'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(rollback) + '\n')

    with open(os.path.join(args.out, 'MAPEO.md'), 'w', encoding='utf-8') as fh:
        fh.write('# Mapeo data table -> Postgres\n\n')
        fh.write('Proyecto n8n `{}` · esquema destino `{}` · {} filas\n\n'.format(
            args.project, esquema, total_filas))
        fh.write('| Data table (n8n) | Tabla Postgres | id n8n | Filas | Columnas |\n')
        fh.write('|---|---|---|---:|---:|\n')
        for n, s, i, f, c in mapeo:
            fh.write('| {} | `{}` | `{}` | {} | {} |\n'.format(n, s, i, f, c))
        fh.write('\n## Orden de aplicacion\n\n```\npsql -f 01_ddl.sql\n')
        for g in generados:
            fh.write('psql -f {}\n'.format(g))
        fh.write('```\n\nPara revertir: `psql -f 99_rollback.sql` (no toca n8n).\n')
        fh.write('\n## Equivalencia de nodos\n\n')
        fh.write('| Data Table (n8n) | Postgres |\n|---|---|\n')
        fh.write('| Get row(s) | Execute Query: `SELECT ... WHERE ...` |\n')
        fh.write('| Insert row | Insert, o Execute Query con `RETURNING id` |\n')
        fh.write('| Update row(s) | Update (matching column), o `UPDATE ... WHERE` |\n')
        fh.write('| Upsert row(s) | `INSERT ... ON CONFLICT (col) DO UPDATE SET ...` |\n')
        fh.write('| Delete row(s) | Execute Query: `DELETE FROM ... WHERE ...` |\n')
        fh.write('| If row does not exist | `SELECT EXISTS(...)` + nodo If |\n')
        fh.write('\nOjo: el nodo Data Table devuelve `id`, `createdAt` y `updatedAt` ')
        fh.write('en cada fila. Las tablas generadas conservan esas tres columnas ')
        fh.write('(`id`, `created_at`, `updated_at`) para no romper expresiones que las usen, ')
        fh.write('pero **renombradas a snake_case**: hay que ajustar las expresiones que ')
        fh.write('referencien `createdAt`/`updatedAt`.\n')

    print('\n{} tablas, {} filas. SQL en: {}'.format(len(generados), total_filas, args.out))

    if args.execute:
        if not args.pg_dsn:
            sys.exit('--execute requiere --pg-dsn')
        try:
            import psycopg2
        except ImportError:
            sys.exit('psycopg2 no esta instalado: aplica los .sql con psql manualmente')
        conn = psycopg2.connect(args.pg_dsn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for archivo in ['01_ddl.sql'] + generados:
                    with open(os.path.join(args.out, archivo), encoding='utf-8') as fh:
                        cur.execute(fh.read())
                    print('  aplicado: {}'.format(archivo))
            conn.commit()
            print('OK, commit hecho.')
        except Exception:
            conn.rollback()
            print('ERROR: rollback hecho, no se aplico nada.')
            raise
        finally:
            conn.close()


if __name__ == '__main__':
    main()
