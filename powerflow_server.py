"""
SolarWatch — powerflow_server.py

Full-screen live power flow display.
Designed for kitchen monitors — scales to any screen size.

Usage:
    python3 powerflow_server.py
    Open: http://your-server:8765

Serves:
    GET /              → full-screen power flow page
    GET /api/sites     → list of sites
    GET /api/flow      → live power data JSON
    GET /api/weather   → latest weather reading for a site
    GET /api/monthly   → this month's PV and grid totals
    GET /api/chart/*   → chart data for the Advanced view
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

load_dotenv(Path(__file__).parent / '.env')

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

DB_HOST = os.getenv('PG_HOST',    'postgres-ha.hfisystems.com')
DB_PORT = int(os.getenv('PG_PORT', '5432'))
DB_NAME = os.getenv('PG_DB',      'solarwatch')
DB_USER = os.getenv('PG_USER',    'solarwatch_user')
DB_PASS = os.getenv('PG_PASS',    '')
DB_SSL  = os.getenv('PG_SSLMODE', 'prefer')
PORT    = int(os.getenv('PORT',   '8765'))

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5,
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASS, sslmode=DB_SSL,
            connect_timeout=5,
        )
        log.info(f"DB pool ready → {DB_HOST}:{DB_PORT}/{DB_NAME}")
    return _pool

def query_one(sql, params=()):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            row  = cur.fetchone()
            return dict(zip(cols, row)) if row else {}
    finally:
        pool.putconn(conn)

def query_all(sql, params=()):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        pool.putconn(conn)

def get_sites():
    rows = query_all("""
        SELECT site_name, display_name
        FROM sites WHERE enabled = TRUE
        ORDER BY display_name
    """)
    return [{'name': r['site_name'], 'display': r['display_name']} for r in rows]

def get_flow(site: str) -> dict:
    row = query_one("""
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
            WHERE site_name ILIKE %s
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
        'site':     site,
        'solar_w':  int(row['solar_w']  or 0),
        'batt_w':   int(row['battery_w'] or 0),
        'grid_w':   int(row['grid_w']   or 0),
        'load_w':   int(row['load_w']   or 0),
        'soc':      float(row['soc']      or 0),
        'batt_temp':float(row['batt_temp'] or 0),
        'batt_v':   float(row['batt_v']   or 0),
        'grid_v':   float(row['grid_v']   or 0),
        'grid_hz':  float(row['grid_hz']  or 0),
        'age_s':    age_s,
        'stale':    age_s is not None and age_s > 300,
    }

    # Daily counters — load, grid, PV, savings
    try:
        RATE = 4.50  # default flat rate R/kWh incl VAT — TODO: make configurable
        TODAY = """
            time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                     AT TIME ZONE 'Africa/Johannesburg' + INTERVAL '1 hour'
        """
        daily_row = query_one(f"""
            SELECT
              -- Use load from the inverter that reports grid (the grid-connected one)
              -- Falls back to MAX load if no inverter has grid data
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
              WHERE site_name ILIKE %s
              AND {TODAY}
              AND daily_load_energy > 0
              AND daily_load_energy < 200
              ORDER BY inverter_name, time DESC
            ) sub
        """, (site,))
        load = float(daily_row.get('load_kwh') or 1)
        grid = float(daily_row.get('grid_kwh') if daily_row.get('grid_kwh') is not None else 0)
        pv   = float(daily_row.get('pv_kwh')   or 0)
        d['self_suff']       = max(0, min(100, round((1 - grid / max(load, 0.001)) * 100)))
        d['daily_load_kwh']  = round(load, 1)
        d['daily_grid_kwh']  = round(grid, 1)
        d['daily_pv_kwh']    = round(pv,   1)
        d['solar_savings_r'] = round(max(0, (load - grid) * RATE), 2)
    except Exception as e:
        d['self_suff'] = 0
        log.warning(f"Daily counters error: {e}")

    return d

def get_monthly(site: str) -> dict:
    """Return this month's PV kWh and grid kWh for the site."""
    MONTH = """
        DATE_TRUNC('month', time AT TIME ZONE 'Africa/Johannesburg')
        = DATE_TRUNC('month', NOW() AT TIME ZONE 'Africa/Johannesburg')
    """
    # Monthly PV — SUM of end-of-day readings per inverter per day
    pv_row = query_one(f"""
        SELECT COALESCE(SUM(eod_pv), 0) AS month_pv_kwh
        FROM (
          SELECT DISTINCT ON (DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name)
            inverter_name,
            daily_pv_energy AS eod_pv
          FROM solar_readings
          WHERE {MONTH}
          AND site_name ILIKE %s
          AND daily_pv_energy IS NOT NULL
          AND daily_pv_energy > 0
          ORDER BY DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name, time DESC
        ) sub
    """, (site,))

    # Monthly grid — MAX per day from the grid-reporting inverter
    grid_row = query_one(f"""
        SELECT COALESCE(SUM(day_grid), 0) AS month_grid_kwh
        FROM (
          SELECT
            DATE(time AT TIME ZONE 'Africa/Johannesburg') AS day,
            MAX(daily_grid_import) FILTER (WHERE daily_grid_import > 0) AS day_grid
          FROM solar_readings
          WHERE {MONTH}
          AND site_name ILIKE %s
          AND daily_grid_import IS NOT NULL
          AND daily_grid_import BETWEEN 0.01 AND 9000
          GROUP BY 1
        ) sub
    """, (site,))

    month_pv   = float(pv_row.get('month_pv_kwh')   or 0)
    month_grid = float(grid_row.get('month_grid_kwh') or 0)

    return {
        'month_pv_kwh':   round(month_pv,   1),
        'month_grid_kwh': round(month_grid,  1),
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SolarWatch</title>
<meta name="description" content="Multi-site solar inverter monitoring — live power flow, battery SOC, grid and load data.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Barlow:wght@300;400;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0c10;--surface:#111318;--s2:#181c24;--border:#232736;
  --text:#e8eaf2;--muted:#8090b8;
  --solar:#f5a623;--green:#2ecc71;--amber:#f39c12;--red:#e74c3c;--load:#4fc3f7;
  --gin:#e74c3c;--gout:#2ecc71;--batt:#2ecc71;
  --mono:'DM Mono',monospace;--sans:'Barlow',sans-serif;
}
html,body{width:100%;height:100%;background:var(--bg);color:var(--text);font-family:var(--sans);overflow:hidden}
html.adv-scroll,html.adv-scroll body{overflow:auto;height:auto}

/* ── APP SHELL ── */
.app{display:grid;grid-template-rows:5.5vh auto 1fr 11vh;height:100vh;width:100vw}
.app.adv-mode{
  display:flex;flex-direction:column;
  height:auto;min-height:100vh;
  width:100vw;
}

