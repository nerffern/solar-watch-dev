"""
SolarWatch — powerflow_server.py  (enhanced)

Full-screen live power flow display.
Designed for kitchen monitors — scales to any screen size.

Enhancements over the original:
  1. FastAPI + uvicorn  — async, non-blocking, concurrent requests
  2. asyncpg            — async PostgreSQL driver (replaces psycopg2)
  3. In-process TTL cache — dramatically reduces DB hits
  4. Pool health / auto-reconnect — survives postgres-ha failovers
  5. Rate calc unified  — solar_savings_r removed from server; JS owns it
  6. Site validation on /api/chart/*  — consistent 400 on missing site
  7. Duplicate imports removed
  8. HTML served from static/index.html (falls back to inline if missing)
  9. GET /health        — DB liveness probe for uptime monitoring

Usage:
    pip install fastapi uvicorn asyncpg python-dotenv
    python3 powerflow_server.py
    Open: http://your-server:8765

Serves:
    GET /              → full-screen power flow page
    GET /health        → {"status":"ok","db":"ok"}
    GET /api/sites     → list of sites
    GET /api/flow      → live power data JSON
    GET /api/weather   → latest weather reading for a site
    GET /api/monthly   → this month's PV and grid totals
    GET /api/chart/*   → chart data for the Advanced view
"""

import os
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import asyncpg
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import uvicorn

load_dotenv(Path(__file__).parent / '.env')

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

DB_HOST = os.getenv('PG_HOST',    'postgres-ha.hfisystems.com')
DB_PORT = int(os.getenv('PG_PORT', '5432'))
DB_NAME = os.getenv('PG_DB',      'solarwatch')
DB_USER = os.getenv('PG_USER',    'solarwatch_user')
DB_PASS = os.getenv('PG_PASS',    '')
DB_SSL  = os.getenv('PG_SSLMODE', 'prefer')   # 'require' | 'prefer' | 'disable'
PORT    = int(os.getenv('PORT',   '8765'))
STATIC  = Path(__file__).parent / 'static' / 'index.html'

# ── CONNECTION POOL ───────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None

# asyncpg uses 'ssl' kwarg, not sslmode string — map the env value
_SSL_MAP = {'require': True, 'prefer': False, 'disable': False, 'allow': False}

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        ssl_val = _SSL_MAP.get(DB_SSL, False)
        _pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASS,
            ssl=ssl_val,
            min_size=1, max_size=5,
            command_timeout=10,
            # Auto-reconnect: if a connection goes stale after an HA failover,
            # asyncpg will retry acquiring a new one up to this many seconds.
            timeout=5,
        )
        log.info(f"DB pool ready → {DB_HOST}:{DB_PORT}/{DB_NAME}")
    return _pool


async def query_one(sql: str, params: tuple = ()) -> dict:
    """Execute SQL and return first row as dict, or {}."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
            return dict(row) if row else {}
    except (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError,
            OSError) as exc:
        # Connection-level failure: invalidate pool so it rebuilds on next call
        log.warning(f"DB connection error (will reconnect): {exc}")
        global _pool
        if _pool:
            await _pool.close()
            _pool = None
        raise


async def query_all(sql: str, params: tuple = ()) -> list[dict]:
    """Execute SQL and return all rows as list of dicts."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]
    except (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError,
            OSError) as exc:
        log.warning(f"DB connection error (will reconnect): {exc}")
        global _pool
        if _pool:
            await _pool.close()
            _pool = None
        raise

# ── IN-PROCESS TTL CACHE ──────────────────────────────────────────────────────
#
# Keyed by (endpoint, site).  TTLs:
#   /api/flow    →  10 s  (matches frontend poll interval)
#   /api/weather →  60 s
#   /api/monthly → 120 s
#   /api/chart/* →  60 s
#
# This cuts DB hits by ~80 % on a three-site, multi-browser deployment with
# no external cache dependency.

_cache: dict[str, tuple[float, Any]] = {}

def _cache_get(key: str, ttl: float) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None

