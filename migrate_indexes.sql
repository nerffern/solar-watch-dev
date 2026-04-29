-- SolarWatch — migrate_indexes.sql
--
-- Adds the unique index on solar_readings required for ON CONFLICT DO NOTHING
-- in collector.py, and checks the current index state of the live database.
--
-- Run ONCE against your live database:
--   psql -h postgres-ha.hfisystems.com -U postgres -d solarwatch -f migrate_indexes.sql
--
-- Safe to re-run — uses IF NOT EXISTS throughout.
-- For a large table this index build may take 1-5 minutes.
--
-- ⚠️  CONCURRENTLY cannot run inside a transaction block.
--    Run via psql command line (recommended):
--      psql -h postgres-ha.hfisystems.com -U postgres -d solarwatch -f migrate_indexes.sql
--
--    If running from a GUI tool (DBeaver, DataGrip etc), replace
--    CONCURRENTLY with nothing:
--      CREATE UNIQUE INDEX IF NOT EXISTS idx_sw_unique_reading ...

-- ── 1. Check existing indexes before doing anything ───────────────────────────
-- Run this block first to see what's already present on your live DB.

SELECT
    i.relname                          AS index_name,
    ix.indisunique                     AS is_unique,
    ix.indisprimary                    AS is_primary,
    array_to_string(
        array_agg(a.attname ORDER BY x.ordinality), ', '
    )                                  AS columns,
    pg_size_pretty(pg_relation_size(i.oid)) AS index_size
FROM
    pg_class t
    JOIN pg_index ix    ON t.oid = ix.indrelid
    JOIN pg_class i     ON i.oid = ix.indexrelid
    JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS x(attnum, ordinality)
        ON TRUE
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.attnum
WHERE
    t.relname IN ('solar_readings', 'weather_readings')
    AND t.relkind = 'r'
GROUP BY i.relname, ix.indisunique, ix.indisprimary, i.oid
ORDER BY t.relname, i.relname;


-- ── 2. Add unique index on solar_readings ─────────────────────────────────────
-- CONCURRENTLY means reads and writes are NOT blocked while the index builds.
-- Cannot be run inside a transaction block — psql runs it at session level which is fine.
--
-- This index enforces: one reading per (timestamp, site, inverter).
-- It also enables ON CONFLICT DO NOTHING in collector.py — safe restarts mid-cycle.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_sw_unique_reading
    ON solar_readings (time, site_name, inverter_name);


-- ── 3. Add unique index on weather_readings ───────────────────────────────────
-- One weather reading per (timestamp, site).

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_wx_unique_reading
    ON weather_readings (time, site_name);


-- ── 4. Verify — run after the index builds complete ───────────────────────────

SELECT
    i.relname                          AS index_name,
    ix.indisunique                     AS is_unique,
    array_to_string(
        array_agg(a.attname ORDER BY x.ordinality), ', '
    )                                  AS columns,
    pg_size_pretty(pg_relation_size(i.oid)) AS index_size
FROM
    pg_class t
    JOIN pg_index ix    ON t.oid = ix.indrelid
    JOIN pg_class i     ON i.oid = ix.indexrelid
    JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS x(attnum, ordinality)
        ON TRUE
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.attnum
WHERE
    t.relname IN ('solar_readings', 'weather_readings')
    AND t.relkind = 'r'
GROUP BY i.relname, ix.indisunique, i.oid
ORDER BY t.relname, i.relname;
