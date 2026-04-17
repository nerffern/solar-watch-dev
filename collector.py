#!/usr/bin/env python3
"""
SolarWatch — collector.py

Single process, runs on one central server.
  • Deye sites    → polled directly over WAN via Solarman V5 (pysolarmanv5)
  • Sunsynk sites → polled via api.sunsynk.net cloud API
  • Weather       → polled from Open-Meteo (free, no API key) every WEATHER_INTERVAL seconds

Site and inverter config is loaded from the `sites` table in the DB —
no hardcoded IPs or serials in this file.

Systemd: solarwatch.service
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool
from dotenv import load_dotenv

import deye_worker
import sunsynk_worker
import weather_worker

load_dotenv()

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "solarwatch.log")),
    ],
)
log = logging.getLogger("solarwatch")

# ── CONFIG ────────────────────────────────────────────────────────────────────

POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL",    "60"))
MAX_RETRIES      = int(os.getenv("MAX_RETRIES",      "3"))
RETRY_DELAY      = int(os.getenv("RETRY_DELAY",      "5"))
CONFIG_RELOAD    = int(os.getenv("CONFIG_RELOAD",    "300"))   # re-read sites table every N seconds
WEATHER_INTERVAL = int(os.getenv("WEATHER_INTERVAL", "900"))   # weather poll every 15 min

PG_DSN = (
    f"host={os.getenv('PG_HOST', 'postgres-ha.hfisystems.com')} "
    f"port={os.getenv('PG_PORT', '5432')} "
    f"dbname={os.getenv('PG_DB', 'solarwatch')} "
    f"user={os.getenv('PG_USER', 'solarwatch_user')} "
    f"password={os.getenv('PG_PASS', '')} "
    f"connect_timeout=10 "
    f"application_name=solarwatch_collector "
    f"sslmode={os.getenv('PG_SSLMODE', 'prefer')}"
)

# ── DATABASE ──────────────────────────────────────────────────────────────────

db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def init_db_pool():
    global db_pool
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, PG_DSN)
    log.info(
        f"DB pool ready → "
        f"{os.getenv('PG_HOST', 'postgres-ha.hfisystems.com')}:"
        f"{os.getenv('PG_PORT','5432')}/solarwatch"
    )


def load_sites() -> tuple[list[dict], list[dict]]:
    """Returns (deye_sites, sunsynk_sites) from the sites table."""
    conn = db_pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT site_name, display_name, source_type,
                       inverters, sunsynk_username, sunsynk_password, sunsynk_plant_id,
                       latitude, longitude
                FROM sites
                WHERE enabled = TRUE
                ORDER BY source_type, site_name
            """)
            rows          = [dict(r) for r in cur.fetchall()]
            deye_sites    = [r for r in rows if r["source_type"] == "deye"]
            sunsynk_sites = [r for r in rows if r["source_type"] == "sunsynk"]
            return deye_sites, sunsynk_sites
    finally:
        db_pool.putconn(conn)


INSERT_SQL = """
INSERT INTO solar_readings (
    time, site_name, source_type, inverter_name, inverter_sn,
    pv1_voltage, pv1_current, pv1_power,
    pv2_voltage, pv2_current, pv2_power,
    battery_voltage, battery_current, battery_power, battery_soc, battery_temp,
    grid_voltage, grid_frequency, grid_power, grid_current,
    load_power, load_voltage, inverter_temp, dc_temp,
    daily_pv_energy, total_pv_energy,
    daily_battery_charge, daily_battery_discharge,
    daily_grid_import, daily_grid_export, daily_load_energy,
    poll_duration_ms, poll_success, ct_power, ct_load_power
) VALUES (
    %(time)s, %(site_name)s, %(source_type)s, %(inverter_name)s, %(inverter_sn)s,
    %(pv1_voltage)s, %(pv1_current)s, %(pv1_power)s,
    %(pv2_voltage)s, %(pv2_current)s, %(pv2_power)s,
    %(battery_voltage)s, %(battery_current)s, %(battery_power)s,
    %(battery_soc)s, %(battery_temp)s,
    %(grid_voltage)s, %(grid_frequency)s, %(grid_power)s, %(grid_current)s,
    %(load_power)s, %(load_voltage)s, %(inverter_temp)s, %(dc_temp)s,
    %(daily_pv_energy)s, %(total_pv_energy)s,
    %(daily_battery_charge)s, %(daily_battery_discharge)s,
    %(daily_grid_import)s, %(daily_grid_export)s, %(daily_load_energy)s,
    %(poll_duration_ms)s, %(poll_success)s, %(ct_power)s, %(ct_load_power)s
)
"""

WEATHER_INSERT_SQL = """
INSERT INTO weather_readings (
    time, site_name,
    temp_c, feels_like_c, cloud_cover, precipitation,
    wind_speed, wind_direction, humidity,
    weather_code, uv_index, sunrise, sunset,
    solar_rad, is_day
) VALUES (
    %(time)s, %(site_name)s,
    %(temp_c)s, %(feels_like_c)s, %(cloud_cover)s, %(precipitation)s,
    %(wind_speed)s, %(wind_direction)s, %(humidity)s,
    %(weather_code)s, %(uv_index)s, %(sunrise)s, %(sunset)s,
    %(solar_rad)s, %(is_day)s
)
"""


def write_reading(site_name: str, inv_name: str, inv_sn: str, data: dict):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            row = {
                "time":          datetime.now(timezone.utc),
                "site_name":     site_name,
                "inverter_name": inv_name,
                "inverter_sn":   inv_sn,
                **data,
            }
            cur.execute(INSERT_SQL, row)
            conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"[{site_name}/{inv_name}] DB write failed: {e}")
        raise
    finally:
        db_pool.putconn(conn)