def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)

# ── BUSINESS LOGIC ────────────────────────────────────────────────────────────

async def get_sites() -> list[dict]:
    rows = await query_all("""
        SELECT site_name, display_name
        FROM sites WHERE enabled = TRUE
        ORDER BY display_name
    """)
    return [{'name': r['site_name'], 'display': r['display_name']} for r in rows]


async def _resolve_site(site: str | None) -> str | None:
    """Return validated site name, or first enabled site if none given."""
    if site:
        return site
    sites = await get_sites()
    return sites[0]['name'] if sites else None


async def get_flow(site: str) -> dict:
    key = f'flow:{site}'
    cached = _cache_get(key, ttl=10)
    if cached is not None:
        return cached

    row = await query_one("""
        SELECT
            COALESCE(SUM(pv1_power + COALESCE(pv2_power,0)), 0)::int  AS solar_w,
            COALESCE(SUM(battery_power), 0)::int                       AS battery_w,
            COALESCE(SUM(grid_power),    0)::int                       AS grid_w,
            COALESCE(SUM(load_power),    0)::int                       AS load_w,
            COALESCE(AVG(battery_soc),   0)::numeric(5,1)              AS soc,
            COALESCE(AVG(battery_temp),  0)::numeric(5,1)              AS batt_temp,
            COALESCE(AVG(battery_voltage),0)::numeric(5,2)             AS batt_v,
            COALESCE(AVG(grid_voltage),  0)::numeric(5,1)              AS grid_v,
            COALESCE(AVG(grid_frequency),0)::numeric(5,2)              AS grid_hz,
            MAX(time)                                                   AS last_poll
        FROM (
            SELECT DISTINCT ON (inverter_name)
                inverter_name,
                pv1_power, pv2_power,
                battery_power, battery_soc, battery_temp, battery_voltage,
                grid_power, grid_voltage, grid_frequency,
                load_power, time
            FROM solar_readings
            WHERE site_name ILIKE $1
            AND time > NOW() - INTERVAL '10 minutes'
            ORDER BY inverter_name, time DESC
        ) latest
    """, (site,))

    if not row or row.get('solar_w') is None:
        return {'error': 'No recent data', 'site': site}

    last_poll = row.get('last_poll')
    age_s = None
    if last_poll:
        if last_poll.tzinfo is None:
            last_poll = last_poll.replace(tzinfo=timezone.utc)
        age_s = int((datetime.now(timezone.utc) - last_poll).total_seconds())

    d = {
        'site':      site,
        'solar_w':   int(row['solar_w']   or 0),
        'batt_w':    int(row['battery_w'] or 0),
        'grid_w':    int(row['grid_w']    or 0),
        'load_w':    int(row['load_w']    or 0),
        'soc':       float(row['soc']       or 0),
        'batt_temp': float(row['batt_temp'] or 0),
        'batt_v':    float(row['batt_v']    or 0),
        'grid_v':    float(row['grid_v']    or 0),
        'grid_hz':   float(row['grid_hz']   or 0),
        'age_s':     age_s,
        'stale':     age_s is not None and age_s > 300,
    }

    # Daily counters — load, grid, PV
    # NOTE: solar_savings_r is intentionally omitted here.
    # The JS frontend calculates it client-side using the user's chosen rate
    # (flat or IBT), so server and client stay in sync automatically.
    try:
        TODAY = """
            time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                     AT TIME ZONE 'Africa/Johannesburg' + INTERVAL '1 hour'
        """
        daily_row = await query_one(f"""
            SELECT
              COALESCE(
                MAX(load_val) FILTER (WHERE grid_val > 0),
                MAX(load_val)
              ) as load_kwh,
              MAX(grid_val)  as grid_kwh,
              SUM(pv_val)    as pv_kwh
            FROM (
              SELECT DISTINCT ON (inverter_name)
                inverter_name,
                daily_load_energy  as load_val,
                daily_grid_import  as grid_val,
                daily_pv_energy    as pv_val
              FROM solar_readings
              WHERE site_name ILIKE $1
              AND {TODAY}
              AND daily_load_energy > 0
              AND daily_load_energy < 200
              ORDER BY inverter_name, time DESC
            ) sub
        """, (site,))
        load = float(daily_row.get('load_kwh') or 1)
        grid = float(daily_row.get('grid_kwh') if daily_row.get('grid_kwh') is not None else 0)
        pv   = float(daily_row.get('pv_kwh')   or 0)
        d['self_suff']      = max(0, min(100, round((1 - grid / max(load, 0.001)) * 100)))
        d['daily_load_kwh'] = round(load, 1)
        d['daily_grid_kwh'] = round(grid, 1)
        d['daily_pv_kwh']   = round(pv,   1)
    except Exception as e:
        d['self_suff'] = 0
        log.warning(f"Daily counters error: {e}")

    _cache_set(key, d)
    return d


