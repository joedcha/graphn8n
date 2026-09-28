-- ============================================================================
-- Carga UNICA de <bd_origen>.<esquema_origen>.<tabla_origen>
--                -> <esquema_destino>.<tabla_destino>   (bases distintas)
-- Estrategia ante duplicados: ON CONFLICT DO NOTHING (re-ejecutable).
--
-- Todo se ejecuta conectado a la base DESTINO, en UNA sola sesion de psql
-- (la conexion con nombre 'origen' vive mientras dure la sesion).
-- Reemplazar los <...>. Ejecutar paso por paso, NO todo de corrido.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS dblink;


-- ----------------------------------------------------------------------------
-- PASO 1. Abrir la conexion y probar que responde
-- ----------------------------------------------------------------------------
SELECT dblink_connect(
    'origen',
    'host=<host_origen> port=5432 dbname=<bd_origen> user=<usuario> password=<clave>'
);

SELECT * FROM dblink('origen', 'SELECT current_database(), current_user')
    AS t(bd text, usuario text);
-- Si esto no devuelve una fila, el resto no tiene sentido: revisar red,
-- pg_hba.conf del origen y credenciales antes de seguir.


-- ----------------------------------------------------------------------------
-- PASO 2. Generar el INSERT completo, con el AS t(...) ya resuelto
-- ----------------------------------------------------------------------------
-- El error mas comun de dblink es escribir a mano la lista de tipos del
-- AS t(...) y que no cuadre con el origen: falla en ejecucion, a mitad de
-- carga. Esto la lee del catalogo del origen y arma la sentencia lista para
-- copiar, pegar y ejecutar. Solo hay que cambiar <...> dentro de este SELECT.

SELECT format(
$sql$INSERT INTO %s (%s)
SELECT %s
FROM dblink('origen', 'SELECT %s FROM %s')
     AS t(%s)
ON CONFLICT (%s) DO NOTHING;$sql$,
    '<esquema_destino>.<tabla_destino>',   -- destino
    s.cols,                                -- columnas del INSERT
    s.cols,                                -- columnas del SELECT local
    s.cols,                                -- columnas de la consulta remota
    '<esquema_origen>.<tabla_origen>',     -- origen
    s.cols_tipadas,                        -- el AS t(...), generado
    '<columna_pk_destino>'                 -- columna con UNIQUE/PK en destino
) AS sentencia_lista
FROM (
    SELECT
        string_agg(col,  ', ' ORDER BY pos)              AS cols,
        string_agg(col || ' ' || tipo, ', ' ORDER BY pos) AS cols_tipadas
    FROM dblink('origen', $q$
            SELECT a.attnum,
                   quote_ident(a.attname),
                   format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = '<esquema_origen>.<tabla_origen>'::regclass
              AND a.attnum > 0
              AND NOT a.attisdropped
         $q$) AS t(pos int, col text, tipo text)
) s;


-- ----------------------------------------------------------------------------
-- PASO 3. Confirmar que destino TIENE un UNIQUE o PK sobre esa columna
-- ----------------------------------------------------------------------------
-- Sin restriccion unica, ON CONFLICT (col) da error de sintaxis; y un
-- ON CONFLICT DO NOTHING sin columna simplemente no detecta nada y termina
-- insertando duplicados en silencio. Este es el chequeo que lo evita.

SELECT c.conname                                  AS restriccion,
       CASE c.contype WHEN 'p' THEN 'PRIMARY KEY'
                      WHEN 'u' THEN 'UNIQUE' END  AS tipo,
       pg_get_constraintdef(c.oid)                AS definicion
FROM pg_constraint c
WHERE c.conrelid = '<esquema_destino>.<tabla_destino>'::regclass
  AND c.contype IN ('p', 'u');
-- Si no devuelve nada, crear la restriccion antes de cargar:
--   ALTER TABLE <esquema_destino>.<tabla_destino>
--       ADD CONSTRAINT <nombre> UNIQUE (<columna_pk_destino>);


-- ----------------------------------------------------------------------------
-- PASO 4. Comparar estructuras origen vs destino antes de mover nada
-- ----------------------------------------------------------------------------
-- Muestra columna por columna que hay en cada lado. Las filas con NULL de un
-- lado son las que no calzan: o sobran, o faltan, o cambian de tipo.

WITH ori AS (
    SELECT * FROM dblink('origen', $q$
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        WHERE a.attrelid = '<esquema_origen>.<tabla_origen>'::regclass
          AND a.attnum > 0 AND NOT a.attisdropped
    $q$) AS t(columna text, tipo text)
),
des AS (
    SELECT a.attname::text                          AS columna,
           format_type(a.atttypid, a.atttypmod)     AS tipo
    FROM pg_attribute a
    WHERE a.attrelid = '<esquema_destino>.<tabla_destino>'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
)
SELECT COALESCE(o.columna, d.columna)   AS columna,
       o.tipo                           AS tipo_origen,
       d.tipo                           AS tipo_destino,
       CASE
           WHEN o.columna IS NULL          THEN 'solo en destino'
           WHEN d.columna IS NULL          THEN 'solo en origen (no se cargara)'
           WHEN o.tipo IS DISTINCT FROM d.tipo THEN 'TIPO DISTINTO'
           ELSE 'ok'
       END                              AS estado
FROM ori o
FULL OUTER JOIN des d ON d.columna = o.columna
ORDER BY 1;


-- ----------------------------------------------------------------------------
-- PASO 5. Conteo previo (para poder comprobar despues)
-- ----------------------------------------------------------------------------
SELECT
    (SELECT n FROM dblink('origen',
        'SELECT count(*) FROM <esquema_origen>.<tabla_origen>') AS t(n bigint))
                                                        AS filas_origen,
    (SELECT count(*) FROM <esquema_destino>.<tabla_destino>)
                                                        AS filas_destino_antes;


-- ----------------------------------------------------------------------------
-- PASO 6. La carga
-- ----------------------------------------------------------------------------
-- Pegar aqui la sentencia que genero el PASO 2, dentro de la transaccion.
-- Con BEGIN explicito: si algo sale mal a mitad, no queda una carga parcial.

BEGIN;

-- <<< sentencia generada en el PASO 2 >>>

-- Revisar el conteo que reporta el INSERT antes de confirmar.
COMMIT;
-- ROLLBACK;   -- si el numero no cuadra


-- ----------------------------------------------------------------------------
-- PASO 7. Verificacion
-- ----------------------------------------------------------------------------
SELECT
    (SELECT n FROM dblink('origen',
        'SELECT count(*) FROM <esquema_origen>.<tabla_origen>') AS t(n bigint))
                                                        AS filas_origen,
    (SELECT count(*) FROM <esquema_destino>.<tabla_destino>)
                                                        AS filas_destino_despues;

-- Que quedo en origen sin llegar a destino (deberia dar 0 filas):
SELECT o.<columna_pk_destino>
FROM dblink('origen',
     'SELECT <columna_pk_destino> FROM <esquema_origen>.<tabla_origen>')
     AS o(<columna_pk_destino> <tipo_pk>)
WHERE NOT EXISTS (
    SELECT 1 FROM <esquema_destino>.<tabla_destino> d
    WHERE d.<columna_pk_destino> = o.<columna_pk_destino>
);

SELECT dblink_disconnect('origen');