/* ── HEADER ── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 2.5vw;background:var(--surface);border-bottom:1px solid var(--border);
  flex-shrink:0;
}
.logo{font-size:clamp(13px,1.4vw,20px);font-weight:800;letter-spacing:-.03em;display:flex;align-items:center;gap:.5vw}
.la{color:var(--solar)}.ls{color:var(--muted);font-weight:300;margin:0 .4vw}.lb{color:var(--muted);font-weight:400}
.hdr-right{display:flex;align-items:center;gap:1.5vw}

/* View toggle buttons */
.view-btns{display:flex;gap:6px}
.vbtn{
  font-family:var(--sans);font-size:clamp(10px,.95vw,14px);font-weight:700;
  padding:5px 14px;border-radius:6px;cursor:pointer;border:1px solid var(--border);
  background:var(--s2);color:var(--muted);letter-spacing:.06em;text-transform:uppercase;
  transition:all .2s;
}
.vbtn.active{background:var(--solar);color:#000;border-color:var(--solar)}
.vbtn:hover:not(.active){border-color:var(--muted);color:var(--text)}

/* Cycle button */
.cycle-btn{
  font-family:var(--sans);font-size:clamp(9px,.85vw,13px);font-weight:600;
  padding:5px 12px;border-radius:6px;cursor:pointer;border:1px solid var(--border);
  background:transparent;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;
  transition:all .2s;display:flex;align-items:center;gap:6px;
}
.cycle-btn.on{border-color:var(--green);color:var(--green)}
.cycle-btn:hover{border-color:var(--muted);color:var(--text)}
.cycle-dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.cycle-btn.on .cycle-dot{animation:pulse 1.5s infinite}

/* Rate selector */
.rate-wrap{display:flex;align-items:center;gap:8px;font-size:clamp(9px,.85vw,13px)}
.rate-lbl{color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
.rate-mode{display:flex;gap:4px}
.rbtn{
  font-family:var(--sans);font-size:clamp(9px,.85vw,12px);font-weight:700;
  padding:3px 10px;border-radius:5px;cursor:pointer;border:1px solid var(--border);
  background:transparent;color:var(--muted);letter-spacing:.06em;transition:all .2s;
}
.rbtn.on{background:rgba(245,166,35,.2);color:var(--solar);border-color:var(--solar)}
select{
  background:var(--s2);border:1px solid var(--border);color:var(--text);
  font-family:var(--sans);font-size:clamp(10px,1vw,14px);font-weight:700;
  padding:4px 28px 4px 10px;border-radius:6px;cursor:pointer;outline:none;
  appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%234a5070' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;
}
.rate-inp{
  width:70px;background:var(--s2);border:1px solid var(--border);color:var(--text);
  font-family:var(--mono);font-size:clamp(10px,.95vw,14px);font-weight:500;
  padding:4px 8px;border-radius:6px;outline:none;text-align:right;
}
.rate-inp:focus{border-color:var(--solar)}

.site-wrap{display:flex;align-items:center;gap:.8vw}
.site-lbl{font-size:clamp(9px,.9vw,13px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.age-badge{font-family:var(--mono);font-size:clamp(9px,.85vw,12px);color:var(--muted);display:flex;align-items:center;gap:6px}
.dot{width:clamp(6px,.6vw,8px);height:clamp(6px,.6vw,8px);border-radius:50%;background:var(--green);animation:pulse 2s infinite}
.dot.stale{background:var(--red);animation:none}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}

/* ── VIEWS ── */
.view{display:none;width:100%;height:100%;overflow:hidden}
.view.active{display:flex}

/* ── BASIC VIEW ── */
#view-basic{flex-direction:row}
.soc-panel{
  width:26vw;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:2.5vh;padding:2vh 2vw;border-right:1px solid var(--border);position:relative;overflow:hidden;flex-shrink:0;
}
.soc-panel::before{content:'';position:absolute;inset:0;pointer-events:none;transition:opacity .8s;opacity:0}
.soc-panel.sg::before{background:radial-gradient(circle at 50% 55%,rgba(46,204,113,.1) 0%,transparent 68%);opacity:1}
.soc-panel.sa::before{background:radial-gradient(circle at 50% 55%,rgba(243,156,18,.1) 0%,transparent 68%);opacity:1}
.soc-panel.sr::before{background:radial-gradient(circle at 50% 55%,rgba(231,76,60,.1) 0%,transparent 68%);opacity:1}
.s-badge{font-size:clamp(10px,1.1vw,16px);font-weight:700;letter-spacing:.12em;text-transform:uppercase;padding:5px 16px;border-radius:20px;transition:all .4s;z-index:1}
.sg .s-badge{background:rgba(46,204,113,.15);color:var(--green);border:1px solid rgba(46,204,113,.35)}
.sa .s-badge{background:rgba(243,156,18,.15);color:var(--amber);border:1px solid rgba(243,156,18,.35)}
.sr .s-badge{background:rgba(231,76,60,.15);color:var(--red);border:1px solid rgba(231,76,60,.35)}
.gauge-wrap{position:relative;width:min(20vw,30vh);aspect-ratio:1;flex-shrink:0;z-index:1}
.gauge-wrap svg{width:100%;height:100%}
.gauge-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.4vh}
.gauge-num{font-size:clamp(28px,4.8vw,68px);font-weight:800;line-height:1;font-family:var(--mono);transition:color .5s;letter-spacing:-.03em}
.gauge-lbl{font-size:clamp(9px,.85vw,13px);color:var(--muted);text-transform:uppercase;letter-spacing:.12em}
.s-msg{font-size:clamp(14px,1.8vw,28px);font-weight:700;text-align:center;line-height:1.25;letter-spacing:-.01em;transition:color .5s;z-index:1;padding:0 1vw}
.flow-area{flex:1;position:relative;overflow:hidden}
#flow-svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
.nc{
  position:absolute;transform:translate(-50%,-50%);background:var(--surface);
  border:1px solid var(--border);border-radius:clamp(10px,1.2vw,18px);
  padding:clamp(12px,1.4vh,22px) clamp(14px,1.6vw,28px);
  display:flex;flex-direction:column;align-items:center;gap:clamp(4px,.5vh,8px);
  width:clamp(130px,14vw,220px);transition:border-color .4s,box-shadow .4s;
}
.n-ico{font-size:clamp(24px,3.2vw,52px);line-height:1}
.n-lbl{font-size:clamp(9px,.85vw,13px);color:var(--muted);letter-spacing:.14em;text-transform:uppercase;font-weight:600}
.n-val{font-size:clamp(22px,3vw,48px);font-weight:800;font-family:var(--mono);line-height:1;letter-spacing:-.02em}
.n-sub{font-size:clamp(9px,.85vw,13px);font-family:var(--mono);font-weight:500}
.soc-bar{width:80%;height:clamp(4px,.5vh,7px);background:var(--s2);border-radius:4px;overflow:hidden}
.soc-bar-fill{height:100%;border-radius:4px;transition:width 1s ease,background .5s}
.hub{position:absolute;transform:translate(-50%,-50%);display:flex;flex-direction:column;align-items:center;gap:clamp(4px,.5vh,8px)}
.hub-ring{
  width:clamp(62px,7.5vw,118px);height:clamp(62px,7.5vw,118px);border-radius:50%;
  background:var(--surface);border:2px solid rgba(245,166,35,.35);
  display:flex;align-items:center;justify-content:center;font-size:clamp(26px,3.2vw,52px);
  box-shadow:0 0 clamp(16px,2vw,40px) rgba(245,166,35,.15),0 0 clamp(40px,5vw,80px) rgba(245,166,35,.06);
  transition:box-shadow .5s,border-color .5s;
}
.hub-ring.on{box-shadow:0 0 clamp(22px,2.8vw,56px) rgba(245,166,35,.4),0 0 clamp(55px,6.5vw,110px) rgba(245,166,35,.12);border-color:rgba(245,166,35,.65)}
.hub-lbl{font-size:clamp(8px,.75vw,11px);color:var(--muted);letter-spacing:.15em;text-transform:uppercase}
@keyframes df{to{stroke-dashoffset:-28}}
@keyframes dr{to{stroke-dashoffset:28}}
.fl{fill:none;stroke-width:2.5;stroke-linecap:round;stroke-dasharray:8 20}
@media(max-width:768px),(orientation:portrait) and (max-width:1024px){
  .fl{stroke-width:3.5;stroke-dasharray:10 16}
}
.fwd{animation:df 1s linear infinite}.rev{animation:dr 1s linear infinite}
.idle{opacity:.1;animation:none}.slow{animation-duration:2s}.med{animation-duration:1.1s}.fast{animation-duration:.5s}

/* ── ADVANCED VIEW ── */
#view-adv{flex-direction:column;overflow:hidden;height:100%}
.adv-inner{
  flex:1;overflow:visible;
  display:grid;
  grid-template-columns:1fr 1fr;
  /* Fixed heights — page scrolls instead of inner div */
  grid-template-rows:320px 320px 320px 320px 380px 260px;
  gap:1px;
  background:var(--border);
  width:100%;
}
/* Full-width panels */
.span2{grid-column:1/-1}
.chart-panel{
  background:var(--surface);padding:clamp(8px,1vh,14px) clamp(10px,1.2vw,18px);
  display:flex;flex-direction:column;gap:6px;overflow:hidden;
  min-height:0;
}
.chart-title{
  font-size:clamp(11px,1vw,15px);font-weight:700;color:var(--text);
  letter-spacing:-.01em;flex-shrink:0;
}
.chart-wrap{flex:1;position:relative;min-height:0;height:0}
.chart-wrap canvas{width:100%!important;height:100%!important}

/* Stat panels in advanced view — two rows */
.adv-stats{
  grid-column:1/-1;display:flex;flex-direction:column;
  flex-shrink:0;
}
.adv-row-a{
  display:flex;gap:1px;background:var(--border);
  height:min(12vh,100px);flex-shrink:0;
}
.adv-row-b{
  display:flex;gap:1px;background:var(--border);
  height:min(5.5vh,50px);flex-shrink:0;
  border-top:2px solid var(--bg);
}
.adv-stat{
  flex:1;background:var(--surface);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:3px;min-width:0;
}
.adv-row-a .adv-stat-lbl{font-size:clamp(8px,.75vw,11px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.adv-row-a .adv-stat-val{font-size:clamp(16px,2.2vw,36px);font-family:var(--mono);font-weight:700}
.adv-row-b .adv-stat-lbl{font-size:clamp(7px,.6vw,10px);color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
.adv-row-b .adv-stat-val{font-size:clamp(12px,1.1vw,18px);font-family:var(--mono);font-weight:600;color:var(--text)}
.adv-stat-lbl{font-size:clamp(9px,.8vw,12px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.adv-stat-val{font-size:clamp(18px,2.5vw,40px);font-family:var(--mono);font-weight:700}

/* Advanced footer inner rows */
#footer-adv > div{gap:1px;background:var(--border)}

/* Smaller stat pills for advanced footer rows */
.adv-footer-stat{
  flex:1;background:var(--surface);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:2px;min-width:0;
}
.adv-footer-lbl{font-size:clamp(7px,.6vw,10px);color:var(--muted);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;padding:0 4px;text-align:center}
.adv-footer-val{font-size:clamp(11px,1.2vw,20px);font-family:var(--mono);font-weight:600}

/* Technical stats strip — compact row above the charts in advanced view */
.tech-stats{
  display:flex;align-items:center;justify-content:center;
  gap:clamp(16px,3vw,48px);padding:6px 2.5vw;
  background:var(--s2);border-bottom:1px solid var(--border);
  flex-shrink:0;
}
.tech-stat{display:flex;align-items:center;gap:8px}
.tech-lbl{font-size:clamp(8px,.7vw,11px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.tech-val{font-size:clamp(12px,1.1vw,18px);font-family:var(--mono);font-weight:500;color:var(--text)}

/* ── FOOTER MODE SWITCHING — global, works on all screen sizes ── */
.footer-hidden{display:none!important}

/* ── FOOTER ── */
footer{
  border-top:1px solid var(--border);background:var(--surface);flex-shrink:0;
  overflow:hidden;
}
/* Basic mode footer — horizontal flex row */
#footer-basic{
  display:flex;align-items:center;justify-content:center;
  gap:clamp(8px,2.2vw,48px);padding:0 3vw;
  width:100%;height:100%;flex-wrap:wrap;
}
/* Advanced mode footer — two rows of stat pills */
#footer-adv{
  display:flex;flex-direction:column;
  width:100%;height:100%;
}
#footer-adv > div{
  display:flex;gap:1px;background:var(--border);flex:1;
}
.stat{display:flex;flex-direction:column;align-items:center;gap:3px}
.st-lbl{font-size:clamp(8px,.75vw,11px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.st-val{font-size:clamp(13px,1.5vw,24px);font-family:var(--mono);font-weight:500}

/* ── WEATHER STRIP ── */
.wx-strip{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 2.5vw;height:clamp(32px,4vh,48px);
  background:var(--s2);border-bottom:1px solid var(--border);
  flex-shrink:0;gap:1.5vw;overflow:hidden;
}
.wx-left{display:flex;align-items:center;gap:1vw;flex-shrink:0}
.wx-icon{font-size:clamp(16px,2vw,26px);line-height:1}
.wx-desc{font-size:clamp(10px,.9vw,14px);color:var(--text);font-weight:600;white-space:nowrap}
.wx-site{font-size:clamp(9px,.8vw,12px);color:var(--muted);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}
.wx-pills{display:flex;align-items:center;gap:clamp(6px,1.2vw,24px);flex-wrap:nowrap;overflow:hidden}
.wx-pill{display:flex;flex-direction:column;align-items:center;gap:2px;flex-shrink:0}
.wx-pill-lbl{font-size:clamp(7px,.65vw,10px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.wx-pill-val{font-size:clamp(11px,1.1vw,17px);font-family:var(--mono);font-weight:600;color:var(--text);white-space:nowrap}
.wx-right{display:flex;align-items:center;gap:1vw;flex-shrink:0}
.wx-sun{font-size:clamp(9px,.8vw,12px);color:var(--muted);font-family:var(--mono);white-space:nowrap}
/* Cloud cover bar */
.wx-cloud-wrap{display:flex;align-items:center;gap:6px}
.wx-cloud-bar{width:clamp(40px,5vw,80px);height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.wx-cloud-fill{height:100%;border-radius:3px;background:var(--muted);transition:width .8s ease}
/* Weather strip loading/no-data state */
.wx-strip.wx-empty{opacity:.4}

/* IBT rate selector (hidden when flat mode) */
#ibt-select{display:none}
#flat-input-wrap{display:flex;align-items:center;gap:4px}

/* ── MOBILE / TABLET RESPONSIVE ─────────────────────────────────────────────
   Breakpoint: ≤768px wide OR portrait orientation on any screen.
   All desktop styles above are completely untouched.
   We only add/override here.
──────────────────────────────────────────────────────────────────────────── */
@media (max-width:768px),(orientation:portrait) and (max-width:1024px){

  /* Allow page to scroll on mobile instead of hard-clipping */
  html,body{overflow:auto}

  /* Tighter grid rows: smaller header, auto weather strip, auto main, compact footer */
  .app{grid-template-rows:auto auto 1fr auto;min-height:100vh;height:auto}

  /* ── HEADER ── */
  header{
    flex-wrap:wrap;gap:6px;padding:8px 12px;
    height:auto;min-height:48px;
  }
  .logo{font-size:15px}
  /* Stack hdr-right controls into a wrapping row */
  .hdr-right{
    flex-wrap:wrap;gap:6px;width:100%;
    justify-content:flex-start;
  }
  /* Hide rate controls on mobile — too cramped, not critical at-a-glance */
  .rate-wrap{display:none}
  /* Hide cycle auto controls on mobile */
  .hdr-right > div:last-of-type{display:none}
  /* Age badge always visible */
  .age-badge{margin-left:auto}
  /* Site select full-width-ish */
  .site-wrap{gap:6px}
  /* Buttons a bit larger for touch */
  .vbtn{padding:7px 16px;font-size:12px}

  /* ── BASIC VIEW — column layout, fill viewport ── */
  #view-basic{
    flex-direction:column;
    height:auto;
    min-height:0;
  }

  /* SOC panel: compact horizontal strip across the top */
  .soc-panel{
    width:100%;
    flex-direction:row;
    border-right:none;
    border-bottom:1px solid var(--border);
    padding:8px 14px;
    gap:12px;
    justify-content:flex-start;
    align-items:center;
    flex-shrink:0;
    min-height:0;
    max-height:90px;
  }
  /* Shrink the gauge on mobile */
  .gauge-wrap{
    width:clamp(64px,18vw,96px);
    flex-shrink:0;
  }
  .gauge-num{font-size:clamp(18px,5vw,32px)}
  .gauge-lbl{font-size:9px}
  /* Status text group: badge + message stacked */
  .soc-panel > .s-badge{order:2;font-size:11px;padding:4px 10px}
  .soc-panel > .s-msg{
    order:3;font-size:clamp(12px,3.5vw,18px);
    text-align:left;padding:0;
  }

  /* Flow area: square-ish on mobile for balanced node layout */
  .flow-area{
    width:100%;
    aspect-ratio:1.1/1;
    min-height:260px;
    max-height:420px;
    flex-shrink:0;
  }

  /* Node cards: sized for phone screens */
  .nc{
    width:clamp(95px,24vw,140px);
    padding:8px 10px;
    border-radius:10px;
    gap:3px;
  }
  .n-ico{font-size:clamp(20px,5.5vw,32px)}
  .n-val{font-size:clamp(15px,5vw,26px)}
  .n-lbl{font-size:9px}
  .n-sub{font-size:9px}

  /* Hub ring: balanced size for phone */
  .hub-ring{
    width:clamp(48px,12vw,68px);
    height:clamp(48px,12vw,68px);
    font-size:clamp(20px,5.5vw,32px);
  }
  .hub-lbl{font-size:8px}

  /* ── FOOTER ── */
  footer{height:auto;padding:0}
  #footer-basic{
    padding:8px 12px;gap:0;
    justify-items:center;align-items:center;
    width:100%;
  }
  /* Mobile footer advanced — 4 columns */
  #footer-basic:not(.footer-hidden){display:grid!important;
    grid-template-columns:repeat(4,1fr);}
  #footer-basic .stat:nth-child(1){order:1}
  #footer-basic .stat:nth-child(2){order:2}
  #footer-basic .stat:nth-child(3){order:3}
  #footer-basic .stat:nth-child(4){order:4}
  #footer-basic .stat:nth-child(5){order:5}
  #footer-basic .stat:nth-child(6){order:6}
  #footer-basic .stat:nth-child(7){order:7}
  #footer-basic .stat:nth-child(8){order:8}
  #footer-basic .stat:nth-child(9){order:9}
  #footer-basic #clock-wrap{display:none}
  /* Advanced footer on mobile — row 1: 4-column grid, row 2: peaks only */
  #footer-adv{flex-direction:column!important}
  #footer-adv > div:first-child{
    display:grid!important;
    grid-template-columns:repeat(4,1fr);
    gap:1px;background:var(--border);
    flex:none!important;
  }
  /* Row 2: show but hide tech stats (Battery V, Batt Temp, Grid V, Freq) — keep peaks */
  #footer-adv > div:last-child{
    display:grid!important;
    grid-template-columns:repeat(3,1fr);
    gap:1px;background:var(--border);
    flex:none!important;
  }
  /* Hide first 4 items in row 2 (tech stats) — keep last 3 (peaks) */
  #footer-adv > div:last-child > div:nth-child(1),
  #footer-adv > div:last-child > div:nth-child(2),
  #footer-adv > div:last-child > div:nth-child(3),
  #footer-adv > div:last-child > div:nth-child(4){display:none!important}
  /* Hide month grid cost in row 1 to keep 4-col clean */
  #footer-adv > div:first-child > div:last-child{display:none!important}
  #footer-adv .adv-footer-stat{padding:4px 2px}
  #footer-adv .adv-footer-val{font-size:clamp(10px,3.5vw,16px)}
  #footer-adv .adv-footer-lbl{font-size:clamp(6px,1.8vw,9px)}

  .st-lbl{font-size:9px}
  .st-val{font-size:clamp(12px,3.5vw,18px)}
  /* Clock is huge on desktop — reset to normal on mobile */
  #clock{font-size:clamp(12px,3.5vw,18px)!important;font-weight:500!important}

  /* ── WEATHER STRIP mobile ── */
  .wx-strip{
    height:auto;
    flex-wrap:wrap;
    padding:5px 10px;
    gap:6px;
  }
  .wx-pills{flex-wrap:wrap;gap:6px 10px}
  .wx-pill{min-width:52px}
  .wx-right{display:none} /* hide sunrise/sunset on mobile — space is premium */

  /* ── ADVANCED VIEW ── */
  /* Single column on mobile */
  .adv-inner{
    grid-template-columns:1fr;
    grid-template-rows:none;
    overflow-y:auto;
    height:auto;
  }
  /* All panels go full width */
  .adv-inner .chart-panel{
    min-height:220px;
  }
  /* Stat rows on mobile */
  .adv-row-a{height:auto;flex-wrap:wrap}
  .adv-row-b{display:none!important} /* peaks shown in footer row 2 instead */
  .adv-row-a .adv-stat{flex:0 0 33.333%;min-width:0;padding:5px 3px;border-bottom:1px solid var(--border)}
  .adv-row-a .adv-stat-lbl{font-size:8px}
  .adv-row-a .adv-stat-val{font-size:clamp(13px,4vw,20px)}
}

/* Extra-small phones (≤380px) — tighten further */
@media (max-width:380px){
  .nc{width:80px;padding:6px 7px}
  .n-val{font-size:13px}
  footer{grid-template-columns:repeat(4,1fr)}
  .st-val{font-size:11px}
  .adv-stat{flex:0 0 50%}
}
/* Crosshair tooltip label */
.ch-crosshair-label{
  position:fixed;background:rgba(17,19,24,.92);border:1px solid var(--border);
  color:var(--muted);font-family:var(--mono);font-size:11px;
  padding:2px 6px;border-radius:4px;pointer-events:none;
  transform:translateX(-50%);white-space:nowrap;z-index:100;
}
</style>
</head>
<body>
<div class="app">

<!-- HEADER -->
<header>
  <div class="logo"><span class="la">◆</span> SolarWatch <span class="ls">/</span> <span class="lb" id="view-label">Power Flow</span></div>
  <div class="hdr-right">

    <!-- Rate selector -->
    <div class="rate-wrap">
      <span class="rate-lbl">Rate</span>
      <div class="rate-mode">
        <button class="rbtn on" id="rbtn-flat" onclick="setRateMode('flat')">Flat</button>
        <button class="rbtn" id="rbtn-ibt" onclick="setRateMode('ibt')">IBT</button>
      </div>
      <div id="flat-input-wrap">
        <span style="font-size:clamp(10px,.9vw,13px);color:var(--muted)">R</span>
        <input class="rate-inp" type="number" id="flat-rate" aria-label="Flat electricity rate in Rand per kWh" value="4.50" step="0.10" min="1" max="10" oninput="onRateChange()">
        <span style="font-size:clamp(9px,.8vw,12px);color:var(--muted)">/kWh</span>
      </div>
      <select id="ibt-select" aria-label="IBT tariff schedule" onchange="onRateChange()">
        <option value="ibt_2025">IBT 2025/26 (Tshwane)</option>
      </select>
    </div>

    <!-- Site selector -->
    <div class="site-wrap">
      <span class="site-lbl">Site</span>
      <select id="site-sel" aria-label="Select site" onchange="onSiteChange()"></select>
    </div>

    <!-- View toggle -->
    <div class="view-btns">
      <button class="vbtn active" id="vbtn-basic" onclick="setView('basic')">Basic</button>
      <button class="vbtn" id="vbtn-adv" onclick="setView('adv')">Advanced</button>
    </div>

    <!-- Auto-cycle with duration selector -->
    <div style="display:flex;align-items:center;gap:6px">
      <button class="cycle-btn" id="cycle-btn" onclick="toggleCycle()">
        <span class="cycle-dot"></span>
        <span id="cycle-lbl">Auto</span>
      </button>
      <select id="cycle-dur" aria-label="Auto-cycle duration" onchange="onCycleDurChange()" style="padding:4px 8px;font-size:clamp(9px,.85vw,12px);border-radius:6px;background:var(--s2);border:1px solid var(--border);color:var(--muted);font-family:var(--sans);cursor:pointer;outline:none">
        <option value="10">10s</option>
        <option value="15" selected>15s</option>
        <option value="20">20s</option>
        <option value="30">30s</option>
        <option value="45">45s</option>
        <option value="60">1m</option>
        <option value="120">2m</option>
        <option value="300">5m</option>
      </select>
    </div>

    <!-- Age -->
    <div class="age-badge"><div class="dot" id="dot"></div><span id="age">—</span></div>
  </div>
</header>

<!-- WEATHER STRIP -->
<div class="wx-strip wx-empty" id="wx-strip">
  <div class="wx-left">
    <span class="wx-icon" id="wx-icon">—</span>
    <div>
      <div class="wx-desc" id="wx-desc">Loading weather…</div>
      <div class="wx-site" id="wx-site">—</div>
    </div>
  </div>
  <div class="wx-pills">
    <div class="wx-pill">
      <span class="wx-pill-lbl">Temp</span>
      <span class="wx-pill-val" id="wx-temp">—</span>
    </div>
    <div class="wx-pill">
      <span class="wx-pill-lbl">Feels</span>
      <span class="wx-pill-val" id="wx-feels">—</span>
    </div>
    <div class="wx-pill">
      <span class="wx-pill-lbl">Cloud</span>
      <span class="wx-pill-val">
        <span class="wx-cloud-wrap">
          <span id="wx-cloud-pct">—</span>
          <span class="wx-cloud-bar"><span class="wx-cloud-fill" id="wx-cloud-bar"></span></span>
        </span>
      </span>
    </div>
    <div class="wx-pill">
      <span class="wx-pill-lbl">Rain/hr</span>
      <span class="wx-pill-val" id="wx-rain">—</span>
    </div>
    <div class="wx-pill">
      <span class="wx-pill-lbl">Wind</span>
      <span class="wx-pill-val" id="wx-wind">—</span>
    </div>
    <div class="wx-pill">
      <span class="wx-pill-lbl">Humidity</span>
      <span class="wx-pill-val" id="wx-hum">—</span>
    </div>
    <div class="wx-pill">
      <span class="wx-pill-lbl">UV</span>
      <span class="wx-pill-val" id="wx-uv">—</span>
    </div>
    <div class="wx-pill" id="wx-rad-pill">
      <span class="wx-pill-lbl" id="wx-rad-lbl">Solar Rad</span>
      <span class="wx-pill-val" id="wx-rad">—</span>
    </div>
  </div>
  <div class="wx-right">
    <span class="wx-sun" id="wx-sun">🌅 — · 🌇 —</span>
  </div>
</div>

<!-- VIEWS -->
<main style="flex:1;overflow:hidden;display:flex;flex-direction:column">

  <!-- BASIC VIEW -->
  <div class="view active" id="view-basic">
    <div class="soc-panel sg" id="sp">
      <div class="s-badge" id="sbadge">GOOD</div>
      <div class="gauge-wrap">
        <svg viewBox="0 0 200 200">
          <circle cx="100" cy="100" r="80" fill="none" stroke="#1a1e2a" stroke-width="14"
            stroke-dasharray="440" stroke-dashoffset="110" stroke-linecap="round" transform="rotate(135 100 100)"/>
          <circle cx="100" cy="100" r="80" id="garc" fill="none" stroke="var(--green)" stroke-width="14"
            stroke-dasharray="330 440" stroke-dashoffset="0" stroke-linecap="round" transform="rotate(135 100 100)"
            style="transition:stroke-dasharray 1s ease,stroke .5s"/>
        </svg>
        <div class="gauge-center">
          <span class="gauge-num" id="gnum" style="color:var(--green)">—%</span>
          <span class="gauge-lbl">Battery SOC</span>
        </div>
      </div>
      <div class="s-msg" id="smsg">Loading...</div>
    </div>
    <div class="flow-area" id="fa">
      <svg id="flow-svg" xmlns="http://www.w3.org/2000/svg"></svg>
      <div class="hub" id="hub" style="left:50%;top:50%">
        <div class="hub-ring" id="hring">⚡</div>
        <span class="hub-lbl">Inverter</span>
      </div>
      <div class="nc" id="nd-solar" style="left:27%;top:23%">
        <span class="n-ico">☀️</span><span class="n-lbl">Solar</span>
        <span class="n-val" id="v-solar" style="color:var(--solar)">—</span>
      </div>
      <div class="nc" id="nd-grid" style="left:73%;top:23%">
        <span class="n-ico">🔌</span><span class="n-lbl">Grid</span>
        <span class="n-val" id="v-grid">—</span>
        <span class="n-sub" id="s-grid">—</span>
      </div>
      <div class="nc" id="nd-batt" style="left:27%;top:77%">
        <span class="n-ico">🔋</span><span class="n-lbl">Battery</span>
        <span class="n-val" id="v-batt">—</span>
        <div class="soc-bar"><div class="soc-bar-fill" id="sbar"></div></div>
        <span class="n-sub" id="s-batt">—</span>
      </div>
      <div class="nc" id="nd-load" style="left:73%;top:77%">
        <span class="n-ico">🏠</span><span class="n-lbl">Load</span>
        <span class="n-val" id="v-load" style="color:var(--load)">—</span>
      </div>
    </div>
  </div>

  <!-- ADVANCED VIEW -->
  <div class="view" id="view-adv">
    <!-- Top stat pills: two rows -->
    <div class="adv-stats" id="adv-stats">
      <!-- Row A: Operational -->
      <div class="adv-row-a">
        <div class="adv-stat"><span class="adv-stat-lbl">Battery SOC</span><span class="adv-stat-val" id="a-soc" style="color:var(--green)">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Self-Suff</span><span class="adv-stat-val" id="a-ss">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">PV Today</span><span class="adv-stat-val" id="a-pv-today" style="color:var(--solar)">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Solar Savings</span><span class="adv-stat-val" id="a-savings" style="color:var(--green)">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Grid Today</span><span class="adv-stat-val" id="a-grid-today" style="color:var(--muted)">—</span></div>
      </div>
      <!-- Row B: Diagnostic + peaks -->
      <div class="adv-row-b" id="tech-stats">
        <div class="adv-stat"><span class="adv-stat-lbl">Battery V</span><span class="adv-stat-val" id="a-bv">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Batt Temp</span><span class="adv-stat-val" id="a-bt">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Grid V</span><span class="adv-stat-val" id="a-gv">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Frequency</span><span class="adv-stat-val" id="a-hz">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Peak Solar</span><span class="adv-stat-val" id="a-peak-pv" style="color:var(--solar)">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Peak Load</span><span class="adv-stat-val" id="a-peak-load" style="color:var(--amber)">—</span></div>
        <div class="adv-stat"><span class="adv-stat-lbl">Peak Grid Draw</span><span class="adv-stat-val" id="a-peak-grid" style="color:var(--red)">—</span></div>
      </div>
    </div>
    <!-- Chart grid -->
    <div class="adv-inner" id="adv-inner">
      <div class="chart-panel span2"><div class="chart-title">☀️ Solar PV Power — Per Inverter &amp; Combined</div><div class="chart-wrap"><canvas id="ch-pv"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">🔋 Battery Power &amp; SOC</div><div class="chart-wrap"><canvas id="ch-batt"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">🔌 Grid Power &amp; Voltage</div><div class="chart-wrap"><canvas id="ch-grid"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">🏠 Load Power — Per Inverter &amp; Combined</div><div class="chart-wrap"><canvas id="ch-load"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">📊 Daily Energy — Combined Site (Last 14 Days)</div><div class="chart-wrap"><canvas id="ch-daily"></canvas></div></div>
      <div class="chart-panel span2 temp-panel" id="temp-panel-wrap">
        <div class="chart-title" style="display:flex;align-items:center;justify-content:space-between;cursor:pointer" onclick="toggleTemps()">
          <span>🌡️ Inverter Temperatures</span>
          <span id="temp-toggle-icon" style="font-size:clamp(10px,.9vw,14px);color:var(--muted);letter-spacing:.06em">▼ HIDE</span>
        </div>
        <div class="chart-wrap" id="temp-chart-wrap"><canvas id="ch-temp"></canvas></div>
      </div>
    </div>
  </div>

</main>

<!-- FOOTER — basic mode: compact stats bar / advanced mode: full stat pills -->
<footer id="main-footer">

  <!-- BASIC MODE footer (default) -->
  <div id="footer-basic">
    <div class="stat"><span class="st-lbl">Self-Suff</span><span class="st-val" id="f-ss">—</span></div>
    <div class="stat"><span class="st-lbl">PV Today</span><span class="st-val" id="f-pv" style="color:var(--solar)">—</span></div>
    <div class="stat"><span class="st-lbl">Load Today</span><span class="st-val" id="f-tl">—</span></div>
    <div class="stat"><span class="st-lbl">Solar Savings</span><span class="st-val" id="f-sv" style="color:var(--green)">—</span></div>
    <div class="stat"><span class="st-lbl">Grid Today</span><span class="st-val" id="f-tg">—</span></div>
    <div class="stat"><span class="st-lbl">Month PV</span><span class="st-val" id="f-mpv" style="color:var(--solar)">—</span></div>
    <div class="stat"><span class="st-lbl">Month PV Value</span><span class="st-val" id="f-mpvr" style="color:var(--green)">—</span></div>
    <div class="stat"><span class="st-lbl">Month Grid</span><span class="st-val" id="f-mgrid">—</span></div>
    <div class="stat"><span class="st-lbl">Month Grid Cost</span><span class="st-val" id="f-mgridr" style="color:var(--red)">—</span></div>
    <div class="stat" id="clock-wrap" onclick="toggleClock()" title="Click to toggle clock" style="cursor:pointer;margin-left:auto;padding-left:clamp(8px,2vw,32px);opacity:0.4;transition:opacity .2s" onmouseenter="this.style.opacity=1" onmouseleave="this.style.opacity=clockVisible?1:0.4">
      <span class="st-lbl">Time</span>
      <span class="st-val" id="clock" style="font-family:var(--mono);color:var(--muted);font-size:clamp(20px,2.4vw,40px);font-weight:600">--:--:--</span>
    </div>
  </div>

  <!-- ADVANCED MODE footer — two rows of stat pills -->
  <div id="footer-adv" class="footer-hidden">
    <!-- Row 1: Daily + monthly energy -->
    <div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Self-Suff</span><span class="adv-footer-val" id="fa-ss">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">PV Today</span><span class="adv-footer-val" id="fa-pv" style="color:var(--solar)">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Load Today</span><span class="adv-footer-val" id="fa-tl">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Solar Savings</span><span class="adv-footer-val" id="fa-sv" style="color:var(--green)">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Grid Today</span><span class="adv-footer-val" id="fa-tg">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Month PV</span><span class="adv-footer-val" id="fa-mpv" style="color:var(--solar)">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Month PV Value</span><span class="adv-footer-val" id="fa-mpvr" style="color:var(--green)">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Month Grid</span><span class="adv-footer-val" id="fa-mgrid">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Month Grid Cost</span><span class="adv-footer-val" id="fa-mgridr" style="color:var(--red)">—</span></div>
    </div>
    <!-- Row 2: Technical + peaks -->
    <div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Battery V</span><span class="adv-footer-val" id="fa-bv">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Batt Temp</span><span class="adv-footer-val" id="fa-bt">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Grid V</span><span class="adv-footer-val" id="fa-gv">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Frequency</span><span class="adv-footer-val" id="fa-hz">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Peak Solar</span><span class="adv-footer-val" id="fa-peak-pv" style="color:var(--solar)">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Peak Load</span><span class="adv-footer-val" id="fa-peak-load" style="color:var(--amber)">—</span></div>
      <div class="adv-footer-stat"><span class="adv-footer-lbl">Peak Grid Draw</span><span class="adv-footer-val" id="fa-peak-grid" style="color:var(--red)">—</span></div>
    </div>
  </div>

</footer>

</div>

<script>
// ── STATE ─────────────────────────────────────────────────────────────────────
let currentSite=null, currentView='basic', cycleOn=false, cycleTimer=null;
let cycleDuration=15; // seconds
let clockVisible=false, clockTimer=null;
let rateMode='flat', flatRate=4.50;
let liveTimer=null, chartTimer=null;
let charts={};
window._ld=null;

const IBT_RATES=[2.9790,3.4864,3.7983,4.0948]; // Tshwane 2025/26 excl VAT ×1.15

// ── RATE CALC ─────────────────────────────────────────────────────────────────
function calcCost(kwh){
  if(rateMode==='flat') return kwh*flatRate;
  // IBT Tshwane 2025/26 incl VAT
  const r=IBT_RATES;
  if(kwh<=0)  return 0;
  if(kwh<=100) return kwh*r[0]*1.15;
  if(kwh<=400) return(100*r[0]+(kwh-100)*r[1])*1.15;
  if(kwh<=650) return(100*r[0]+300*r[1]+(kwh-400)*r[2])*1.15;
  return(100*r[0]+300*r[1]+250*r[2]+(kwh-650)*r[3])*1.15;
}

function setRateMode(mode){
  rateMode=mode;
  document.getElementById('rbtn-flat').className='rbtn'+(mode==='flat'?' on':'');
  document.getElementById('rbtn-ibt').className='rbtn'+(mode==='ibt'?' on':'');
  document.getElementById('flat-input-wrap').style.display=mode==='flat'?'flex':'none';
  document.getElementById('ibt-select').style.display=mode==='ibt'?'block':'none';
  if(window._ld) updateFooterCosts(window._ld);
  if(window._monthly) renderMonthly(window._monthly);
}

function onRateChange(){
  flatRate=parseFloat(document.getElementById('flat-rate').value)||4.50;
  if(window._ld) updateFooterCosts(window._ld);
  if(window._monthly) renderMonthly(window._monthly);
}

function updateFooterCosts(d){
  const load=d.daily_load_kwh||0, grid=d.daily_grid_kwh||0;
  const savings=Math.max(0, calcCost(load)-calcCost(grid));
  const s='R'+savings.toFixed(2);
  document.getElementById('f-sv').textContent=s;
  document.getElementById('fa-sv').textContent=s;
  document.getElementById('a-savings').textContent=s;
}

// ── VIEW MANAGEMENT ───────────────────────────────────────────────────────────
function setView(v){
  currentView=v;
  document.getElementById('view-basic').className='view'+(v==='basic'?' active':'');
  document.getElementById('view-adv').className='view'+(v==='adv'?' active':'');
  document.getElementById('vbtn-basic').className='vbtn'+(v==='basic'?' active':'');
  document.getElementById('vbtn-adv').className='vbtn'+(v==='adv'?' active':'');
  document.getElementById('view-label').textContent=v==='basic'?'Power Flow':'Advanced';
  // Toggle footer mode
  const fb=document.getElementById('footer-basic');
  const fa=document.getElementById('footer-adv');
  fb.classList.toggle('footer-hidden', v!=='basic');
  fa.classList.toggle('footer-hidden', v!=='adv');
  // Hide tech-stats strip in advanced view — those stats now live in the footer pills
  const ts=document.getElementById('tech-stats');
  if(ts) ts.style.display=v==='adv'?'none':'flex';
  document.querySelector('.app').className='app'+(v==='adv'?' adv-mode':'');
  // Toggle scroll mode for advanced view
  document.documentElement.classList.toggle('adv-scroll', v==='adv');
  if(v==='adv'){ window.scrollTo(0,0); loadCharts(); }
}

function toggleCycle(){
  cycleOn=!cycleOn;
  const btn=document.getElementById('cycle-btn');
  btn.className='cycle-btn'+(cycleOn?' on':'');
  document.getElementById('cycle-lbl').textContent=cycleOn?'Auto: ON':'Auto';
  if(cycleOn) startCycle(); else{if(cycleTimer)clearInterval(cycleTimer);}
}

function startCycle(){
  if(cycleTimer)clearInterval(cycleTimer);
  cycleTimer=setInterval(()=>{
    setView(currentView==='basic'?'adv':'basic');
  }, cycleDuration * 1000);
}

function onCycleDurChange(){
  cycleDuration = parseInt(document.getElementById('cycle-dur').value);
  if(cycleOn){ clearInterval(cycleTimer); startCycle(); } // restart with new duration
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function fmt(w){const n=parseFloat(w)||0;const a=Math.abs(n);return a>=1000?(a/1000).toFixed(1)+' kW':Math.round(a)+' W'}
function spd(w){const a=Math.abs(w);if(a<15)return 'idle';if(a<600)return 'slow';if(a<2500)return 'med';return 'fast'}
function fdir(w,inv=false){if(Math.abs(w)<15)return 'idle';return(inv?w<0:w>0)?'fwd':'rev'}
function sc(s){return s>60?'var(--green)':s>25?'var(--amber)':'var(--red)'}
function status(d){
  const hr=new Date().toLocaleString('en-ZA',{hour:'numeric',hour12:false,timeZone:'Africa/Johannesburg'})*1;
  const isNight=hr>=18||hr<6;
  const isMorning=hr>=6&&hr<10;

  if(isNight){
    if(d.soc>70) return{cls:'sg',badge:'GOOD OVERNIGHT RESERVE',msg:'Sufficient charge for the night ✓'};
    if(d.soc>40) return{cls:'sa',badge:'MODERATE RESERVE',msg:'Limit heavy appliances tonight'};
    return{cls:'sr',badge:'LOW RESERVE',msg:'Avoid high loads — battery running low'};
  }
  if(isMorning&&d.solar_w<500){
    if(d.soc>60) return{cls:'sg',badge:'GOOD',msg:'Solar charging — safe to use appliances'};
    if(d.soc>30) return{cls:'sa',badge:'MODERATE',msg:'Wait for solar to ramp up'};
    return{cls:'sr',badge:'CONSERVE POWER',msg:'Avoid heavy appliances'};
  }
  if(d.soc>80||d.solar_w>3000) return{cls:'sg',badge:'PLENTY OF POWER',msg:'Safe to run heavy appliances ✓'};
  if(d.soc>40||d.solar_w>1000) return{cls:'sa',badge:'MODERATE',msg:'Light usage recommended'};
  return{cls:'sr',badge:'CONSERVE POWER',msg:'Avoid heavy appliances'};
}

// ── FLOW LINES ────────────────────────────────────────────────────────────────
function drawLines(d){
  const fa=document.getElementById('fa');
  const svg=document.getElementById('flow-svg');
  const W=fa.offsetWidth,H=fa.offsetHeight;
  const pos={hub:{x:W*.50,y:H*.50},solar:{x:W*.27,y:H*.23},grid:{x:W*.73,y:H*.23},batt:{x:W*.27,y:H*.77},load:{x:W*.73,y:H*.77}};
  const bc=d.batt_w<-15?'var(--green)':'var(--amber)';
  const gc=d.grid_w>15?'var(--gin)':d.grid_w<-15?'var(--gout)':'var(--muted)';
  const pad=Math.min(W,H)*.09;
  function seg(a,b,di,sp,col){
    const dx=b.x-a.x,dy=b.y-a.y,len=Math.sqrt(dx*dx+dy*dy);
    const ux=dx/len,uy=dy/len;
    const x1=a.x+ux*pad,y1=a.y+uy*pad,x2=b.x-ux*pad,y2=b.y-uy*pad;
    const mx=(x1+x2)/2+(dy/len)*18,my=(y1+y2)/2-(dx/len)*18;
    const p=document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d',`M${x1},${y1} Q${mx},${my} ${x2},${y2}`);
    p.setAttribute('class',`fl ${di} ${sp}`);
    p.setAttribute('stroke',col);
    return p;
  }
  svg.innerHTML='';svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.appendChild(seg(pos.solar,pos.hub,fdir(d.solar_w),spd(d.solar_w),'var(--solar)'));
  svg.appendChild(seg(pos.grid,pos.hub,fdir(d.grid_w),spd(d.grid_w),gc));
  svg.appendChild(seg(pos.hub,pos.batt,fdir(-d.batt_w),spd(d.batt_w),bc));
  svg.appendChild(seg(pos.hub,pos.load,fdir(d.load_w),spd(d.load_w),'var(--load)'));
}

// ── RENDER BASIC VIEW ─────────────────────────────────────────────────────────
function render(d){
  window._ld=d;
  const soc=d.soc,bc=d.batt_w<-15,battC=bc?'var(--green)':'var(--amber)';
  const gc=d.grid_w>15?'var(--gin)':d.grid_w<-15?'var(--gout)':'var(--muted)';
  const gl=d.grid_w>15?'Importing':d.grid_w<-15?'Exporting':'Idle';
  const bl=`${soc%1===0?soc:soc.toFixed(1)}% · ${bc?'Charging':d.batt_w>15?'Discharging':'Idle'}`;
  const st=status(d);

  // Gauge
  const filled=(soc/100)*330;
  document.getElementById('garc').setAttribute('stroke-dasharray',`${filled} 440`);
  document.getElementById('garc').setAttribute('stroke',sc(soc));
  document.getElementById('gnum').textContent=(soc%1===0?soc:soc.toFixed(1))+'%';
  document.getElementById('gnum').style.color=sc(soc);

  // Status
  document.getElementById('sp').className='soc-panel '+st.cls;
  document.getElementById('sbadge').textContent=st.badge;
  // Append sunrise time to night message
  let msg = st.msg;
  if(window._wx && !window._wx.is_day && window._wx.sunrise){
    const srMs = new Date(window._wx.sunrise).getTime();
    const diffMs = srMs - Date.now();
    if(diffMs > 0){
      const hrs = Math.floor(diffMs/3600000);
      const mins = Math.floor((diffMs%3600000)/60000);
      msg += ' · ☀️ '+hrs+'h '+mins+'m';
    }
  }
  document.getElementById('smsg').textContent=msg;
  document.getElementById('smsg').style.color=st.cls==='sg'?'var(--green)':st.cls==='sa'?'var(--amber)':'var(--red)';

  // Nodes
  document.getElementById('v-solar').textContent=fmt(d.solar_w);
  document.getElementById('v-grid').textContent=fmt(d.grid_w);
  document.getElementById('v-grid').style.color=gc;
  document.getElementById('s-grid').textContent=gl;document.getElementById('s-grid').style.color=gc;
  document.getElementById('v-batt').textContent=fmt(d.batt_w);
  document.getElementById('v-batt').style.color=battC;
  document.getElementById('s-batt').textContent=bl;document.getElementById('s-batt').style.color=battC;
  document.getElementById('sbar').style.width=soc+'%';
  document.getElementById('sbar').style.background=sc(soc);
  document.getElementById('v-load').textContent=fmt(d.load_w);
  document.getElementById('nd-solar').style.cssText+=`;border-color:${d.solar_w>50?'rgba(245,166,35,.45)':''};box-shadow:${d.solar_w>50?'0 0 20px rgba(245,166,35,.08)':'none'}`;
  document.getElementById('nd-batt').style.borderColor=Math.abs(d.batt_w)>15?'rgba(46,204,113,.4)':'';
  document.getElementById('nd-grid').style.borderColor=Math.abs(d.grid_w)>15?(d.grid_w>0?'rgba(231,76,60,.4)':'rgba(46,204,113,.4)'):'';
  document.getElementById('nd-load').style.borderColor=d.load_w>50?'rgba(79,195,247,.35)':'';
  document.getElementById('hring').className='hub-ring'+(d.solar_w>50?' on':'');

  // Tech stats — basic tech strip + advanced footer pills
  const bv=d.batt_v.toFixed(1)+'V';
  const bt=d.batt_temp>5?d.batt_temp.toFixed(1)+'°C':'—';
  const gv=d.grid_v.toFixed(0)+'V';
  const hz=d.grid_hz.toFixed(2)+'Hz';
  document.getElementById('a-bv').textContent=bv;
  document.getElementById('a-bt').textContent=bt;
  document.getElementById('a-gv').textContent=gv;
  document.getElementById('a-hz').textContent=hz;
  // Advanced footer pills — tech stats
  document.getElementById('fa-bv').textContent=bv;
  document.getElementById('fa-bt').textContent=bt;
  document.getElementById('fa-gv').textContent=gv;
  document.getElementById('fa-hz').textContent=hz;
  const ss=d.self_suff??0;
  const ssColor=ss>80?'var(--green)':ss>50?'var(--amber)':'var(--red)';
  // Basic footer
  const sse=document.getElementById('f-ss');
  sse.textContent=ss+'%';sse.style.color=ssColor;
  document.getElementById('f-pv').textContent=(d.daily_pv_kwh??0)+' kWh';
  document.getElementById('f-tl').textContent=(d.daily_load_kwh??0)+' kWh';
  document.getElementById('f-tg').textContent=(d.daily_grid_kwh??0)+' kWh';
  // Advanced footer pills — daily stats
  const fass=document.getElementById('fa-ss');
  fass.textContent=ss+'%';fass.style.color=ssColor;
  document.getElementById('fa-pv').textContent=(d.daily_pv_kwh??0)+' kWh';
  document.getElementById('fa-tl').textContent=(d.daily_load_kwh??0)+' kWh';
  document.getElementById('fa-tg').textContent=(d.daily_grid_kwh??0)+' kWh';
  updateFooterCosts(d);

  // Advanced stat pills (update even if not visible)
  document.getElementById('a-soc').textContent=(soc%1===0?soc:soc.toFixed(1))+'%';
  document.getElementById('a-soc').style.color=sc(soc);
  document.getElementById('a-pv-today').textContent=(d.daily_pv_kwh??0)+' kWh';
  document.getElementById('a-ss').textContent=ss+'%';
  document.getElementById('a-ss').style.color=ss>80?'var(--green)':ss>50?'var(--amber)':'var(--red)';
  document.getElementById('a-grid-today').textContent=(d.daily_grid_kwh??0)+' kWh';

  document.getElementById('dot').className='dot'+(d.stale?' stale':'');
  document.getElementById('age').textContent=d.age_s!==null?d.age_s+'s ago':'live';

  drawLines(d);
}

// ── CHART HELPERS ─────────────────────────────────────────────────────────────
function mkOpts(){
  const isMob=window.innerWidth<=768||( window.innerWidth<=1024&&window.innerHeight>window.innerWidth);
  const lgnd={color:'#8e9bc0',font:{size:isMob?12:11,family:"'Barlow',sans-serif"},boxWidth:12,padding:isMob?8:10};
  return {
  responsive:true,maintainAspectRatio:false,
  animation:{duration:400},
  plugins:{
    legend:{display:!isMob,labels:lgnd},
    tooltip:{mode:'index',intersect:false,backgroundColor:'rgba(17,19,24,.95)',titleColor:'#e8eaf2',bodyColor:'#8e9bc0',borderColor:'#232736',borderWidth:1},
    crosshair:{} // registered above
  },
  scales:{
    x:{type:'time',time:{tooltipFormat:'HH:mm',displayFormats:{minute:'HH:mm',hour:'HH:mm'}},ticks:{color:'#4a5070',font:{size:10},maxTicksLimit:8},grid:{color:'rgba(255,255,255,.04)'},border:{color:'rgba(255,255,255,.06)'}},
    y:{ticks:{color:'#4a5070',font:{size:10},callback:v=>v.toLocaleString()},grid:{color:'rgba(255,255,255,.04)'},border:{color:'rgba(255,255,255,.06)'},afterFit(scale){const m=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);scale.width=m?46:58;}}
  }
};
  // Apply shared x-axis bounds if available — aligns all timeseries charts
  if(window._chartBounds){
    o.scales.x.min=window._chartBounds.min;
    o.scales.x.max=window._chartBounds.max;
  }
  return o;
}

// Chart options for PV/Load charts that have a per-inverter + combined dataset.
// The combined dataset is hidden from the tooltip — afterBody shows accurate sum
// computed from the aligned per-inverter values instead.
// Dummy right axis — forces single-axis charts to same plot width as dual-axis charts
const DUMMY_RIGHT_AXIS={position:'right',display:true,grid:{display:false},border:{display:false},
  ticks:{display:false,color:'transparent'},afterFit(scale){const m=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);scale.width=m?36:46;}};

function mkOptsCombined(){
  const base=mkOpts();
  base.scales.y2=DUMMY_RIGHT_AXIS; // align with battery/grid dual-axis charts
  const isMobC=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);
  if(isMobC) base.plugins.legend={display:false};
  base.plugins.tooltip.filter=function(item){
    // Exclude combined dataset from tooltip items
    return !item.dataset.label.includes('Combined');
  };
  base.plugins.tooltip.callbacks={
    afterBody:function(items){
      if(items.length<2) return [];
      const total=items.reduce((s,t)=>s+(t.parsed.y||0),0);
      return ['━━━━━━━━━━━━━━━','Combined: '+Math.round(total).toLocaleString()+' W'];
    }
  };
  return base;
}


function makeChart(id,cfg){
  if(charts[id]){charts[id].destroy();delete charts[id];}
  const ctx=document.getElementById(id);
  if(!ctx)return null;
  const c=new Chart(ctx,cfg);
  charts[id]=c;
  return c;
}

function timeLabels(rows,col='time'){return rows.map(r=>new Date(r[col]))}

// ── LOAD CHARTS ───────────────────────────────────────────────────────────────
// ── CROSSHAIR — synced vertical line across all timeseries charts ────────────
const crosshairPlugin={
  id:'crosshair',
  afterDraw(chart){
    if(chart._crosshairX==null) return;
    const {ctx,chartArea:{top,bottom}}=chart;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(chart._crosshairX, top);
    ctx.lineTo(chart._crosshairX, bottom);
    ctx.strokeStyle='rgba(255,255,255,0.3)';
    ctx.lineWidth=1;
    ctx.setLineDash([4,4]);
    ctx.stroke();
    ctx.restore();
  }
};
Chart.register(crosshairPlugin);

// Sync crosshair across all timeseries charts
function syncCrosshair(e, sourceChart){
  const x = e.native ? e.native.offsetX : e.offsetX;
  Object.values(charts).forEach(c=>{
    if(c===sourceChart||!c.scales?.x) return;
    // Map x coordinate proportionally
    const src=sourceChart.chartArea, dst=c.chartArea;
    const ratio=(x-src.left)/(src.right-src.left);
    const dstX=dst.left+ratio*(dst.right-dst.left);
    if(dstX>=dst.left&&dstX<=dst.right){
      c._crosshairX=dstX;
    } else {
      c._crosshairX=null;
    }
    c.draw();
  });
}

function clearCrosshair(){
  Object.values(charts).forEach(c=>{c._crosshairX=null;c.draw&&c.draw();});
}

// Wire crosshair to chart containers via event delegation
document.addEventListener('mousemove', e=>{
  const canvas=e.target.closest('canvas');
  if(!canvas) return;
  const chartId=canvas.id;
  const chart=charts[chartId];
  if(!chart||!chart.scales?.x) return;
  const rect=canvas.getBoundingClientRect();
  const x=e.clientX-rect.left;
  chart._crosshairX=x;
  chart.draw();
  syncCrosshair({native:{offsetX:x}}, chart);
});
document.addEventListener('mouseleave', clearCrosshair, true);

async function loadCharts(){
  if(!currentSite)return;
  const site=encodeURIComponent(currentSite);
  const get=url=>fetch(url).then(r=>r.json()).catch(e=>{console.error(url,e);return null;});
  const [pvR,loadR,battR,gridR,dailyR,tempR,peakR]=await Promise.all([
    get(`/api/chart/pv?site=${site}`),
    get(`/api/chart/load?site=${site}`),
    get(`/api/chart/battery?site=${site}`),
    get(`/api/chart/grid?site=${site}`),
    get(`/api/chart/daily?site=${site}`),
    get(`/api/chart/temps?site=${site}`),
    get(`/api/chart/peaks?site=${site}`),
  ]);

  // Compute shared x-axis bounds from all time-series datasets so all
  // timeseries charts line up exactly for easy visual comparison
  window._chartBounds = computeChartBounds([
    pvR?.per_inv, battR?.power, battR?.soc, gridR?.power, gridR?.voltage,
    loadR?.per_inv, tempR
  ]);

  if(pvR)   buildPVChart(pvR);
  if(loadR) buildLoadChart(loadR);
  if(battR) buildBattChart(battR);
  if(gridR) buildGridChart(gridR);
  if(dailyR)buildDailyChart(dailyR);
  if(tempR) buildTempChart(tempR);
  if(peakR) buildPeaks(peakR);
}

function computeChartBounds(datasets){
  let min=Infinity, max=-Infinity;
  (datasets||[]).forEach(ds=>{
    (ds||[]).forEach(r=>{
      const t=new Date(r.time||r.minute).getTime();
      if(t<min) min=t;
      if(t>max) max=t;
    });
  });
  if(min===Infinity) return null;
  return {min:new Date(min), max:new Date(max)};
}

function buildPVChart(data){
  const inv=groupBy(data.per_inv,'inverter_name');
  const colors=['#2ecc71','#f5a623','#4fc3f7','#9b59b6'];
  const datasets=Object.entries(inv).map(([name,rows],i)=>({
    label:`PV (W) ${name}`,
    data:rows.map(r=>({x:new Date(r.time),y:parseFloat(r.pv_w)||0})),
    borderColor:colors[i],backgroundColor:'transparent',
    fill:false,tension:.2,borderWidth:1.5,pointRadius:0,
  }));
  // Server-side combined — correct SQL, correct visual line
  // Hidden from tooltip (tooltip shows per-inverter values + computed sum)
  if(data.combined?.length){
    datasets.push({
      label:'Combined Total (W)',
      data:data.combined.map(r=>({x:new Date(r.time),y:parseFloat(r.combined_w)||0})),
      borderColor:'rgba(255,255,255,0.65)',backgroundColor:'transparent',
      fill:false,tension:.2,borderWidth:2,pointRadius:0,
      tooltip:{enabled:false}, // hide from tooltip — use afterBody computed sum
    });
  }
  makeChart('ch-pv',{type:'line',data:{datasets},options:mkOptsCombined()});
}

function buildLoadChart(data){
  const inv=groupBy(data.per_inv,'inverter_name');
  const colors=['#2ecc71','#f5a623','#4fc3f7'];
  const datasets=Object.entries(inv).map(([name,rows],i)=>({
    label:`Load (W) ${name}`,
    data:rows.map(r=>({x:new Date(r.time),y:parseFloat(r.load_w)||0})),
    borderColor:colors[i],backgroundColor:'transparent',
    fill:false,tension:.2,borderWidth:1.5,pointRadius:0,
  }));
  // Server-side combined — correct SQL, correct visual line
  // Hidden from tooltip — afterBody callback shows accurate computed sum
  if(data.combined?.length){
    datasets.push({
      label:'Combined Load (W)',
      data:data.combined.map(r=>({x:new Date(r.time),y:parseFloat(r.combined_w)||0})),
      borderColor:'rgba(255,255,255,0.65)',backgroundColor:'transparent',
      fill:false,tension:.2,borderWidth:2,pointRadius:0,
      tooltip:{enabled:false}, // hide from tooltip — use afterBody computed sum
    });
  }
  makeChart('ch-load',{type:'line',data:{datasets},options:mkOptsCombined()});
}

function buildBattChart(data){
  // Build options first — single object, no redundant first opts
  const battOpts=mkOpts();
  battOpts.scales.y1={position:'right',min:0,max:100,
    ticks:{color:'#2ecc71',font:{size:10},maxTicksLimit:5,callback:v=>v+'%'},
    grid:{display:false},border:{color:'rgba(255,255,255,.06)'},
    afterFit(scale){const m=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);scale.width=m?36:46;}
  };
  // Zero reference line — charging above, discharging below
  battOpts.plugins.annotation={annotations:{
    zeroLine:{type:'line',yMin:0,yMax:0,yScaleID:'y',
      borderColor:'rgba(255,255,255,.3)',borderWidth:1,borderDash:[3,3]},
    zeroLbl:{type:'label',xValue:null,yValue:0,yScaleID:'y',
      content:['0W'],color:'rgba(255,255,255,.3)',font:{size:9}}
  }};
  // Battery power: SQL returns discharging as positive (×-1 in SQL)
  // We flip sign again so: positive = charging (↑), negative = discharging (↓)
  const battData=(data.power||[]).map(r=>({x:new Date(r.time),y:-(parseFloat(r.batt_w)||0)}));
  const datasets=[
    {label:'Battery Power (W)',data:battData,
     borderColor:'#4fc3f7',backgroundColor:'rgba(79,195,247,.10)',
     fill:true,tension:.2,borderWidth:1.5,pointRadius:0,yAxisID:'y'},
    {label:'SOC (%)',data:(data.soc||[]).map(r=>({x:new Date(r.time),y:parseFloat(r.soc)||0})),
     borderColor:'#2ecc71',backgroundColor:'transparent',
     fill:false,tension:.2,borderWidth:1.5,pointRadius:0,yAxisID:'y1'},
  ];
  // Custom tooltip: show Charging/Discharging label with positive watts
  battOpts.plugins.tooltip.callbacks={
    label:function(item){
      if(item.dataset.label.includes('SOC')) return ' SOC: '+item.parsed.y.toFixed(1)+'%';
      const w=item.parsed.y;
      const abs=Math.abs(w);
      const label=w>15?'Charging':w<-15?'Discharging':'Idle';
      const disp=abs>=1000?(abs/1000).toFixed(1)+' kW':Math.round(abs)+' W';
      return ' Battery: '+disp+' ('+label+')';
    }
  };
  // Symmetric y-axis so charging and discharging get equal visual space
  const powerVals=battData.map(p=>p.y).filter(v=>v!==0);
  if(powerVals.length){
    const maxAbs=Math.ceil(Math.max(...powerVals.map(Math.abs))/500)*500;
    battOpts.scales.y.min=-maxAbs;
    battOpts.scales.y.max=maxAbs;
  }
  makeChart('ch-batt',{type:'line',data:{datasets},options:battOpts});
}

function buildGridChart(data){
  const opts=mkOpts();
  // Compute tight voltage range from actual data
  const voltages=(data.voltage||[]).map(r=>parseFloat(r.grid_v)||0).filter(v=>v>100);
  const vMin=voltages.length?Math.floor(Math.min(...voltages)/5)*5-10:215;
  const vMax=voltages.length?Math.ceil(Math.max(...voltages)/5)*5+5:260;
  opts.scales.y1={position:'right',min:vMin,max:vMax,
    ticks:{color:'#4fc3f7',font:{size:10},maxTicksLimit:6,callback:v=>v+'V'},
    grid:{display:false},border:{color:'rgba(255,255,255,.06)'},
    afterFit(scale){const m=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);scale.width=m?36:46;}
  };
  const datasets=[
    {label:'Grid Power (W)',data:data.power?.map(r=>({x:new Date(r.time),y:parseFloat(r.grid_w)||0}))||[],
     borderColor:'#FADE2A',
     segment:{
       borderColor:ctx=>ctx.p1.parsed.y<0?'#2ecc71':'#FADE2A',
       backgroundColor:ctx=>ctx.p1.parsed.y<0?'rgba(46,204,113,.10)':'rgba(250,222,42,.10)'
     },
     fill:false,tension:.2,borderWidth:1.5,pointRadius:0,yAxisID:'y'},
    {label:'Grid Voltage (V)',data:data.voltage?.map(r=>({x:new Date(r.time),y:parseFloat(r.grid_v)||0}))||[],
     borderColor:'#4fc3f7',backgroundColor:'transparent',fill:false,tension:.2,borderWidth:1.5,pointRadius:0,yAxisID:'y1'},
  ];
  makeChart('ch-grid',{type:'line',data:{datasets},options:opts});
}

function buildDailyChart(data){
  // On mobile show 7 days, desktop 14 days — prevents bars becoming too thin
  const isMobD=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);
  const displayData=isMobD?data.slice(-7):data;
  const labels=displayData.map(r=>r.day);
  const today=new Date().toLocaleDateString('en-CA',{timeZone:'Africa/Johannesburg'});
  const colors={pv:'#FADE2A',load:'#e74c3c',grid:'#4fc3f7',chg:'#f5a623',dis:'#2ecc71'};
  // Today's bars at 60% opacity — day not complete yet
  const mkColor=(hex,idx)=>hex+(labels[idx]===today?'99':'ff');
  const datasets=[
    {label:'PV Generated',   data:displayData.map(r=>parseFloat(r.pv)||0),  backgroundColor:labels.map((_,i)=>mkColor(colors.pv,i)),   barPercentage:.85},
    {label:'Load Consumed',  data:displayData.map(r=>parseFloat(r.load)||0), backgroundColor:labels.map((_,i)=>mkColor(colors.load,i)), barPercentage:.7},
    {label:'Grid Import',    data:displayData.map(r=>parseFloat(r.grid)||0), backgroundColor:labels.map((_,i)=>mkColor(colors.grid,i)), barPercentage:.7},
    {label:'Batt Charge',    data:displayData.map(r=>parseFloat(r.chg)||0),  backgroundColor:labels.map((_,i)=>mkColor(colors.chg,i)),  barPercentage:.7},
    {label:'Batt Discharge', data:displayData.map(r=>parseFloat(r.dis)||0),  backgroundColor:labels.map((_,i)=>mkColor(colors.dis,i)),  barPercentage:.7},
  ];
  const opts=mkOpts();
  // On mobile show short dates: 'Apr 11' instead of '2026-04-11'
  const isMobChart=window.innerWidth<=768||(window.innerWidth<=1024&&window.innerHeight>window.innerWidth);
  const shortLabels=isMobChart?labels.map(d=>{
    const dt=new Date(d+'T12:00:00');
    return dt.toLocaleDateString('en-ZA',{month:'short',day:'numeric',timeZone:'Africa/Johannesburg'});
  }):labels;
  opts.scales.x={type:'category',
    labels:shortLabels,
    ticks:{color:'#4a5070',font:{size:isMobChart?9:10},maxRotation:isMobChart?45:0},
    grid:{color:'rgba(255,255,255,.04)'},border:{color:'rgba(255,255,255,.06)'}};
  opts.scales.y.ticks.callback=v=>v+' kWh';
  // "In progress" label on today's bar
  const todayIdx=labels.indexOf(today);
  if(todayIdx>=0){
    if(!opts.plugins) opts.plugins={};
    opts.plugins.annotation={annotations:{todayLbl:{
      type:'label',xValue:todayIdx,
      content:['in progress'],color:'rgba(255,255,255,.4)',
      font:{size:9,style:'italic'},yAdjust:-14
    }}};
  }
  makeChart('ch-daily',{type:'bar',data:{labels,datasets},options:opts});
}

function buildTempChart(data){
  const inv=groupBy(data,'inverter_name');
  const colors=['#e74c3c','#f5a623','#4fc3f7','#9b59b6'];
  const datasets=[];
  let ci=0;
  Object.entries(inv).forEach(([name,rows])=>{
    const c=colors[ci++%colors.length];
    datasets.push({label:`Inv Temp ${name}`,data:rows.filter(r=>r.inv_temp).map(r=>({x:new Date(r.time),y:parseFloat(r.inv_temp)||0})),borderColor:c,backgroundColor:'transparent',fill:false,tension:.2,borderWidth:1.5,pointRadius:0});
    datasets.push({label:`DC Temp ${name}`,data:rows.filter(r=>r.dc_temp).map(r=>({x:new Date(r.time),y:parseFloat(r.dc_temp)||0})),borderColor:c,backgroundColor:'transparent',fill:false,tension:.2,borderWidth:1,borderDash:[4,4],pointRadius:0});
  });
  const opts=mkOpts();
  opts.scales.y.ticks.callback=v=>v+'°C';
  opts.scales.y2=DUMMY_RIGHT_AXIS; // align with battery/grid charts
  makeChart('ch-temp',{type:'line',data:{datasets},options:opts});
}

function buildPeaks(data){
  // PostgreSQL NUMERIC comes back as strings — parseFloat before any arithmetic
  const fmtW=w=>{const n=parseFloat(w)||0;return n>=1000?(n/1000).toFixed(1)+' kW':Math.round(n)+' W';};
  const pv=fmtW(data.peak_pv), load=fmtW(data.peak_load), grid=fmtW(data.peak_grid);
  // Top stat pills
  document.getElementById('a-peak-pv').textContent=pv;
  document.getElementById('a-peak-load').textContent=load;
  if(data.peak_grid!=null) document.getElementById('a-peak-grid').textContent=grid;
  // Footer row 2 peaks
  document.getElementById('fa-peak-pv').textContent=pv;
  document.getElementById('fa-peak-load').textContent=load;
  if(data.peak_grid!=null) document.getElementById('fa-peak-grid').textContent=grid;
}

function groupBy(arr,key){
  const m={};
  (arr||[]).forEach(r=>{const k=r[key];if(!m[k])m[k]=[];m[k].push(r);});
  return m;
}

// ── MONTHLY STATS ────────────────────────────────────────────────────────────
let monthlyTimer = null;

async function refreshMonthly(){
  if(!currentSite) return;
  try{
    const r = await fetch(`/api/monthly?site=${encodeURIComponent(currentSite)}`);
    const d = await r.json();
    if(!d.error) renderMonthly(d);
  } catch(e){ console.error('monthly fetch:', e); }
  // Refresh every 15 min — monthly figures change slowly
  monthlyTimer = setTimeout(refreshMonthly, 15 * 60 * 1000);
}

function renderMonthly(d){
  window._monthly = d; // store for rate recalculation
  const pv   = parseFloat(d.month_pv_kwh)   || 0;
  const grid = parseFloat(d.month_grid_kwh)  || 0;
  const pvVal   = Math.max(0, calcCost(pv));
  const gridCost = Math.max(0, calcCost(grid));

  document.getElementById('f-mpv').textContent   = pv.toFixed(1)   + ' kWh';
  document.getElementById('f-mgrid').textContent  = grid.toFixed(1)  + ' kWh';
  document.getElementById('f-mpvr').textContent   = 'R' + pvVal.toFixed(2);
  document.getElementById('f-mgridr').textContent = 'R' + gridCost.toFixed(2);
  // Also update advanced footer pills
  document.getElementById('fa-mpv').textContent   = pv.toFixed(1)   + ' kWh';
  document.getElementById('fa-mgrid').textContent  = grid.toFixed(1)  + ' kWh';
  document.getElementById('fa-mpvr').textContent   = 'R' + pvVal.toFixed(2);
  document.getElementById('fa-mgridr').textContent = 'R' + gridCost.toFixed(2);
}

// ── TEMP CHART TOGGLE ────────────────────────────────────────────────────────
let tempsVisible = true;
function toggleTemps(){
  tempsVisible = !tempsVisible;
  const wrap = document.getElementById('temp-chart-wrap');
  const icon = document.getElementById('temp-toggle-icon');
  wrap.style.display = tempsVisible ? '' : 'none';
  icon.textContent = tempsVisible ? '▼ HIDE' : '▶ SHOW';
  // Resize chart when shown
  if(tempsVisible && charts['ch-temp']) charts['ch-temp'].resize();
}

// ── WEATHER ───────────────────────────────────────────────────────────────────
let wxTimer = null;

function fmtTime(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-ZA',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Africa/Johannesburg'});
}