def write_weather(data: dict):
    """Insert one weather reading — strips internal _emoji/_description keys."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            row = {k: v for k, v in data.items() if not k.startswith("_")}
            cur.execute(WEATHER_INSERT_SQL, row)
            conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"[weather/{data.get('site_name')}] DB write failed: {e}")
    finally:
        db_pool.putconn(conn)


# ── DEYE POLLING ──────────────────────────────────────────────────────────────

def poll_deye_inverter_with_retry(inv: dict, site_name: str) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        result = deye_worker.poll(inv, site_name)
        if result is not None:
            return result
        if attempt < MAX_RETRIES:
            log.warning(
                f"[{site_name}/{inv['name']}] Retry {attempt}/{MAX_RETRIES} "
                f"in {RETRY_DELAY}s..."
            )
            time.sleep(RETRY_DELAY)
    log.error(f"[{site_name}/{inv['name']}] All {MAX_RETRIES} attempts failed")
    return None


def poll_deye_sites(sites: list[dict]):
    for site in sites:
        site_name = site["site_name"]
        inverters = site.get("inverters") or []
        for inv in inverters:
            if not running:
                return
            if inv.get("inverter_sn", "").startswith("CONFIRM"):
                log.warning(
                    f"[{site_name}/{inv['name']}] Skipping — inverter_sn not confirmed"
                )
                continue
            data = poll_deye_inverter_with_retry(inv, site_name)
            if data:
                try:
                    write_reading(site_name, inv["name"], inv["inverter_sn"], data)
                except Exception:
                    pass


# ── SUNSYNK POLLING ───────────────────────────────────────────────────────────

_sunsynk_clients: dict[str, sunsynk_worker.SunsynkClient] = {}


def get_sunsynk_client(site: dict) -> sunsynk_worker.SunsynkClient:
    key = site["sunsynk_username"]
    if key not in _sunsynk_clients:
        _sunsynk_clients[key] = sunsynk_worker.SunsynkClient(
            site["sunsynk_username"],
            site["sunsynk_password"],
        )
    return _sunsynk_clients[key]


def poll_sunsynk_sites(sites: list[dict]):
    for site in sites:
        site_name = site["site_name"]
        try:
            client   = get_sunsynk_client(site)
            readings = sunsynk_worker.poll(site, client)
            for reading in readings:
                if not running:
                    return
                try:
                    write_reading(
                        site_name,
                        reading.pop("inverter_name"),
                        reading.pop("inverter_sn"),
                        reading,
                    )
                except Exception:
                    pass
        except Exception as e:
            log.error(f"[{site_name}] Sunsynk poll error: {e}")


# ── WEATHER POLLING ───────────────────────────────────────────────────────────

# Tracks last weather poll time per site (monotonic seconds)
_last_weather: dict[str, float] = {}


def poll_weather(all_sites: list[dict]):
    """
    Poll Open-Meteo for every site that has lat/lon set and whose last
    weather fetch is older than WEATHER_INTERVAL.
    Silently skips sites with no coordinates configured.
    """
    now = time.monotonic()
    for site in all_sites:
        site_name = site["site_name"]
        lat = site.get("latitude")
        lon = site.get("longitude")

        if lat is None or lon is None:
            continue  # coordinates not set yet — skip silently

        last = _last_weather.get(site_name, 0)
        if now - last < WEATHER_INTERVAL:
            continue  # not due yet

        data = weather_worker.fetch(site_name, float(lat), float(lon))
        if data:
            write_weather(data)
            _last_weather[site_name] = now


# ── GRACEFUL SHUTDOWN ─────────────────────────────────────────────────────────

running = True


def handle_signal(sig, frame):
    global running
    log.info(f"Signal {sig} — shutting down gracefully...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("SolarWatch Collector starting")
    log.info(f"Poll interval    : {POLL_INTERVAL}s")
    log.info(f"Config reload    : every {CONFIG_RELOAD}s")
    log.info(f"Weather interval : every {WEATHER_INTERVAL}s")
    log.info("=" * 60)

    init_db_pool()

    deye_sites    = []
    sunsynk_sites = []
    last_cfg_load = 0

    while running:
        cycle_start = time.monotonic()

        # Reload site config from DB periodically
        if time.monotonic() - last_cfg_load > CONFIG_RELOAD:
            try:
                deye_sites, sunsynk_sites = load_sites()
                last_cfg_load = time.monotonic()
                log.info(
                    f"Sites loaded — "
                    f"Deye: {[s['site_name'] for s in deye_sites]} | "
                    f"Sunsynk: {[s['site_name'] for s in sunsynk_sites]}"
                )
            except Exception as e:
                log.error(f"Failed to load sites: {e}")

        # Poll Deye sites (WAN Modbus)
        if deye_sites:
            poll_deye_sites(deye_sites)

        # Poll Sunsynk sites (cloud API)
        if sunsynk_sites:
            poll_sunsynk_sites(sunsynk_sites)

        # Poll weather for all sites that have coordinates set
        all_sites = deye_sites + sunsynk_sites
        if all_sites:
            poll_weather(all_sites)

        elapsed    = time.monotonic() - cycle_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        log.debug(f"Cycle done in {elapsed:.1f}s — sleeping {sleep_time:.1f}s")

        deadline = time.monotonic() + sleep_time
        while running and time.monotonic() < deadline:
            time.sleep(1)

    if db_pool:
        db_pool.closeall()
    log.info("SolarWatch collector stopped cleanly")


if __name__ == "__main__":
    main()