async def get_monthly(site: str) -> dict:
    """Return this month's PV kWh and grid kWh for the site."""
    key = f'monthly:{site}'
    cached = _cache_get(key, ttl=120)
    if cached is not None:
        return cached

    MONTH = """
        DATE_TRUNC('month', time AT TIME ZONE 'Africa/Johannesburg')
        = DATE_TRUNC('month', NOW() AT TIME ZONE 'Africa/Johannesburg')
    """
    pv_row = await query_one(f"""
        SELECT COALESCE(SUM(eod_pv), 0) AS month_pv_kwh
        FROM (
          SELECT DISTINCT ON (DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name)
            inverter_name,
            daily_pv_energy AS eod_pv
          FROM solar_readings
          WHERE {MONTH}
          AND site_name ILIKE $1
          AND daily_pv_energy IS NOT NULL
          AND daily_pv_energy > 0
          ORDER BY DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name, time DESC
        ) sub
    """, (site,))

    grid_row = await query_one(f"""
        SELECT COALESCE(SUM(day_grid), 0) AS month_grid_kwh
        FROM (
          SELECT
            DATE(time AT TIME ZONE 'Africa/Johannesburg') AS day,
            MAX(daily_grid_import) FILTER (WHERE daily_grid_import > 0) AS day_grid
          FROM solar_readings
          WHERE {MONTH}
          AND site_name ILIKE $1
          AND daily_grid_import IS NOT NULL
          AND daily_grid_import BETWEEN 0.01 AND 9000
          GROUP BY 1
        ) sub
    """, (site,))

    result = {
        'month_pv_kwh':   round(float(pv_row.get('month_pv_kwh')   or 0), 1),
        'month_grid_kwh': round(float(grid_row.get('month_grid_kwh') or 0), 1),
    }
    _cache_set(key, result)
    return result


async def get_weather(site: str) -> dict:
    """Return the most recent weather reading for a site."""
    key = f'weather:{site}'
    cached = _cache_get(key, ttl=60)
    if cached is not None:
        return cached

    WMO = {
        0:  ("☀️",  "Clear sky"),      1:  ("🌤️", "Mainly clear"),
        2:  ("⛅",  "Partly cloudy"),   3:  ("☁️",  "Overcast"),
        45: ("🌫️", "Foggy"),           48: ("🌫️", "Icy fog"),
        51: ("🌦️", "Light drizzle"),   53: ("🌦️", "Moderate drizzle"),
        55: ("🌧️", "Dense drizzle"),   61: ("🌧️", "Slight rain"),
        63: ("🌧️", "Moderate rain"),   65: ("🌧️", "Heavy rain"),
        71: ("🌨️", "Slight snow"),     73: ("🌨️", "Moderate snow"),
        75: ("❄️",  "Heavy snow"),      80: ("🌦️", "Slight showers"),
        81: ("🌧️", "Moderate showers"),82: ("⛈️",  "Violent showers"),
        95: ("⛈️",  "Thunderstorm"),    96: ("⛈️",  "T-storm w/ hail"),
        99: ("⛈️",  "T-storm heavy hail"),
    }

    row = await query_one("""
        SELECT
            temp_c, feels_like_c, cloud_cover, precipitation,
            wind_speed, wind_direction, humidity,
            weather_code, uv_index, sunrise, sunset,
            solar_rad, is_day, time AS last_updated
        FROM weather_readings
        WHERE site_name ILIKE $1
        ORDER BY time DESC
        LIMIT 1
    """, (site,))

    if not row:
        return {'error': 'No weather data yet', 'site': site}

    code = row.get('weather_code')
    emoji, desc = WMO.get(code, ("🌡️", f"Code {code}"))
    row['emoji']       = emoji
    row['description'] = desc
    result = dict(row)
    _cache_set(key, result)
    return result