async function refreshWeather(){
  if(!currentSite) return;
  try{
    const r = await fetch(`/api/weather?site=${encodeURIComponent(currentSite)}`);
    const d = await r.json();
    if(d.error || !d.temp_c) return; // no data yet — keep loading state
    renderWeather(d);
  } catch(e){ console.error('weather fetch:', e); }
  wxTimer = setTimeout(refreshWeather, 10 * 60 * 1000); // refresh every 10 min
}

function renderWeather(d){
  window._wx = d; // store for use in render() sunrise countdown
  const strip = document.getElementById('wx-strip');
  strip.classList.remove('wx-empty');

  // PostgreSQL NUMERIC columns come back as strings in JSON — coerce everything
  const f = v => v != null ? parseFloat(v) : null;
  const temp  = f(d.temp_c);
  const feels = f(d.feels_like_c);
  const rain  = f(d.precipitation);
  const wind  = f(d.wind_speed);
  const uv    = f(d.uv_index);
  const rad   = f(d.solar_rad);
  const cloud = f(d.cloud_cover) ?? 0;
  const hum   = d.humidity != null ? parseInt(d.humidity) : null;

  document.getElementById('wx-icon').textContent   = d.emoji       || '🌡️';
  document.getElementById('wx-desc').textContent   = d.description || '—';
  document.getElementById('wx-site').textContent   = currentSite;
  document.getElementById('wx-temp').textContent   = temp  != null ? temp.toFixed(1)  + '°C'    : '—';
  document.getElementById('wx-feels').textContent  = feels != null ? feels.toFixed(1) + '°C'    : '—';
  document.getElementById('wx-rain').textContent   = rain  != null ? (rain===0?'0':rain.toFixed(1)) + ' mm/hr' : '—';
  document.getElementById('wx-wind').textContent   = wind  != null ? wind.toFixed(0)  + ' km/h' : '—';
  document.getElementById('wx-hum').textContent    = hum   != null ? hum + '%'                  : '—';
  document.getElementById('wx-uv').textContent     = uv    != null ? uv.toFixed(1)              : '—';
  // At night hide solar rad (always 0) and show sunrise countdown instead
  const radPill = document.getElementById('wx-rad-pill');
  if(radPill){
    if(!d.is_day && d.sunrise){
      const srMs = new Date(d.sunrise).getTime();
      const nowMs = Date.now();
      const diffMs = srMs - nowMs;
      if(diffMs > 0){
        const hrs = Math.floor(diffMs/3600000);
        const mins = Math.floor((diffMs%3600000)/60000);
        document.getElementById('wx-rad-lbl').textContent = 'SUNRISE IN';
        document.getElementById('wx-rad').textContent = hrs+'h '+mins+'m';
      } else {
        document.getElementById('wx-rad-lbl').textContent = 'SOLAR RAD';
        document.getElementById('wx-rad').textContent = rad != null ? Math.round(rad) + ' W/m²' : '—';
      }
    } else {
      document.getElementById('wx-rad-lbl').textContent = 'SOLAR RAD';
      document.getElementById('wx-rad').textContent = rad != null ? Math.round(rad) + ' W/m²' : '—';
    }
  } else {
    document.getElementById('wx-rad').textContent = rad != null ? Math.round(rad) + ' W/m²' : '—';
  }

  document.getElementById('wx-cloud-pct').textContent      = cloud + '%';
  document.getElementById('wx-cloud-bar').style.width      = cloud + '%';
  const cloudColor = cloud < 30 ? 'var(--solar)' : cloud < 70 ? 'var(--amber)' : 'var(--muted)';
  document.getElementById('wx-cloud-bar').style.background = cloudColor;

  document.getElementById('wx-sun').textContent =
    '🌅 ' + fmtTime(d.sunrise) + '  ·  🌇 ' + fmtTime(d.sunset);
}

