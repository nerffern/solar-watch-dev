# Sunsynk Cloud API Reference

Documented from live HAR capture against inverter SN `2506303417` (Penguin site).
Base URL: `https://api.sunsynk.net/api`
All requests require: `Authorization: Bearer {token}`

---

## Authentication

### Get RSA Public Key
```
GET /anonymous/publicKey?nonce={ms_timestamp}&source=sunsynk&sign={md5(nonce+source)}
→ data: "base64_rsa_public_key"
```

### Login
```
POST /oauth/token/new
Body: {username, password (RSA encrypted), grant_type:"password",
       client_id:"csp-web", source:"sunsynk", nonce, sign}
→ data.access_token
```
Token TTL: ~1 hour. Sign = md5("nonce={nonce}&source=sunsynk{rsa_key[:10]}")

---

## Plant / Site Endpoints

### List Plants
```
GET /v1/plants?page=1&limit=20
→ data.infos[]: {id, name, pac, etoday, etotal, address, status}
```

### Plant Realtime Summary
```
GET /v1/plant/{plant_id}/realtime
→ data: {pac, etoday, emonth, eyear, etotal, currency{code,text}, income}
```
Used for: site-level daily/monthly/yearly totals in kWh.

---

## Inverter Endpoints

### Inverter Info
```
GET /v1/inverter/{sn}
→ data: {sn, alias, status, runStatus, pac, etoday, etotal, emonth, eyear,
         ratePower, brand, model, pvNum, version{masterVer,softVer,hmiVer,bmsVer}}
```
Used for: firmware versions, rated power, inverter model.

### Live Power Flow  ✅ USED IN SOLARWATCH
```
GET /v1/inverter/{sn}/flow
→ data: {
    soc: 86.0,                    → battery_soc
    battPower: -2601,             → battery_power (negative = charging)
    gridOrMeterPower: 12,         → grid_power
    loadOrEpsPower: 344,          → load_power (use /load/realtime for accuracy)
    pv: [{power:1339}, {power:1576}],  → pv1_power, pv2_power
    pvPower: 0,                   (total, unreliable — use pv[] array instead)
    pvTo, toBat, toLoad, toGrid,  (boolean flow direction flags)
    existsGrid, existsMeter,      (presence flags)
    upsLoadPower: 344             (same as loadOrEpsPower for single-phase)
  }
```

### PV String Detail  ✅ USED IN SOLARWATCH
```
GET /v1/inverter/{sn}/realtime/input
→ data: {
    pac: 2915,                    (total AC power)
    pvIV: [{
      pvNo: 1,                    → string number
      vpv: "254.8",               → pv1_voltage
      ipv: "5.3",                 → pv1_current
      ppv: "1339.0",              → pv1_power (more precise than flow)
      todayPv: "0.0",             (always 0.0 — use etoday instead)
      sn, time
    }, {pvNo:2, ...}],
    etoday: 17.8,                 → daily_pv_energy
    etotal: 1952.8                → total_pv_energy
  }
```

### AC Output (Load side)
```
GET /v1/inverter/{sn}/realtime/output
→ data: {
    vip: [{volt:"231.7", current:"1.6", power:333}],   (AC output)
    pInv: 3275,                   (inverter output power W)
    pac: 333,                     (load power W — less accurate than /load/realtime)
    fac: 50.2                     → grid_frequency (also in /grid/realtime)
  }
```
Note: use `/load/realtime` for load_power — more accurate.

---

## Battery Endpoints  ✅ USED IN SOLARWATCH

