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
              MIN(load_val)  as load_kwh,
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
              AND daily_load_energy < 100
              ORDER BY inverter_name, time DESC
            ) sub
        """, (site,))
        load = float(daily_row.get('load_kwh') or 1)
        grid = float(daily_row.get('grid_kwh') or 0)
        pv   = float(daily_row.get('pv_kwh')   or 0)
        d['self_suff']       = max(0, min(100, round((1 - grid / max(load, 0.001)) * 100)))
        d['daily_load_kwh']  = round(load, 1)
        d['daily_grid_kwh']  = round(grid, 1)
        d['daily_pv_kwh']    = round(pv,   1)
        d['solar_savings_r'] = round((load - grid) * RATE, 2)
    except Exception as e:
        d['self_suff'] = 0
        log.warning(f"Daily counters error: {e}")

    return d

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SolarWatch Power Flow</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Barlow:wght@300;400;600;700;800&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #0a0c10;
  --surface: #111318;
  --s2:      #181c24;
  --border:  #232736;
  --text:    #e8eaf2;
  --muted:   #4a5070;
  --solar:   #f5a623;
  --green:   #2ecc71;
  --amber:   #f39c12;
  --red:     #e74c3c;
  --load:    #4fc3f7;
  --gin:     #e74c3c;
  --gout:    #2ecc71;
  --mono:    'DM Mono', monospace;
  --sans:    'Barlow', sans-serif;
}

html, body { width:100%; height:100%; background:var(--bg); color:var(--text); font-family:var(--sans); overflow:hidden; }

/* ── APP SHELL: header / main / footer ── */
.app {
  display: grid;
  grid-template-rows: 5.5vh 1fr 11vh;
  height: 100vh;
  width: 100vw;
}

/* ── HEADER ── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 2.5vw;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}

.logo {
  font-size: clamp(13px, 1.4vw, 20px);
  font-weight: 800;
  letter-spacing: -0.03em;
  display: flex;
  align-items: center;
  gap: 0.5vw;
}
.la { color: var(--solar); }
.ls { color: var(--muted); font-weight: 300; margin: 0 0.4vw; }
.lb { color: var(--muted); font-weight: 400; }

.hdr-right { display:flex; align-items:center; gap:2vw; }

.site-wrap { display:flex; align-items:center; gap:0.8vw; }
.site-lbl  { font-size:clamp(9px,.9vw,13px); color:var(--muted); letter-spacing:.1em; text-transform:uppercase; }

select {
  background: var(--s2);
  border: 1px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: clamp(11px, 1.1vw, 16px);
  font-weight: 700;
  padding: 4px 28px 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  outline: none;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%234a5070' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
}

.age-badge { font-family:var(--mono); font-size:clamp(9px,.9vw,13px); color:var(--muted); display:flex; align-items:center; gap:6px; }
.dot { width:clamp(6px,.6vw,9px); height:clamp(6px,.6vw,9px); border-radius:50%; background:var(--green); animation:pulse 2s infinite; }
.dot.stale { background:var(--red); animation:none; }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.7)} }

/* ── MAIN: left SOC panel + right flow ── */
main {
  display: grid;
  grid-template-columns: 26vw 1fr;
  overflow: hidden;
}

/* ── LEFT: SOC PANEL ── */
.soc-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2.5vh;
  padding: 2vh 2vw;
  border-right: 1px solid var(--border);
  position: relative;
  overflow: hidden;
}

/* Ambient glow */
.soc-panel::before {
  content:'';
  position:absolute;
  inset:0;
  pointer-events:none;
  transition: opacity .8s;
  opacity: 0;
}
.soc-panel.sg::before { background:radial-gradient(circle at 50% 55%,rgba(46,204,113,.1) 0%,transparent 68%); opacity:1; }
.soc-panel.sa::before { background:radial-gradient(circle at 50% 55%,rgba(243,156,18,.1) 0%,transparent 68%); opacity:1; }
.soc-panel.sr::before { background:radial-gradient(circle at 50% 55%,rgba(231,76,60,.1)  0%,transparent 68%); opacity:1; }

/* Status badge */
.s-badge {
  font-size: clamp(10px, 1.1vw, 16px);
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  padding: 5px 16px;
  border-radius: 20px;
  transition: all .4s;
  z-index: 1;
}
.sg .s-badge { background:rgba(46,204,113,.15);  color:var(--green); border:1px solid rgba(46,204,113,.35); }
.sa .s-badge { background:rgba(243,156,18,.15);  color:var(--amber); border:1px solid rgba(243,156,18,.35); }
.sr .s-badge { background:rgba(231,76,60,.15);   color:var(--red);   border:1px solid rgba(231,76,60,.35);  }

/* Gauge */
.gauge-wrap {
  position:relative;
  width: min(20vw, 30vh);
  aspect-ratio: 1;
  flex-shrink: 0;
  z-index: 1;
}
.gauge-wrap svg { width:100%; height:100%; }
.gauge-center {
  position:absolute; inset:0;
  display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:.4vh;
}
.gauge-num   { font-size:clamp(28px,4.8vw,68px); font-weight:800; line-height:1; font-family:var(--mono); transition:color .5s; letter-spacing:-.03em; }
.gauge-lbl   { font-size:clamp(9px,.85vw,13px); color:var(--muted); text-transform:uppercase; letter-spacing:.12em; }

/* Status message — BIG, this is the kitchen message */
.s-msg {
  font-size: clamp(14px, 1.8vw, 28px);
  font-weight: 700;
  text-align: center;
  line-height: 1.25;
  letter-spacing: -.01em;
  transition: color .5s;
  z-index: 1;
  padding: 0 1vw;
}

/* ── RIGHT: FLOW AREA ── */
.flow-area {
  position: relative;
  overflow: hidden;
}

#flow-svg {
  position:absolute; inset:0;
  width:100%; height:100%;
  pointer-events:none;
  overflow:visible;
}

/* Node cards — all equal via min-width + consistent padding */
.nc {
  position: absolute;
  transform: translate(-50%, -50%);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: clamp(10px, 1.2vw, 18px);
  padding: clamp(12px, 1.4vh, 22px) clamp(14px, 1.6vw, 28px);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: clamp(4px,.5vh,8px);
  min-width: clamp(130px, 14vw, 220px);
  width: clamp(130px, 14vw, 220px); /* FIXED width so all cards identical */
  transition: border-color .4s, box-shadow .4s;
}

.n-ico  { font-size:clamp(24px,3.2vw,52px); line-height:1; }
.n-lbl  { font-size:clamp(9px,.85vw,13px); color:var(--muted); letter-spacing:.14em; text-transform:uppercase; font-weight:600; }
.n-val  { font-size:clamp(22px,3vw,48px); font-weight:800; font-family:var(--mono); line-height:1; letter-spacing:-.02em; }
.n-sub  { font-size:clamp(9px,.85vw,13px); font-family:var(--mono); font-weight:500; }

.soc-bar      { width:80%; height:clamp(4px,.5vh,7px); background:var(--s2); border-radius:4px; overflow:hidden; }
.soc-bar-fill { height:100%; border-radius:4px; transition:width 1s ease, background .5s; }

/* Hub */
.hub {
  position:absolute;
  transform:translate(-50%,-50%);
  display:flex; flex-direction:column; align-items:center;
  gap: clamp(4px,.5vh,8px);
}
.hub-ring {
  width:clamp(62px,7.5vw,118px); height:clamp(62px,7.5vw,118px);
  border-radius:50%;
  background:var(--surface);
  border:2px solid rgba(245,166,35,.35);
  display:flex; align-items:center; justify-content:center;
  font-size:clamp(26px,3.2vw,52px);
  box-shadow: 0 0 clamp(16px,2vw,40px) rgba(245,166,35,.15), 0 0 clamp(40px,5vw,80px) rgba(245,166,35,.06);
  transition: box-shadow .5s, border-color .5s;
}
.hub-ring.on {
  box-shadow: 0 0 clamp(22px,2.8vw,56px) rgba(245,166,35,.4), 0 0 clamp(55px,6.5vw,110px) rgba(245,166,35,.12);
  border-color: rgba(245,166,35,.65);
}
.hub-lbl { font-size:clamp(8px,.75vw,11px); color:var(--muted); letter-spacing:.15em; text-transform:uppercase; }

/* Animated flow lines */
@keyframes df { to { stroke-dashoffset: -28; } }
@keyframes dr { to { stroke-dashoffset:  28; } }

.fl { fill:none; stroke-width:2.5; stroke-linecap:round; stroke-dasharray:8 20; }
.fwd  { animation: df 1s linear infinite; }
.rev  { animation: dr 1s linear infinite; }
.idle { opacity:.1; animation:none; }
.slow { animation-duration:2s; }
.med  { animation-duration:1.1s; }
.fast { animation-duration:.5s; }

/* ── FOOTER ── */
footer {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: clamp(12px, 3vw, 60px);
  padding: 0 4vw;
  border-top: 1px solid var(--border);
  background: var(--surface);
  flex-wrap: wrap;
}

.stat { display:flex; flex-direction:column; align-items:center; gap:3px; }
.st-lbl { font-size:clamp(9px,.8vw,12px); color:var(--muted); letter-spacing:.1em; text-transform:uppercase; }
.st-val { font-size:clamp(15px,1.7vw,26px); font-family:var(--mono); font-weight:500; }
</style>
</head>
<body>
<div class="app">

  <header>
    <div class="logo">
      <span class="la">◆</span> SolarWatch
      <span class="ls">/</span>
      <span class="lb">Power Flow</span>
    </div>
    <div class="hdr-right">
      <div class="site-wrap">
        <span class="site-lbl">Site</span>
        <select id="site-sel" onchange="onSiteChange()"></select>
      </div>
      <div class="age-badge">
        <div class="dot" id="dot"></div>
        <span id="age">—</span>
      </div>
    </div>
  </header>

  <main>

    <!-- LEFT: SOC + STATUS -->
    <div class="soc-panel sg" id="sp">
      <div class="s-badge" id="sbadge">GOOD</div>

      <div class="gauge-wrap">
        <svg viewBox="0 0 200 200">
          <circle cx="100" cy="100" r="80"
            fill="none" stroke="#1a1e2a" stroke-width="14"
            stroke-dasharray="440" stroke-dashoffset="110"
            stroke-linecap="round"
            transform="rotate(135 100 100)"/>
          <circle cx="100" cy="100" r="80"
            id="garc"
            fill="none" stroke="var(--green)" stroke-width="14"
            stroke-dasharray="330 440" stroke-dashoffset="0"
            stroke-linecap="round"
            transform="rotate(135 100 100)"
            style="transition:stroke-dasharray 1s ease,stroke .5s"/>
        </svg>
        <div class="gauge-center">
          <span class="gauge-num" id="gnum" style="color:var(--green)">—%</span>
          <span class="gauge-lbl">Battery SOC</span>
        </div>
      </div>

      <div class="s-msg" id="smsg">Loading...</div>
    </div>

    <!-- RIGHT: FLOW DIAGRAM -->
    <div class="flow-area" id="fa">

      <svg id="flow-svg" xmlns="http://www.w3.org/2000/svg"></svg>

      <!-- Hub centre -->
      <div class="hub" id="hub" style="left:50%;top:50%">
        <div class="hub-ring" id="hring">⚡</div>
        <span class="hub-lbl">Inverter</span>
      </div>

      <!-- Solar TL -->
      <div class="nc" id="nd-solar" style="left:27%;top:23%">
        <span class="n-ico">☀️</span>
        <span class="n-lbl">Solar</span>
        <span class="n-val" id="v-solar" style="color:var(--solar)">—</span>
      </div>

      <!-- Grid TR -->
      <div class="nc" id="nd-grid" style="left:73%;top:23%">
        <span class="n-ico">🔌</span>
        <span class="n-lbl">Grid</span>
        <span class="n-val" id="v-grid">—</span>
        <span class="n-sub" id="s-grid">—</span>
      </div>

      <!-- Battery BL -->
      <div class="nc" id="nd-batt" style="left:27%;top:77%">
        <span class="n-ico">🔋</span>
        <span class="n-lbl">Battery</span>
        <span class="n-val" id="v-batt">—</span>
        <div class="soc-bar"><div class="soc-bar-fill" id="sbar"></div></div>
        <span class="n-sub" id="s-batt">—</span>
      </div>

      <!-- Load BR -->
      <div class="nc" id="nd-load" style="left:73%;top:77%">
        <span class="n-ico">🏠</span>
        <span class="n-lbl">Load</span>
        <span class="n-val" id="v-load" style="color:var(--load)">—</span>
      </div>

    </div>
  </main>

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
  </footer>

</div>
<script>
function fmt(w){const a=Math.abs(w);return a>=1000?(a/1000).toFixed(1)+' kW':a+' W';}
function spd(w){const a=Math.abs(w);if(a<15)return 'idle';if(a<600)return 'slow';if(a<2500)return 'med';return 'fast';}
function dir(w,inv=false){if(Math.abs(w)<15)return 'idle';return(inv?w<0:w>0)?'fwd':'rev';}
function sc(s){return s>60?'var(--green)':s>25?'var(--amber)':'var(--red)';}

function status(d){
  if(d.soc>80||d.solar_w>3000) return{cls:'sg',badge:'PLENTY OF POWER',msg:'Safe to run heavy appliances ✓'};
  if(d.soc>40||d.solar_w>1000) return{cls:'sa',badge:'MODERATE',msg:'Light usage recommended'};
  return{cls:'sr',badge:'CONSERVE POWER',msg:'Avoid heavy appliances'};
}

function drawLines(d){
  const fa=document.getElementById('fa');
  const svg=document.getElementById('flow-svg');
  const W=fa.offsetWidth, H=fa.offsetHeight;
  const pos={
    hub:  {x:W*.50,y:H*.50},
    solar:{x:W*.27,y:H*.23},
    grid: {x:W*.73,y:H*.23},
    batt: {x:W*.27,y:H*.77},
    load: {x:W*.73,y:H*.77},
  };
  const bc=d.batt_w<-15?'var(--green)':'var(--amber)';
  const gc=d.grid_w>15?'var(--gin)':d.grid_w<-15?'var(--gout)':'var(--muted)';
  const pad=Math.min(W,H)*.09;

  function seg(a,b,w,di,sp,col){
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

  svg.innerHTML='';
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.appendChild(seg(pos.solar,pos.hub, d.solar_w, dir(d.solar_w),     spd(d.solar_w),'var(--solar)'));
  svg.appendChild(seg(pos.grid, pos.hub, d.grid_w,  dir(d.grid_w),      spd(d.grid_w), gc));
  svg.appendChild(seg(pos.hub,  pos.batt,d.batt_w,  dir(-d.batt_w),     spd(d.batt_w), bc));
  svg.appendChild(seg(pos.hub,  pos.load,d.load_w,  dir(d.load_w),      spd(d.load_w),'var(--load)'));
}

function render(d){
  window._ld=d;
  const soc=d.soc, bc=d.batt_w<-15, battC=bc?'var(--green)':'var(--amber)';
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

  // Status panel
  const sp=document.getElementById('sp');
  sp.className='soc-panel '+st.cls;
  document.getElementById('sbadge').textContent=st.badge;
  document.getElementById('smsg').textContent=st.msg;
  const msgColor=st.cls==='sg'?'var(--green)':st.cls==='sa'?'var(--amber)':'var(--red)';
  document.getElementById('smsg').style.color=msgColor;

  // Nodes
  document.getElementById('v-solar').textContent=fmt(d.solar_w);
  document.getElementById('v-grid').textContent=fmt(d.grid_w);
  document.getElementById('v-grid').style.color=gc;
  document.getElementById('s-grid').textContent=gl;
  document.getElementById('s-grid').style.color=gc;
  document.getElementById('v-batt').textContent=fmt(d.batt_w);
  document.getElementById('v-batt').style.color=battC;
  document.getElementById('s-batt').textContent=bl;
  document.getElementById('s-batt').style.color=battC;
  document.getElementById('sbar').style.width=soc+'%';
  document.getElementById('sbar').style.background=sc(soc);
  document.getElementById('v-load').textContent=fmt(d.load_w);

  // Card border glows
  document.getElementById('nd-solar').style.cssText+=`;border-color:${d.solar_w>50?'rgba(245,166,35,.45)':''};box-shadow:${d.solar_w>50?'0 0 20px rgba(245,166,35,.08)':'none'}`;
  document.getElementById('nd-grid').style.cssText+=`;border-color:${Math.abs(d.grid_w)>15?(d.grid_w>0?'rgba(231,76,60,.4)':'rgba(46,204,113,.4)'):''};`;
  document.getElementById('nd-batt').style.cssText+=`;border-color:${Math.abs(d.batt_w)>15?'rgba(46,204,113,.4)':''};`;
  document.getElementById('nd-load').style.cssText+=`;border-color:${d.load_w>50?'rgba(79,195,247,.35)':''};`;
  document.getElementById('hring').className='hub-ring'+(d.solar_w>50?' on':'');

  // Footer
  document.getElementById('f-bv').textContent=d.batt_v.toFixed(1)+'V';
  document.getElementById('f-bt').textContent=d.batt_temp.toFixed(1)+'°C';
  document.getElementById('f-gv').textContent=d.grid_v.toFixed(0)+'V';
  document.getElementById('f-hz').textContent=d.grid_hz.toFixed(2)+'Hz';
  const ss=d.self_suff??0;
  const sse=document.getElementById('f-ss');
  sse.textContent=ss+'%';
  sse.style.color=ss>80?'var(--green)':ss>50?'var(--amber)':'var(--red)';
  document.getElementById('f-tl').textContent=(d.daily_load_kwh??0)+' kWh';
  document.getElementById('f-tg').textContent=(d.daily_grid_kwh??0)+' kWh';
  document.getElementById('f-pv').textContent=(d.daily_pv_kwh??0)+' kWh';
  const sv=d.solar_savings_r??0;
  document.getElementById('f-sv').textContent='R'+sv.toFixed(2);

  // Age + dot
  document.getElementById('dot').className='dot'+(d.stale?' stale':'');
  document.getElementById('age').textContent=d.age_s!==null?d.age_s+'s ago':'live';

  drawLines(d);
}

// ── BOOT ──────────────────────────────────────────────────────────────────────
let currentSite=null, timer=null;

async function loadSites(){
  const r=await fetch('/api/sites');
  const sites=await r.json();
  const sel=document.getElementById('site-sel');
  sel.innerHTML=sites.map(s=>`<option value="${s.name}">${s.display}</option>`).join('');
  if(sites.length){currentSite=sites[0].name;refresh();}
}

function onSiteChange(){
  currentSite=document.getElementById('site-sel').value;
  if(timer)clearTimeout(timer);
  refresh();
}

async function refresh(){
  if(!currentSite)return;
  try{
    const r=await fetch(`/api/flow?site=${encodeURIComponent(currentSite)}`);
    const d=await r.json();
    if(!d.error)render(d);
  }catch(e){console.error(e);}
  timer=setTimeout(refresh,10000);
}

new ResizeObserver(()=>{if(window._ld)drawLines(window._ld);}).observe(document.getElementById('fa'));
loadSites();
</script>
</body>
</html>"""

# ── HTTP SERVER ───────────────────────────────────────────────────────────────
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