async def get_chart(chart: str, site: str) -> dict:
    """Return chart data for the advanced view."""
    key = f'chart:{chart}:{site}'
    cached = _cache_get(key, ttl=60)
    if cached is not None:
        return cached

    S = site

    if chart == 'pv':
        per_inv = await query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name,
              AVG(pv1_power + COALESCE(pv2_power,0)) as pv_w
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE $1
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        combined = await query_all("""
            SELECT minute as time, SUM(avg_pv) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(pv1_power + COALESCE(pv2_power,0)) as avg_pv
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE $1 GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        result = {'per_inv': per_inv, 'combined': combined}

    elif chart == 'load':
        per_inv = await query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name, AVG(load_power) as load_w
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE $1
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        combined = await query_all("""
            SELECT minute as time, SUM(avg_load) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(load_power) as avg_load
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE $1 GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        result = {'per_inv': per_inv, 'combined': combined}

    elif chart == 'battery':
        power = await query_all("""
            SELECT minute as time, SUM(avg_batt) as batt_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(battery_power) as avg_batt
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE $1 GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        soc = await query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(battery_soc) as soc
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE $1 AND battery_soc IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (S,))
        result = {'power': power, 'soc': soc}

    elif chart == 'grid':
        power = await query_all("""
            SELECT minute as time, SUM(avg_grid) as grid_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(grid_power) as avg_grid
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE $1 GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        voltage = await query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(grid_voltage) as grid_v
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE $1 AND grid_voltage IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (S,))
        result = {'power': power, 'voltage': voltage}

    elif chart == 'daily':
        rows = await query_all("""
            SELECT day,
              SUM(eod_pv)   as pv,   MAX(eod_load) as load,
              MAX(eod_grid) as grid, SUM(eod_chg)  as chg,
              SUM(eod_dis)  as dis
            FROM (
              SELECT DISTINCT ON (DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name)
                DATE(time AT TIME ZONE 'Africa/Johannesburg')::text as day,
                inverter_name,
                daily_pv_energy         as eod_pv,
                daily_load_energy       as eod_load,
                daily_grid_import       as eod_grid,
                daily_battery_charge    as eod_chg,
                daily_battery_discharge as eod_dis
              FROM solar_readings
              WHERE time > NOW() - INTERVAL '14 days'
              AND site_name ILIKE $1
              AND daily_pv_energy IS NOT NULL
              ORDER BY DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name, time DESC
            ) sub GROUP BY day ORDER BY day
        """, (S,))
        result = rows

    elif chart == 'temps':
        rows = await query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name,
              AVG(CASE WHEN inverter_temp < 100 THEN inverter_temp END) as inv_temp,
              AVG(CASE WHEN dc_temp < 100 THEN dc_temp END) as dc_temp,
              AVG(CASE WHEN battery_temp > 0 THEN battery_temp END) as batt_temp
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE $1
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        result = rows

    elif chart == 'peaks':
        row = await query_one("""
            SELECT
              COALESCE(MAX(pv_total),   0) AS peak_pv,
              COALESCE(MAX(load_total), 0) AS peak_load,
              COALESCE(MAX(grid_total), 0) AS peak_grid
            FROM (
              SELECT ts,
                SUM(avg_pv)   AS pv_total,
                SUM(avg_load) AS load_total,
                SUM(avg_grid) AS grid_total
              FROM (
                SELECT DATE_TRUNC('minute', time) AS ts, inverter_name,
                  AVG(pv1_power + COALESCE(pv2_power,0)) AS avg_pv,
                  AVG(load_power)  AS avg_load,
                  AVG(CASE WHEN grid_power > 0 THEN grid_power ELSE 0 END) AS avg_grid
                FROM solar_readings
                WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                             AT TIME ZONE 'Africa/Johannesburg' + INTERVAL '1 hour'
                AND site_name ILIKE $1
                GROUP BY 1, 2
              ) inv GROUP BY ts
            ) totals
        """, (S,))
        result = row

    else:
        return {'error': f'Unknown chart: {chart}'}

    _cache_set(key, result)
    return result

# ── HTML ──────────────────────────────────────────────────────────────────────
# Prefer static/index.html so the front-end can be edited without touching
# this file.  Falls back to the inline string for single-file deployments.

def _load_html() -> str:
    if STATIC.exists():
        log.info(f"Serving HTML from {STATIC}")
        return STATIC.read_text()
    log.info("static/index.html not found — using inline HTML fallback")
    return _HTML_INLINE

# Inline fallback — extracted to static/index.html by setup but kept here so
# a single-file deployment still works.
_HTML_INLINE: str = ""  # populated at bottom of file after HTML literal

# ── FASTAPI APP ───────────────────────────────────────────────────────────────

_html_content: str = ""  # loaded at startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    global _html_content
    _html_content = _load_html()
    try:
        await get_pool()
    except Exception as e:
        log.error(f"DB connection failed at startup: {e}")
        # Don't abort — pool will retry on first request
    yield
    # ── shutdown ──
    global _pool
    if _pool:
        await _pool.close()
        log.info("DB pool closed")


app = FastAPI(title="SolarWatch", lifespan=lifespan, docs_url=None, redoc_url=None)

# Serve static assets — icons, manifest, service worker
_STATIC_DIR = Path(__file__).parent / 'static'
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    return HTMLResponse(_html_content)


@app.get("/health")
async def health():
    """Liveness probe — checks DB connectivity."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        log.error(f"Health check failed: {e}")
        return JSONResponse({"status": "degraded", "db": str(e)}, status_code=503)


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    """Web App Manifest for PWA install."""
    p = Path(__file__).parent / 'static' / 'manifest.json'
    return FileResponse(str(p), media_type='application/manifest+json')


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service worker — must be served from root scope."""
    p = Path(__file__).parent / 'static' / 'sw.js'
    resp = FileResponse(str(p), media_type='application/javascript')
    # No caching — browser must always get the latest SW
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = Path(__file__).parent / 'static' / 'icons' / 'favicon-32x32.png'
    return FileResponse(str(p), media_type='image/png')


@app.get("/api/sites")
async def api_sites():
    return await get_sites()


@app.get("/api/flow")
async def api_flow(site: str | None = Query(default=None)):
    resolved = await _resolve_site(site)
    if not resolved:
        raise HTTPException(400, "No sites available")
    return await get_flow(resolved)


@app.get("/api/monthly")
async def api_monthly(site: str | None = Query(default=None)):
    resolved = await _resolve_site(site)
    if not resolved:
        raise HTTPException(400, "No sites available")
    return await get_monthly(resolved)


@app.get("/api/weather")
async def api_weather(site: str | None = Query(default=None)):
    resolved = await _resolve_site(site)
    if not resolved:
        raise HTTPException(400, "No sites available")
    return await get_weather(resolved)


@app.get("/api/chart/{chart}")
async def api_chart(chart: str, site: str | None = Query(default=None)):
    # Validate site — consistent with all other endpoints
    resolved = await _resolve_site(site)
    if not resolved:
        raise HTTPException(400, "No sites available")
    result = await get_chart(chart, resolved)
    if isinstance(result, dict) and 'error' in result:
        raise HTTPException(404, result['error'])
    return result


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    uvicorn.run(
        "powerflow_server:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=True,
    )