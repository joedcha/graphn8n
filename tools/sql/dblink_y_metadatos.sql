-- ============================================================================
-- 1) Copiar filas de una tabla de OTRA base de datos a una tabla local
-- ============================================================================
-- Todo esto se ejecuta en la base DESTINO. Reemplazar los <...>.
-- Requisito: la extension se crea en la base DESTINO, y el servidor destino
-- tiene que alcanzar por red al origen (puerto 5432 abierto entre ambos).

CREATE EXTENSION IF NOT EXISTS dblink;

-- --- Opcion A: consulta suelta (la mas directa) -----------------------------
-- La lista de tipos del AS t(...) NO es opcional y tiene que coincidir con lo
-- que devuelve el SELECT remoto, en el mismo orden. Si no coincide, el error
-- aparece en ejecucion, no al planificar.

INSERT INTO <esquema_destino>.<tabla_destino> (col_a, col_b, col_c)
SELECT col_a, col_b, col_c
FROM dblink(
        'host=<host_origen> port=5432 dbname=<bd_origen> user=<usuario> password=<clave>',
        'SELECT col_a, col_b, col_c FROM <esquema_origen>.<tabla_origen>'
     ) AS t(col_a text, col_b integer, col_c timestamptz)
ON CONFLICT (<columna_pk>) DO NOTHING;   -- hace el script re-ejecutable

-- --- Opcion B: conexion con nombre (para varias consultas seguidas) ---------
-- Evita repetir la cadena de conexion (y la clave) en cada consulta.

SELECT dblink_connect('origen',
    'host=<host_origen> port=5432 dbname=<bd_origen> user=<usuario> password=<clave>');

INSERT INTO <esquema_destino>.<tabla_destino> (col_a, col_b)
SELECT col_a, col_b
FROM dblink('origen', 'SELECT col_a, col_b FROM <esquema_origen>.<tabla_origen>')
     AS t(col_a text, col_b integer);

SELECT dblink_disconnect('origen');

-- --- Carga incremental (para no recopiar todo cada vez) ---------------------
-- Trae solo lo nuevo desde la ultima marca local. Ojo: el WHERE va DENTRO de
-- la cadena de la consulta remota, si no se trae la tabla entera y filtra aca.

INSERT INTO <esquema_destino>.<tabla_destino> (id, col_a, actualizado_en)
SELECT id, col_a, actualizado_en
FROM dblink('origen', format(
        'SELECT id, col_a, actualizado_en FROM <esquema_origen>.<tabla_origen>
         WHERE actualizado_en > %L',
        (SELECT COALESCE(MAX(actualizado_en), '-infinity'::timestamptz)
         FROM <esquema_destino>.<tabla_destino>)
     )) AS t(id bigint, col_a text, actualizado_en timestamptz)
ON CONFLICT (id) DO UPDATE
   SET col_a = EXCLUDED.col_a,
       actualizado_en = EXCLUDED.actualizado_en;


-- ============================================================================
-- 1-bis) Alternativa recomendada si esto se va a repetir: postgres_fdw
-- ============================================================================
-- Mismo resultado, pero la tabla remota queda como una tabla mas: el planner
-- puede empujar filtros y joins al origen (dblink siempre trae y luego filtra),
-- y la clave queda en el USER MAPPING en vez de escrita en cada consulta.

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE SERVER origen_srv
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host '<host_origen>', port '5432', dbname '<bd_origen>');

CREATE USER MAPPING FOR CURRENT_USER
    SERVER origen_srv
    OPTIONS (user '<usuario>', password '<clave>');

CREATE SCHEMA IF NOT EXISTS staging;

IMPORT FOREIGN SCHEMA <esquema_origen>
    LIMIT TO (<tabla_origen>)
    FROM SERVER origen_srv INTO staging;

-- A partir de aca se usa como cualquier tabla local:
INSERT INTO <esquema_destino>.<tabla_destino> (col_a, col_b)
SELECT col_a, col_b FROM staging.<tabla_origen>
ON CONFLICT (<columna_pk>) DO NOTHING;


-- ============================================================================
-- 2) Columnas de una tabla + conteo total en una columna aparte
-- ============================================================================
-- Una fila por columna, y en cada fila el total de columnas de la tabla.
-- COUNT(*) OVER () cuenta sobre todas las filas del resultado sin agrupar,
-- que es justo lo que hace que el total quepa al lado del detalle.

SELECT
    c.ordinal_position                       AS posicion,
    c.column_name                            AS columna,
    c.data_type                              AS tipo,
    c.is_nullable                            AS acepta_null,
    c.column_default                         AS valor_defecto,
    COUNT(*) OVER ()                         AS total_columnas
FROM information_schema.columns c
WHERE c.table_schema = '<esquema>'
  AND c.table_name   = '<tabla>'
ORDER BY c.ordinal_position;

-- Variante: una sola fila, con las columnas concatenadas y el total.
SELECT
    string_agg(c.column_name, ', ' ORDER BY c.ordinal_position) AS columnas,
    COUNT(*)                                                    AS total_columnas
FROM information_schema.columns c
WHERE c.table_schema = '<esquema>'
  AND c.table_name   = '<tabla>';

-- Ojo: information_schema.columns solo muestra las tablas sobre las que el
-- usuario conectado tiene algun privilegio. Si una tabla existe pero no
-- aparece, no es que falte: es permisos. Para ver el catalogo sin ese filtro:
SELECT
    a.attnum                                 AS posicion,
    a.attname                                AS columna,
    format_type(a.atttypid, a.atttypmod)     AS tipo,
    NOT a.attnotnull                         AS acepta_null,
    COUNT(*) OVER ()                         AS total_columnas
FROM pg_attribute a
WHERE a.attrelid = '<esquema>.<tabla>'::regclass
  AND a.attnum > 0            -- descarta columnas de sistema (ctid, xmin, ...)
  AND NOT a.attisdropped      -- descarta columnas borradas con ALTER TABLE DROP
ORDER BY a.attnum;

-- Variante: total de columnas por cada tabla del esquema.
SELECT
    c.table_name                             AS tabla,
    COUNT(*)                                 AS total_columnas,
    string_agg(c.column_name, ', ' ORDER BY c.ordinal_position) AS columnas
FROM information_schema.columns c
WHERE c.table_schema = '<esquema>'
GROUP BY c.table_name
ORDER BY c.table_name;
