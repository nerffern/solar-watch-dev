-- SolarWatch — Weather Feature Migration
-- Run ONCE against your live database as postgres superuser:
--   psql -h postgres-ha.hfisystems.com -U postgres -d solarwatch -f migrate_weather.sql
--
-- Safe to re-run — all statements use IF NOT EXISTS / DO $$ guards.
-- Does NOT touch any existing tables or data.

\connect solarwatch

-- ── 1. Add coordinates to sites table ────────────────────────────────────────
ALTER TABLE sites
    ADD COLUMN IF NOT EXISTS latitude  NUMERIC(9,6),
    ADD COLUMN IF NOT EXISTS longitude NUMERIC(9,6);

-- ── 2. Set coordinates for existing sites ────────────────────────────────────
-- Penguin: 277 Penguin Crescent, Wierda Park, Centurion
UPDATE sites
SET latitude = -25.8823, longitude = 28.1541,
    location = '277 Penguin Crescent, Wierda Park, Centurion'
WHERE site_name = 'Penguin';

-- Selati: 60 Selati Street, Alphen Park, Pretoria
UPDATE sites
SET latitude = -25.8156, longitude = 28.2134,
    location = '60 Selati Street, Alphen Park, Pretoria'
WHERE site_name = 'Selati';

-- Lanner: 15A Lanner Street, Amberfield Glen Estates, Centurion
UPDATE sites
SET latitude = -25.8611, longitude = 28.1089,
    location = '15A Lanner Street, Amberfield Glen Estates, Centurion'
WHERE site_name = 'Lanner';

-- ── 3. Create weather_readings table ─────────────────────────────────────────
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

-- ── 4. Index ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_wx_site_time
    ON weather_readings (site_name, time DESC);

-- ── 5. Verify ─────────────────────────────────────────────────────────────────
SELECT site_name, latitude, longitude FROM sites ORDER BY site_name;
SELECT 'weather_readings table created' AS status
WHERE EXISTS (
    SELECT FROM information_schema.tables
    WHERE table_name = 'weather_readings'
);
