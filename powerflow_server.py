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

/* ── APP SHELL ── */
.app{display:grid;grid-template-rows:5.5vh auto 1fr 11vh;height:100vh;width:100vw}

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
.fwd{animation:df 1s linear infinite}.rev{animation:dr 1s linear infinite}
.idle{opacity:.1;animation:none}.slow{animation-duration:2s}.med{animation-duration:1.1s}.fast{animation-duration:.5s}

/* ── ADVANCED VIEW ── */
#view-adv{flex-direction:column;overflow:hidden;height:100%}
.adv-inner{
  flex:1;overflow-y:auto;overflow-x:hidden;
  display:grid;
  grid-template-columns:1fr 1fr;
  grid-template-rows:repeat(3,min(28vh,280px)) min(28vh,280px) min(28vh,280px);
  gap:1px;
  background:var(--border);
  min-height:0;
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

/* Stat panels in advanced view */
.adv-stats{
  grid-column:1/-1;display:flex;gap:1px;background:var(--border);
  height:min(14vh,110px);flex-shrink:0;
}
.adv-stat{
  flex:1;background:var(--surface);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:4px;
}
.adv-stat-lbl{font-size:clamp(9px,.8vw,12px);color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.adv-stat-val{font-size:clamp(18px,2.5vw,40px);font-family:var(--mono);font-weight:700}

/* ── FOOTER ── */
footer{
  display:flex;align-items:center;justify-content:center;
  gap:clamp(8px,2.2vw,48px);padding:0 3vw;
  border-top:1px solid var(--border);background:var(--surface);flex-wrap:wrap;flex-shrink:0;
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

  /* ── BASIC VIEW — switch to column layout ── */
  #view-basic{
    flex-direction:column;
    height:auto;
    min-height:calc(100svh - 120px);
  }

  /* SOC panel: horizontal strip across the top */
  .soc-panel{
    width:100%;
    flex-direction:row;
    border-right:none;
    border-bottom:1px solid var(--border);
    padding:10px 16px;
    gap:14px;
    justify-content:flex-start;
    align-items:center;
    flex-shrink:0;
    min-height:0;
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

  /* Flow area: constrained on mobile to prevent excessive vertical stretching */
  .flow-area{
    flex:1;
    min-height:280px;
    max-height:min(60vw,380px);
    width:100%;
  }

  /* Node cards: smaller on mobile */
  .nc{
    width:clamp(90px,22vw,130px);
    padding:8px 10px;
    border-radius:10px;
    gap:3px;
  }
  .n-ico{font-size:clamp(18px,5vw,30px)}
  .n-val{font-size:clamp(14px,4.5vw,24px)}
  .n-lbl{font-size:9px}
  .n-sub{font-size:9px}

  /* Hub ring: smaller */
  .hub-ring{
    width:clamp(44px,11vw,64px);
    height:clamp(44px,11vw,64px);
    font-size:clamp(18px,5vw,30px);
  }
  .hub-lbl{font-size:8px}

  /* ── FOOTER ── */
  footer{
    height:auto;
    padding:8px 12px;
    gap:0;
    display:grid;
    grid-template-columns:repeat(4,1fr);
    justify-items:center;
    align-items:center;
  }
  /* Show only first 4 most important stats */
  footer .stat:nth-child(1){order:1}  /* Battery V */
  footer .stat:nth-child(2){order:2}  /* Batt Temp */
  footer .stat:nth-child(3){order:3}  /* Grid V */
  footer .stat:nth-child(4){order:4}  /* Frequency */
  footer .stat:nth-child(5){order:5}  /* Self-Suff */
  footer .stat:nth-child(6){order:6}  /* PV Today */
  footer .stat:nth-child(7){order:7}  /* Load Today */
  footer .stat:nth-child(8){order:8}  /* Solar Savings */
  /* Hide Grid Today and Clock in the tight footer */
  footer .stat:nth-child(9){display:none}
  footer #clock-wrap{display:none}

  .st-lbl{font-size:9px}
  .st-val{font-size:clamp(12px,3.5vw,18px)}
  /* Clock is huge on desktop — reset to normal on mobile */
  #clock{font-size:clamp(12px,3.5vw,18px)!important;font-weight:500!important}

  /* ── WEATHER STRIP mobile ── */
  .wx-strip{
    height:auto;
    flex-wrap:wrap;
    padding:6px 12px;
    gap:8px;
  }
  .wx-pills{flex-wrap:wrap;gap:8px}
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
  /* Stat pills: 3 across instead of 6 */
  .adv-stats{
    height:auto;
    flex-wrap:wrap;
    padding:4px 0;
  }
  .adv-stat{
    flex:0 0 33.333%;
    min-width:0;
    padding:6px 4px;
    border-bottom:1px solid var(--border);
  }
  .adv-stat-lbl{font-size:8px}
  .adv-stat-val{font-size:clamp(14px,4vw,22px)}
}

/* Extra-small phones (≤380px) — tighten further */
@media (max-width:380px){
  .nc{width:80px;padding:6px 7px}
  .n-val{font-size:13px}
  footer{grid-template-columns:repeat(4,1fr)}
  .st-val{font-size:11px}
  .adv-stat{flex:0 0 50%}
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
    <div class="wx-pill">
      <span class="wx-pill-lbl">Solar Rad</span>
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
    <!-- Top stat pills -->
    <div class="adv-stats" id="adv-stats">
      <div class="adv-stat"><span class="adv-stat-lbl">Peak Solar Today</span><span class="adv-stat-val" id="a-peak-pv" style="color:var(--solar)">—</span></div>
      <div class="adv-stat"><span class="adv-stat-lbl">Peak Load Today</span><span class="adv-stat-val" id="a-peak-load" style="color:var(--amber)">—</span></div>
      <div class="adv-stat"><span class="adv-stat-lbl">Battery SOC</span><span class="adv-stat-val" id="a-soc" style="color:var(--green)">—</span></div>
      <div class="adv-stat"><span class="adv-stat-lbl">PV Today</span><span class="adv-stat-val" id="a-pv-today" style="color:var(--solar)">—</span></div>
      <div class="adv-stat"><span class="adv-stat-lbl">Self-Suff</span><span class="adv-stat-val" id="a-ss">—</span></div>
      <div class="adv-stat"><span class="adv-stat-lbl">Solar Savings</span><span class="adv-stat-val" id="a-savings" style="color:var(--green)">—</span></div>
    </div>
    <!-- Chart grid -->
    <div class="adv-inner" id="adv-inner">
      <div class="chart-panel span2"><div class="chart-title">Solar PV Power — Per Inverter &amp; Combined</div><div class="chart-wrap"><canvas id="ch-pv"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">Load Power — Per Inverter &amp; Combined</div><div class="chart-wrap"><canvas id="ch-load"></canvas></div></div>
      <div class="chart-panel"><div class="chart-title">Battery Power &amp; SOC</div><div class="chart-wrap"><canvas id="ch-batt"></canvas></div></div>
      <div class="chart-panel"><div class="chart-title">Grid Power &amp; Voltage</div><div class="chart-wrap"><canvas id="ch-grid"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">Daily Energy — Combined Site (Last 14 Days)</div><div class="chart-wrap"><canvas id="ch-daily"></canvas></div></div>
      <div class="chart-panel span2"><div class="chart-title">Inverter Temperatures</div><div class="chart-wrap"><canvas id="ch-temp"></canvas></div></div>
    </div>
  </div>

</main>

<!-- FOOTER -->
<footer>
  <div class="stat"><span class="st-lbl">Battery V</span><span class="st-val" id="f-bv">—</span></div>
  <div class="stat"><span class="st-lbl">Batt Temp</span><span class="st-val" id="f-bt">—</span></div>
  <div class="stat"><span class="st-lbl">Grid V</span><span class="st-val" id="f-gv">—</span></div>
  <div class="stat"><span class="st-lbl">Frequency</span><span class="st-val" id="f-hz">—</span></div>
  <div class="stat"><span class="st-lbl">Self-Suff</span><span class="st-val" id="f-ss">—</span></div>
  <div class="stat"><span class="st-lbl">PV Today</span><span class="st-val" id="f-pv" style="color:var(--solar)">—</span></div>
  <div class="stat"><span class="st-lbl">Load Today</span><span class="st-val" id="f-tl">—</span></div>
  <div class="stat"><span class="st-lbl">Solar Savings</span><span class="st-val" id="f-sv" style="color:var(--green)">—</span></div>
  <div class="stat"><span class="st-lbl">Grid Today</span><span class="st-val" id="f-tg">—</span></div>
  <!-- Clock — click to toggle visibility, persists in localStorage -->
  <div class="stat" id="clock-wrap" onclick="toggleClock()" title="Click to toggle clock" style="cursor:pointer;margin-left:auto;padding-left:clamp(8px,2vw,32px);opacity:0.4;transition:opacity .2s" onmouseenter="this.style.opacity=1" onmouseleave="this.style.opacity=clockVisible?1:0.4">
    <span class="st-lbl">Time</span>
    <span class="st-val" id="clock" style="font-family:var(--mono);color:var(--muted);font-size:clamp(20px,2.4vw,40px);font-weight:600">--:--:--</span>
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
}

function onRateChange(){
  flatRate=parseFloat(document.getElementById('flat-rate').value)||4.50;
  if(window._ld) updateFooterCosts(window._ld);
}

function updateFooterCosts(d){
  const load=d.daily_load_kwh||0, grid=d.daily_grid_kwh||0;
  const savings=Math.max(0, calcCost(load)-calcCost(grid));
  document.getElementById('f-sv').textContent='R'+savings.toFixed(2);
  document.getElementById('a-savings').textContent='R'+savings.toFixed(2);
}

// ── VIEW MANAGEMENT ───────────────────────────────────────────────────────────
function setView(v){
  currentView=v;
  document.getElementById('view-basic').className='view'+(v==='basic'?' active':'');
  document.getElementById('view-adv').className='view'+(v==='adv'?' active':'');
  document.getElementById('vbtn-basic').className='vbtn'+(v==='basic'?' active':'');
  document.getElementById('vbtn-adv').className='vbtn'+(v==='adv'?' active':'');
  document.getElementById('view-label').textContent=v==='basic'?'Power Flow':'Advanced';
  if(v==='adv') loadCharts();
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
function fmt(w){const a=Math.abs(w);return a>=1000?(a/1000).toFixed(1)+' kW':a+' W'}
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
  document.getElementById('smsg').textContent=st.msg;
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

  // Footer
  document.getElementById('f-bv').textContent=d.batt_v.toFixed(1)+'V';
  document.getElementById('f-bt').textContent=d.batt_temp.toFixed(1)+'°C';
  document.getElementById('f-gv').textContent=d.grid_v.toFixed(0)+'V';
  document.getElementById('f-hz').textContent=d.grid_hz.toFixed(2)+'Hz';
  const ss=d.self_suff??0;
  const sse=document.getElementById('f-ss');
  sse.textContent=ss+'%';sse.style.color=ss>80?'var(--green)':ss>50?'var(--amber)':'var(--red)';
  document.getElementById('f-pv').textContent=(d.daily_pv_kwh??0)+' kWh';
  document.getElementById('f-tl').textContent=(d.daily_load_kwh??0)+' kWh';
  document.getElementById('f-tg').textContent=(d.daily_grid_kwh??0)+' kWh';
  updateFooterCosts(d);

  // Advanced stat pills (update even if not visible)
  document.getElementById('a-soc').textContent=(soc%1===0?soc:soc.toFixed(1))+'%';
  document.getElementById('a-soc').style.color=sc(soc);
  document.getElementById('a-pv-today').textContent=(d.daily_pv_kwh??0)+' kWh';
  document.getElementById('a-ss').textContent=ss+'%';
  document.getElementById('a-ss').style.color=ss>80?'var(--green)':ss>50?'var(--amber)':'var(--red)';

  document.getElementById('dot').className='dot'+(d.stale?' stale':'');
  document.getElementById('age').textContent=d.age_s!==null?d.age_s+'s ago':'live';

  drawLines(d);
}

// ── CHART HELPERS ─────────────────────────────────────────────────────────────
function mkOpts(){return {
  responsive:true,maintainAspectRatio:false,
  animation:{duration:400},
  plugins:{legend:{labels:{color:'#8e9bc0',font:{size:11,family:"'Barlow',sans-serif"},boxWidth:12,padding:10}},tooltip:{mode:'index',intersect:false,backgroundColor:'rgba(17,19,24,.95)',titleColor:'#e8eaf2',bodyColor:'#8e9bc0',borderColor:'#232736',borderWidth:1}},
  scales:{
    x:{type:'time',time:{tooltipFormat:'HH:mm',displayFormats:{minute:'HH:mm',hour:'HH:mm'}},ticks:{color:'#4a5070',font:{size:10},maxTicksLimit:8},grid:{color:'rgba(255,255,255,.04)'},border:{color:'rgba(255,255,255,.06)'}},
    y:{ticks:{color:'#4a5070',font:{size:10},callback:v=>v.toLocaleString()},grid:{color:'rgba(255,255,255,.04)'},border:{color:'rgba(255,255,255,.06)'}}
  }
};}

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
  if(pvR)   buildPVChart(pvR);
  if(loadR) buildLoadChart(loadR);
  if(battR) buildBattChart(battR);
  if(gridR) buildGridChart(gridR);
  if(dailyR)buildDailyChart(dailyR);
  if(tempR) buildTempChart(tempR);
  if(peakR) buildPeaks(peakR);
}

function buildPVChart(data){
  // data: [{time, inverter_name, pv_w}, ...] + [{time, combined_w}]
  const inv=groupBy(data.per_inv,'inverter_name');
  const colors=['#2ecc71','#f5a623','#4fc3f7','#9b59b6'];
  const datasets=Object.entries(inv).map(([name,rows],i)=>({
    label:`PV (W) ${name}`,data:rows.map(r=>({x:new Date(r.time),y:r.pv_w})),
    borderColor:colors[i],backgroundColor:'transparent',fill:false,tension:.3,borderWidth:2,pointRadius:0,
  }));
  if(data.combined?.length){
    datasets.push({label:'Combined Total (W)',data:data.combined.map(r=>({x:new Date(r.time),y:r.combined_w})),
      borderColor:'rgba(255,255,255,0.5)',backgroundColor:'rgba(255,255,255,0.12)',fill:true,tension:.3,borderWidth:3,pointRadius:0,});
  }
  makeChart('ch-pv',{type:'line',data:{datasets},options:mkOpts()});
}

function buildLoadChart(data){
  const inv=groupBy(data.per_inv,'inverter_name');
  const colors=['#2ecc71','#f5a623','#4fc3f7'];
  const datasets=Object.entries(inv).map(([name,rows],i)=>({
    label:`Load (W) ${name}`,data:rows.map(r=>({x:new Date(r.time),y:r.load_w})),
    borderColor:colors[i],backgroundColor:'transparent',fill:false,tension:.3,borderWidth:2,pointRadius:0,
  }));
  if(data.combined?.length){
    datasets.push({label:'Combined Load (W)',data:data.combined.map(r=>({x:new Date(r.time),y:r.combined_w})),
      borderColor:'rgba(255,255,255,0.5)',backgroundColor:'rgba(255,255,255,0.12)',fill:true,tension:.3,borderWidth:4,pointRadius:0,});
  }
  makeChart('ch-load',{type:'line',data:{datasets},options:mkOpts()});
}

function buildBattChart(data){
  const opts=mkOpts();
  opts.scales.y1={position:'right',ticks:{color:'#2ecc71',font:{size:10},callback:v=>v+'%'},grid:{display:false},border:{color:'rgba(255,255,255,.06)'},min:0,max:100};
  const datasets=[
    {label:'Battery Power (W)',data:data.power?.map(r=>({x:new Date(r.time),y:r.batt_w}))||[],
     borderColor:'#4fc3f7',backgroundColor:'rgba(79,195,247,.15)',fill:true,tension:.3,borderWidth:1.5,pointRadius:0,yAxisID:'y'},
    {label:'SOC (%)',data:data.soc?.map(r=>({x:new Date(r.time),y:r.soc}))||[],
     borderColor:'#2ecc71',backgroundColor:'transparent',fill:false,tension:.3,borderWidth:2,pointRadius:0,yAxisID:'y1'},
  ];
  makeChart('ch-batt',{type:'line',data:{datasets},options:opts});
}

function buildGridChart(data){
  const opts=mkOpts();
  opts.scales.y1={position:'right',ticks:{color:'#4fc3f7',font:{size:10},callback:v=>v+'V'},grid:{display:false},border:{color:'rgba(255,255,255,.06)'}};
  const datasets=[
    {label:'Grid Power (W)',data:data.power?.map(r=>({x:new Date(r.time),y:r.grid_w}))||[],
     borderColor:'#FADE2A',backgroundColor:'rgba(250,222,42,.15)',fill:true,tension:.3,borderWidth:1.5,pointRadius:0,yAxisID:'y'},
    {label:'Grid Voltage (V)',data:data.voltage?.map(r=>({x:new Date(r.time),y:r.grid_v}))||[],
     borderColor:'#4fc3f7',backgroundColor:'transparent',fill:false,tension:.3,borderWidth:1.5,pointRadius:0,yAxisID:'y1'},
  ];
  makeChart('ch-grid',{type:'line',data:{datasets},options:opts});
}

function buildDailyChart(data){
  const labels=data.map(r=>r.day);
  const colors={pv:'#FADE2A',load:'#e74c3c',grid:'#4fc3f7',chg:'#f5a623',dis:'#2ecc71'};
  const datasets=[
    {label:'PV Generated',data:data.map(r=>r.pv),backgroundColor:colors.pv,barPercentage:.7},
    {label:'Load Consumed',data:data.map(r=>r.load),backgroundColor:colors.load,barPercentage:.7},
    {label:'Grid Import',data:data.map(r=>r.grid),backgroundColor:colors.grid,barPercentage:.7},
    {label:'Batt Charge',data:data.map(r=>r.chg),backgroundColor:colors.chg,barPercentage:.7},
    {label:'Batt Discharge',data:data.map(r=>r.dis),backgroundColor:colors.dis,barPercentage:.7},
  ];
  const opts=mkOpts();
  opts.scales.x={type:'category',ticks:{color:'#4a5070',font:{size:10}},grid:{color:'rgba(255,255,255,.04)'},border:{color:'rgba(255,255,255,.06)'}};
  opts.scales.y.ticks.callback=v=>v+' kWh';
  makeChart('ch-daily',{type:'bar',data:{labels,datasets},options:opts});
}

function buildTempChart(data){
  const inv=groupBy(data,'inverter_name');
  const colors=['#e74c3c','#f5a623','#4fc3f7','#9b59b6'];
  const datasets=[];
  let ci=0;
  Object.entries(inv).forEach(([name,rows])=>{
    const c=colors[ci++%colors.length];
    datasets.push({label:`Inv Temp ${name}`,data:rows.filter(r=>r.inv_temp).map(r=>({x:new Date(r.time),y:r.inv_temp})),borderColor:c,backgroundColor:'transparent',fill:false,tension:.3,borderWidth:1.5,pointRadius:0});
    datasets.push({label:`DC Temp ${name}`,data:rows.filter(r=>r.dc_temp).map(r=>({x:new Date(r.time),y:r.dc_temp})),borderColor:c,backgroundColor:'transparent',fill:false,tension:.3,borderWidth:1,borderDash:[4,4],pointRadius:0});
  });
  const opts=mkOpts();
  opts.scales.y.ticks.callback=v=>v+'°C';
  makeChart('ch-temp',{type:'line',data:{datasets},options:opts});
}

function buildPeaks(data){
  const fmtW=w=>w>=1000?(w/1000).toFixed(1)+' kW':w+' W';
  document.getElementById('a-peak-pv').textContent=fmtW(data.peak_pv||0);
  document.getElementById('a-peak-load').textContent=fmtW(data.peak_load||0);
}

function groupBy(arr,key){
  const m={};
  (arr||[]).forEach(r=>{const k=r[key];if(!m[k])m[k]=[];m[k].push(r);});
  return m;
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
  document.getElementById('wx-rad').textContent    = rad   != null ? Math.round(rad)  + ' W/m²' : '—';

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

async function loadSites(){
  const r=await fetch('/api/sites');
  const sites=await r.json();
  const sel=document.getElementById('site-sel');
  sel.innerHTML=sites.map(s=>`<option value="${s.name}">${s.display}</option>`).join('');
  if(sites.length){currentSite=sites[0].name;refresh();refreshWeather();}
}

function onSiteChange(){
  currentSite=document.getElementById('site-sel').value;
  if(liveTimer)clearTimeout(liveTimer);
  if(wxTimer)clearTimeout(wxTimer);
  // Reset weather strip to loading state for new site
  document.getElementById('wx-strip').classList.add('wx-empty');
  document.getElementById('wx-desc').textContent='Loading weather…';
  // Destroy charts so they rebuild fresh for new site
  Object.values(charts).forEach(c=>c.destroy());charts={};
  refresh();
  refreshWeather();
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
            WHERE time >= NOW() - INTERVAL '24 hours'
            AND site_name ILIKE %s
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        combined = query_all("""
            SELECT minute as time, SUM(avg_pv) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(pv1_power + COALESCE(pv2_power,0)) as avg_pv
              FROM solar_readings
              WHERE time >= NOW() - INTERVAL '24 hours'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        return {'per_inv': per_inv, 'combined': combined}

    elif chart == 'load':
        per_inv = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name, AVG(load_power) as load_w
            FROM solar_readings
            WHERE time >= NOW() - INTERVAL '24 hours'
            AND site_name ILIKE %s
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        combined = query_all("""
            SELECT minute as time, SUM(avg_load) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(load_power) as avg_load
              FROM solar_readings
              WHERE time >= NOW() - INTERVAL '24 hours'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        return {'per_inv': per_inv, 'combined': combined}

    elif chart == 'battery':
        power = query_all("""
            SELECT minute as time, SUM(avg_batt) * -1 as batt_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(battery_power) as avg_batt
              FROM solar_readings
              WHERE time >= NOW() - INTERVAL '24 hours'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        soc = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(battery_soc) as soc
            FROM solar_readings
            WHERE time >= NOW() - INTERVAL '24 hours'
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
              WHERE time >= NOW() - INTERVAL '24 hours'
              AND site_name ILIKE %s GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
        """, (S,))
        voltage = query_all("""
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(grid_voltage) as grid_v
            FROM solar_readings
            WHERE time >= NOW() - INTERVAL '24 hours'
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
            WHERE time >= NOW() - INTERVAL '24 hours'
            AND site_name ILIKE %s
            GROUP BY 1, 2 ORDER BY 1
        """, (S,))
        return rows

    elif chart == 'peaks':
        row = query_one("""
            SELECT
              COALESCE(MAX(pv_total), 0)   as peak_pv,
              COALESCE(MAX(load_total), 0) as peak_load
            FROM (
              SELECT ts, SUM(avg_pv) as pv_total, SUM(avg_load) as load_total
              FROM (
                SELECT DATE_TRUNC('minute', time) as ts, inverter_name,
                  AVG(pv1_power + COALESCE(pv2_power,0)) as avg_pv,
                  AVG(load_power) as avg_load
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