-- SolarWatch — Import Selati historical data from old 'solar' DB
-- Run as postgres superuser AFTER setup.sql has been run:
--   psql -h postgres-ha.hfisystems.com -U postgres -f import_selati.sql
--
-- Copies all rows from solar.solar_readings into solarwatch.solar_readings,
-- tagging them as site_name='Selati' and source_type='deye'.
-- The old DB is never modified.

\connect solarwatch

INSERT INTO solar_readings (
    time, site_name, source_type, inverter_name, inverter_sn,
    pv1_voltage, pv1_current, pv1_power,
    pv2_voltage, pv2_current, pv2_power,
    battery_voltage, battery_current, battery_power, battery_soc, battery_temp,
    grid_voltage, grid_frequency, grid_power, grid_current,
    load_power, load_voltage,
    inverter_temp, dc_temp,
    daily_pv_energy, total_pv_energy,
    daily_battery_charge, daily_battery_discharge,
    daily_grid_import, daily_grid_export, daily_load_energy,
    poll_duration_ms, poll_success,
    ct_power, ct_load_power
)
SELECT
    time,
    'Selati'    AS site_name,
    'deye'      AS source_type,
    inverter_name,
    inverter_sn,
    pv1_voltage, pv1_current, pv1_power,
    pv2_voltage, pv2_current, pv2_power,
    battery_voltage, battery_current, battery_power, battery_soc, battery_temp,
    grid_voltage, grid_frequency, grid_power, grid_current,
    load_power, load_voltage,
    inverter_temp, dc_temp,
    daily_pv_energy, total_pv_energy,
    daily_battery_charge, daily_battery_discharge,
    daily_grid_import, daily_grid_export, daily_load_energy,
    poll_duration_ms, poll_success,
    ct_power, ct_load_power
FROM solar.public.solar_readings;   -- cross-DB reference via dblink is needed — see note below

-- NOTE: PostgreSQL cannot query across databases natively.
-- Use one of these approaches instead:
--
-- OPTION A — pg_dump/restore (recommended, simplest):
--   pg_dump -h postgres-ha.hfisystems.com -U postgres -t solar_readings solar \
--     | sed 's/solar_readings/solar_readings_import/g' > selati_data.sql
--   psql -h postgres-ha.hfisystems.com -U postgres -d solarwatch -f selati_data.sql
--   -- then INSERT INTO solar_readings SELECT 'Selati','deye',* FROM solar_readings_import;
--   -- DROP TABLE solar_readings_import;
--
-- OPTION B — dblink extension:
--   CREATE EXTENSION IF NOT EXISTS dblink;
--   INSERT INTO solar_readings (time, site_name, source_type, inverter_name, ...)
--   SELECT time, 'Selati', 'deye', inverter_name, ...
--   FROM dblink(
--     'host=localhost dbname=solar user=solar_user password=YOURPASS',
--     'SELECT time, inverter_name, inverter_sn, pv1_voltage, ... FROM solar_readings'
--   ) AS t(time timestamptz, inverter_name text, inverter_sn text, pv1_voltage numeric, ...);
--
-- OPTION C — Python one-liner (easiest if you have both DSNs handy):
--   python3 import_selati.py   (see import_selati.py)

-- Verify row count after import
SELECT COUNT(*) AS imported_rows, MIN(time) AS earliest, MAX(time) AS latest
FROM solar_readings
WHERE site_name = 'Selati';
