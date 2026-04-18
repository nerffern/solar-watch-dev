-- SolarWatch — Full DB Setup
-- Run as postgres superuser:
--   psql -h postgres-ha.hfisystems.com -U postgres -f setup.sql
--
-- Creates the solarwatch database, user, schema, and seeds both Deye sites.
-- Does NOT touch any existing databases.

-- ── 1. Create user ────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'solarwatch_user') THEN
        CREATE USER solarwatch_user WITH PASSWORD 'CHANGEME';
        RAISE NOTICE 'Created user solarwatch_user';
    ELSE
        RAISE NOTICE 'User solarwatch_user already exists — skipping';
    END IF;
END
$$;

-- ── 2. Create database ────────────────────────────────────────────────────────
-- NOTE: If you get "database already exists" just skip this line.
CREATE DATABASE solarwatch
    OWNER    = solarwatch_user
    ENCODING = 'UTF8'
    TEMPLATE = template0;

-- ── 3. Connect and configure schema ──────────────────────────────────────────
\connect solarwatch

GRANT ALL ON SCHEMA public TO solarwatch_user;
ALTER SCHEMA public OWNER TO solarwatch_user;

-- ── 4. Sites table ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sites (
    id                  SERIAL      PRIMARY KEY,
    site_name           TEXT        NOT NULL UNIQUE,
    display_name        TEXT        NOT NULL,
    source_type         TEXT        NOT NULL CHECK (source_type IN ('deye', 'sunsynk')),
    enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    location            TEXT,
    latitude            NUMERIC(9,6),          -- decimal degrees, e.g. -24.052200
    longitude           NUMERIC(9,6),          -- decimal degrees, e.g.  31.263700
    -- Deye: array of inverter connection details
    inverters           JSONB,
    -- Sunsynk: cloud credentials
    sunsynk_username    TEXT,
    sunsynk_password    TEXT,
    sunsynk_plant_id    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

GRANT ALL ON TABLE sites TO solarwatch_user;
GRANT USAGE, SELECT ON SEQUENCE sites_id_seq TO solarwatch_user;

-- ── 5. Solar readings table ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS solar_readings (
    time                    TIMESTAMPTZ NOT NULL,
    site_name               TEXT        NOT NULL,
    source_type             TEXT        NOT NULL DEFAULT 'deye',
    inverter_name           TEXT        NOT NULL,
    inverter_sn             TEXT        NOT NULL,
    pv1_voltage             NUMERIC(7,2),
    pv1_current             NUMERIC(7,2),
    pv1_power               NUMERIC(9,2),
    pv2_voltage             NUMERIC(7,2),
    pv2_current             NUMERIC(7,2),
    pv2_power               NUMERIC(9,2),
    battery_voltage         NUMERIC(7,2),
    battery_current         NUMERIC(7,2),
    battery_power           NUMERIC(9,2),
    battery_soc             NUMERIC(5,1),
    battery_temp            NUMERIC(5,1),
    grid_voltage            NUMERIC(7,2),
    grid_frequency          NUMERIC(6,3),
    grid_power              NUMERIC(9,2),
    grid_current            NUMERIC(7,2),
    load_power              NUMERIC(9,2),
    load_voltage            NUMERIC(7,2),
    inverter_temp           NUMERIC(5,1),
    dc_temp                 NUMERIC(5,1),
    daily_pv_energy         NUMERIC(9,2),
    total_pv_energy         NUMERIC(12,2),
    daily_battery_charge    NUMERIC(9,2),
    daily_battery_discharge NUMERIC(9,2),
    daily_grid_import       NUMERIC(9,2),
    daily_grid_export       NUMERIC(9,2),
    daily_load_energy       NUMERIC(9,2),
    poll_duration_ms        INTEGER,
    poll_success            BOOLEAN     NOT NULL DEFAULT TRUE,
    ct_power                NUMERIC(9,2),
    ct_load_power           NUMERIC(9,2)
);

GRANT ALL ON TABLE solar_readings TO solarwatch_user;

-- ── 6. Weather readings table ─────────────────────────────────────────────────
-- Populated by weather_worker.py via Open-Meteo (free, no API key).
-- Polled every WEATHER_INTERVAL seconds (default 900 = 15 min).
CREATE TABLE IF NOT EXISTS weather_readings (
    time             TIMESTAMPTZ  NOT NULL,
    site_name        TEXT         NOT NULL,
    temp_c           NUMERIC(5,1),          -- air temperature °C
    feels_like_c     NUMERIC(5,1),          -- apparent temperature °C
    cloud_cover      INTEGER,               -- 0–100 %
    precipitation    NUMERIC(6,2),          -- mm in last hour
    wind_speed       NUMERIC(6,1),          -- km/h at 10m
    wind_direction   INTEGER,               -- degrees 0–360
    humidity         INTEGER,               -- relative humidity %
    weather_code     INTEGER,               -- WMO weather interpretation code
    uv_index         NUMERIC(4,1),          -- UV index (0–11+)
    sunrise          TIMESTAMPTZ,           -- local sunrise time (daily)
    sunset           TIMESTAMPTZ,           -- local sunset time (daily)
    solar_rad        NUMERIC(8,2),          -- shortwave radiation W/m² (instant)
    is_day           BOOLEAN                -- true during daylight hours
);

GRANT ALL ON TABLE weather_readings TO solarwatch_user;

-- ── 7. Indexes ────────────────────────────────────────────────────────────────
CREATE INDEX idx_sw_site_inv_time  ON solar_readings (site_name, inverter_name, time DESC);
CREATE INDEX idx_sw_time           ON solar_readings (time DESC);
CREATE INDEX idx_sw_source_type    ON solar_readings (source_type, time DESC);
CREATE INDEX idx_wx_site_time      ON weather_readings (site_name, time DESC);

-- ── 8. Seed sites ─────────────────────────────────────────────────────────────

INSERT INTO sites (site_name, display_name, source_type, location, latitude, longitude, inverters)
VALUES (
    'Selati', 'HFI Selati', 'deye', 'Selati, Limpopo, ZA',
    -24.052200, 31.263700,
    '[
        {"name":"Inverter_1","ip":"192.168.10.3","dongle_serial":2705000422,"inverter_sn":"2208262350"},
        {"name":"Inverter_2","ip":"192.168.10.2","dongle_serial":2776470283,"inverter_sn":"2303020166"}
    ]'::jsonb
) ON CONFLICT (site_name) DO NOTHING;

-- Lanner — update coordinates once confirmed:
--   UPDATE sites SET latitude = -XX.XXXX, longitude = XX.XXXX WHERE site_name = 'Lanner';
-- Inverter_1 SN also pending confirmation:
--   UPDATE sites SET inverters = jsonb_set(inverters, '{0,inverter_sn}', '"ACTUAL_SN"')
--   WHERE site_name = 'Lanner';
INSERT INTO sites (site_name, display_name, source_type, location, inverters)
VALUES (
    'Lanner', 'HFI Lanner', 'deye', 'Lanner, ZA',
    '[
        {"name":"Inverter_1","ip":"100.100.6.105","dongle_serial":2771845636,"inverter_sn":"2305138031"},
        {"name":"Inverter_2","ip":"100.100.6.204","dongle_serial":2774034843,"inverter_sn":"2304096005"}
    ]'::jsonb
) ON CONFLICT (site_name) DO NOTHING;

-- ── 9. Verify ─────────────────────────────────────────────────────────────────
SELECT site_name, display_name, source_type, enabled, location, latitude, longitude
FROM sites ORDER BY site_name;