// ── LIVE DATA LOOP ────────────────────────────────────────────────────────────
async function refresh(){
  if(!currentSite)return;
  try{
    const r=await fetch(`/api/flow?site=${encodeURIComponent(currentSite)}`);
    const d=await r.json();
    if(!d.error)render(d);
  }catch(e){console.error(e);}
  liveTimer=setTimeout(refresh,10000);
}

// Update URL ?site= param without page reload — enables bookmarking
function updateURL(siteName){
  const url=new URL(window.location.href);
  url.searchParams.set('site',siteName);
  window.history.replaceState({},'',url.toString());
}

async function loadSites(){
  const r=await fetch('/api/sites');
  const sites=await r.json();
  const sel=document.getElementById('site-sel');
  sel.innerHTML=sites.map(s=>`<option value="${s.name}">${s.display}</option>`).join('');
  if(!sites.length) return;

  // Priority: 1) ?site= URL param  2) localStorage  3) first site
  const urlParam=new URLSearchParams(window.location.search).get('site');
  const stored=localStorage.getItem('sw_site');
  const preferred=urlParam||stored||null;
  const match=preferred?sites.find(s=>s.name.toLowerCase()===preferred.toLowerCase()):null;
  currentSite=(match?match.name:sites[0].name);
  sel.value=currentSite;
  localStorage.setItem('sw_site',currentSite);
  if(urlParam) updateURL(currentSite);
  refresh();refreshWeather();refreshMonthly();
}