### Battery Realtime
```
GET /v1/inverter/battery/{sn}/realtime?sn={sn}&lan=en
→ data: {
    voltage: "54.4",              → battery_voltage
    current: -47.83,              → battery_current (negative = charging)
    temp: "23.7",                 → battery_temp
    soc: "86.0",                  → battery_soc (string, matches flow.soc)
    power: -2601,                 → battery_power (matches flow.battPower)
    
    etodayChg: "10.2",            → daily_battery_charge
    etodayDischg: "2.6",          → daily_battery_discharge
    emonthChg: "83.0",            (monthly charge kWh)
    emonthDischg: "65.1",         (monthly discharge kWh)
    eyearChg: "967.3",            (yearly charge kWh)
    eyearDischg: "781.1",         (yearly discharge kWh)
    etotalChg: "1130.4",          (lifetime charge kWh)
    etotalDischg: "909.5",        (lifetime discharge kWh)
    
    bmsVolt: 54.22,               (BMS-reported voltage — slightly different from inverter)
    bmsCurrent: 50.0,             (BMS-reported current)
    bmsTemp: 23.7,                (BMS temperature — same as temp for single battery)
    bmsSoc: 86.0,                 (BMS SOC)
    
    capacity: "300.0",            (battery capacity Ah)
    correctCap: 300,
    chargeVolt: 55.8,             (charge cutoff voltage)
    dischargeVolt: 0.0,           (discharge cutoff voltage)
    chargeCurrentLimit: 285.0,    (max charge current A)
    dischargeCurrentLimit: 315.0, (max discharge current A)
    status: 1,                    (1=charging, 0=discharging, 2=idle)
    type: 1                       (1=lithium)
  }
```
Note: `current2`, `voltage2` etc are for second battery string — null if single battery.

### Battery Historical (day)
```
GET /v1/inverter/battery/{sn}/day?lan=en&date=YYYY-MM-DD&column=p_bms
→ data: {infos: [{unit:"W", records:[{time, value},...]}]}
```
Columns: `p_bms` (power W), `soc` (%), `volt` (V), `curr` (A), `temp` (°C)

### Battery Historical (month)
```
GET /v1/inverter/battery/{sn}/month?lan=en&date=YYYY-MM
→ data: {infos: [{unit:"kWh", records:[{time, value}]}]}
```

---

## Grid Endpoints  ✅ USED IN SOLARWATCH

### Grid Realtime
```
GET /v1/inverter/grid/{sn}/realtime?sn={sn}
→ data: {
    vip: [{volt:"231.6", current:"1.5", power:12}],  → grid_voltage, grid_current, grid_power
    fac: 50.15,                   → grid_frequency
    pac: 12,                      (grid power W — same as vip[0].power)
    
    etodayFrom: "2.1",            → daily_grid_import (bought from grid today kWh)
    etodayTo: "0.0",              → daily_grid_export (sold to grid today kWh)
    etotalFrom: "563.7",          (lifetime grid import kWh)
    etotalTo: "0.8",              (lifetime grid export kWh)
    
    status: 1,                    (1=connected)
    acRealyStatus: 1,             (relay closed)
    pf: 1.0,                      (power factor)
    limiterTotalPower: 29         (export limiter power W)
  }
```

### Grid Historical (day)
```
GET /v1/inverter/grid/{sn}/day?lan=en&date=YYYY-MM-DD&column=pac
→ data: {infos: [{unit:"W", records:[{time, value}]}]}
```
Columns: `pac` (power W), `volt` (V), `curr` (A), `freq` (Hz)

---

## Load Endpoints  ✅ USED IN SOLARWATCH

### Load Realtime
```
GET /v1/inverter/load/{sn}/realtime?sn={sn}
→ data: {
    totalPower: 344,              → load_power (most accurate)
    upsPowerTotal: 344.0,         (same as totalPower for single-phase)
    dailyUsed: 11.8,              → daily_load_energy (kWh)
    totalUsed: 2194.0,            (lifetime load energy kWh)
    vip: [{volt:"232.1", current:"0.0", power:344}],  → load_voltage
    loadFac: 50.15                (load side frequency)
  }
```

### Load Historical (day)
```
GET /v1/inverter/load/{sn}/day?lan=en&date=YYYY-MM-DD&column=pac
→ data: {infos: [{unit:"W", records:[{time, value}]}]}
```

---

## Output Endpoints (Historical)

### Output Historical (day)
```
GET /v1/inverter/{sn}/output/day?lan=en&date=YYYY-MM-DD&column={col}
→ data: {infos: [{unit, records:[{time, value}]}]}
```
Columns:
- `pac`  — AC output power W
- `ppv`  — PV input power W
- `iac1` — AC output current A
- `vac1` — AC output voltage V

---

## Temperature Endpoints (Historical Only)