function onSiteChange(){
  currentSite=document.getElementById('site-sel').value;
  localStorage.setItem('sw_site',currentSite);
  updateURL(currentSite);
  if(liveTimer)clearTimeout(liveTimer);
  if(wxTimer)clearTimeout(wxTimer);
  // Reset weather strip to loading state for new site
  document.getElementById('wx-strip').classList.add('wx-empty');
  document.getElementById('wx-desc').textContent='Loading weather…';
  // Destroy charts so they rebuild fresh for new site
  Object.values(charts).forEach(c=>c.destroy());charts={};
  refresh();
  refreshWeather();
  refreshMonthly();
  if(currentView==='adv')loadCharts();
}

// Refresh charts every 60s when in advanced view
setInterval(()=>{if(currentView==='adv')loadCharts();},60000);

new ResizeObserver(()=>{if(window._ld)drawLines(window._ld);}).observe(document.getElementById('fa'));

// ── CLOCK ─────────────────────────────────────────────────────────────────────
function toggleClock(){
  clockVisible=!clockVisible;
  localStorage.setItem('sw_clock',clockVisible?'1':'0');
  applyClockState();
}

function applyClockState(){
  const el=document.getElementById('clock-wrap');
  const clk=document.getElementById('clock');
  if(clockVisible){
    el.style.opacity='1';
    clk.style.color='var(--text)';
    if(!clockTimer) clockTimer=setInterval(updateClock,1000);
    updateClock();
  } else {
    el.style.opacity='0.4';
    clk.style.color='var(--muted)';
    clk.textContent='--:--:--';
    if(clockTimer){clearInterval(clockTimer);clockTimer=null;}
  }
}