Live inverter temperature is NOT available via any realtime endpoint.
All 4 candidate realtime endpoints (`/realtime/detail`, `/realtime`, `/detail`, `/temperature`)
return null/404. Temperature is only available as historical chart data:

```
GET /v1/inverter/{sn}/output/day?lan=en&date=YYYY-MM-DD&column=dc_temp
GET /v1/inverter/{sn}/output/day?lan=en&date=YYYY-MM-DD&column=temp
→ data: {infos: [{unit:"°C", records:[{time:"YYYY-MM-DD HH:MM:SS", value:"61.5"},...]}]}
```

`dc_temp` = DC side (MPPT) temperature — maps to `dc_temp` in solar_readings (confirmed ✓)  
`temp`    = AC side (inverter) temperature — maps to `inverter_temp` in solar_readings  
Note: column name `ac_temp` returns empty `infos:[]` — use `temp` instead

**SolarWatch approach:** Fetch these endpoints every 5 polls (~5 min) and use the
last non-zero record value. This keeps overhead low while providing temperature data.
The Sunsynk inverter updates these values approximately every 5 minutes anyway.

---

## NOT AVAILABLE via Cloud API (for this inverter)

These fields are NULL or require workarounds:
- `inverter_temp` — only via day history endpoint (see above)
- `dc_temp`       — only via day history endpoint (see above)
- `/realtime/battery` (old path) — returns null; use `/inverter/battery/{sn}/realtime`
- `/realtime/grid`    (old path) — returns null; use `/inverter/grid/{sn}/realtime`

---

## Field Mapping Summary (solar_readings → API source)

| DB Column | API Endpoint | Field |
|-----------|-------------|-------|
| pv1_voltage | /realtime/input | pvIV[0].vpv |
| pv1_current | /realtime/input | pvIV[0].ipv |
| pv1_power | /realtime/input | pvIV[0].ppv |
| pv2_voltage | /realtime/input | pvIV[1].vpv |
| pv2_current | /realtime/input | pvIV[1].ipv |
| pv2_power | /realtime/input | pvIV[1].ppv |
| battery_voltage | /battery/{sn}/realtime | voltage |
| battery_current | /battery/{sn}/realtime | current |
| battery_power | /flow | battPower |
| battery_soc | /flow | soc |
| battery_temp | /battery/{sn}/realtime | temp |
| grid_voltage | /grid/{sn}/realtime | vip[0].volt |
| grid_current | /grid/{sn}/realtime | vip[0].current |
| grid_power | /grid/{sn}/realtime | vip[0].power |
| grid_frequency | /grid/{sn}/realtime | fac |
| load_power | /load/{sn}/realtime | totalPower |
| load_voltage | /load/{sn}/realtime | vip[0].volt |
| daily_pv_energy | /realtime/input | etoday |
| total_pv_energy | /realtime/input | etotal |
| daily_battery_charge | /battery/{sn}/realtime | etodayChg |
| daily_battery_discharge | /battery/{sn}/realtime | etodayDischg |
| daily_grid_import | /grid/{sn}/realtime | etodayFrom |
| daily_grid_export | /grid/{sn}/realtime | etodayTo |
| daily_load_energy | /load/{sn}/realtime | dailyUsed |
| inverter_temp | ❌ not available | — |
| dc_temp | ❌ not available | — |

---

## Future Use Cases

### Automation (change inverter settings)
```
POST /api/v1/inverter/{sn}/settings/...
```
Potential uses: adjust min SOC based on weather forecast, enable/disable grid charge
based on time-of-use tariffs, set charge/discharge windows.

### Historical Data Backfill
```
GET /v1/inverter/{sn}/output/day?date=YYYY-MM-DD&column=ppv
GET /v1/inverter/battery/{sn}/day?date=YYYY-MM-DD&column=soc
GET /v1/inverter/grid/{sn}/day?date=YYYY-MM-DD&column=pac
GET /v1/inverter/load/{sn}/day?date=YYYY-MM-DD&column=pac
```
Can be used to backfill historical data for new Sunsynk sites by
iterating dates and inserting into solar_readings.

### Monthly/Yearly Energy Reports
```
GET /v1/inverter/battery/{sn}/month?date=YYYY-MM
→ Monthly charge/discharge kWh
```
Useful for monthly billing reconciliation and performance reports.