function updateClock(){
  const now=new Date();
  document.getElementById('clock').textContent=
    now.toLocaleTimeString('en-ZA',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Africa/Johannesburg'});
}

// Restore clock preference
clockVisible = localStorage.getItem('sw_clock')=='1';
applyClockState();

loadSites();
</script>
</body>
</html>"""


def get_chart(chart: str, site: str) -> dict:
    """Return chart data for the advanced view."""
    S = site

    if chart == 'pv':
        per_inv = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name,
              AVG(pv1_power + COALESCE(pv2_power,0)) as pv_w
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE %s
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        combined = query_all("""
            SELECT minute as time, SUM(avg_pv) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(pv1_power + COALESCE(pv2_power,0)) as avg_pv
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        return {'per_inv': per_inv, 'combined': combined}

    elif chart == 'load':
        per_inv = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name, AVG(load_power) as load_w
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE %s
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        combined = query_all("""
            SELECT minute as time, SUM(avg_load) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(load_power) as avg_load
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        return {'per_inv': per_inv, 'combined': combined}

    elif chart == 'battery':
        power = query_all("""
            SELECT minute as time, SUM(avg_batt) as batt_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(battery_power) as avg_batt
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        soc = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(battery_soc) as soc
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE %s AND battery_soc IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (S,))
        return {'power': power, 'soc': soc}

    elif chart == 'grid':
        power = query_all("""
            SELECT minute as time, SUM(avg_grid) as grid_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(grid_power) as avg_grid
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        voltage = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(grid_voltage) as grid_v
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE %s AND grid_voltage IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, (S,))
        return {'power': power, 'voltage': voltage}

    elif chart == 'daily':
        rows = query_all("""
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
              AND site_name ILIKE %s
              AND daily_pv_energy IS NOT NULL
              ORDER BY DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name, time DESC
            ) sub GROUP BY day ORDER BY day
        """, (S,))
        return rows

    elif chart == 'temps':
        rows = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name,
              AVG(CASE WHEN inverter_temp < 100 THEN inverter_temp END) as inv_temp,
              AVG(CASE WHEN dc_temp < 100 THEN dc_temp END) as dc_temp,
              AVG(CASE WHEN battery_temp > 0 THEN battery_temp END) as batt_temp
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE %s
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        return rows

    elif chart == 'peaks':
        row = query_one("""
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
                AND site_name ILIKE %s
                GROUP BY 1, 2
              ) inv GROUP BY ts
            ) totals
        """, (S,))
        return row

    return {'error': f'Unknown chart: {chart}'}


def get_weather(site: str) -> dict:
    """
    Return the most recent weather reading for a site, plus emoji/description
    derived from the WMO weather code.
    """
    # WMO code → (emoji, description) — kept in sync with weather_worker.py
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

    row = query_one("""
        SELECT
            temp_c, feels_like_c, cloud_cover, precipitation,
            wind_speed, wind_direction, humidity,
            weather_code, uv_index, sunrise, sunset,
            solar_rad, is_day, time AS last_updated
        FROM weather_readings
        WHERE site_name ILIKE %s
        ORDER BY time DESC
        LIMIT 1
    """, (site,))

    if not row:
        return {'error': 'No weather data yet', 'site': site}

    code = row.get('weather_code')
    emoji, desc = WMO.get(code, ("🌡️", f"Code {code}"))
    row['emoji']       = emoji
    row['description'] = desc
    return row


from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(f"{self.address_string()} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        try:
            if parsed.path == '/':
                body = HTML.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == '/api/sites':
                self.send_json(get_sites())

            elif parsed.path == '/api/flow':
                site = qs.get('site', [None])[0]
                if not site:
                    sites = get_sites()
                    site = sites[0]['name'] if sites else None
                if not site:
                    self.send_json({'error': 'No sites'}, 400)
                    return
                self.send_json(get_flow(site))

            elif parsed.path == '/api/monthly':
                site = qs.get('site', [None])[0]
                if not site:
                    sites = get_sites()
                    site = sites[0]['name'] if sites else None
                if not site:
                    self.send_json({'error': 'No sites'}, 400)
                    return
                self.send_json(get_monthly(site))

            elif parsed.path == '/api/weather':
                site = qs.get('site', [None])[0]
                if not site:
                    sites = get_sites()
                    site = sites[0]['name'] if sites else None
                if not site:
                    self.send_json({'error': 'No sites'}, 400)
                    return
                self.send_json(get_weather(site))

            elif parsed.path.startswith('/api/chart/'):
                site = qs.get('site', [None])[0] or ''
                chart = parsed.path.split('/')[-1]
                self.send_json(get_chart(chart, site))

            else:
                self.send_response(404)
                self.end_headers()

        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            self.send_json({'error': str(e)}, 500)


if __name__ == '__main__':
    try:
        get_pool()
    except Exception as e:
        log.error(f"DB connection failed: {e}")
        raise SystemExit(1)

    server = HTTPServer(('0.0.0.0', PORT), Handler)
    log.info(f"SolarWatch Power Flow  →  http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()