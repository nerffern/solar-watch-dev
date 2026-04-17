# ☀️ SolarWatch

Multi-site solar inverter monitoring platform. Polls Deye inverters directly over LAN/WAN via the Solarman V5 Modbus protocol, and Sunsynk inverters via the Sunsynk cloud API. Fetches live weather from Open-Meteo (free, no API key). All data is written to a central PostgreSQL database and visualised in Grafana and a built-in live power flow web app.

---

## Architecture

```
Central Server (one process)
│
├── collector.py          ← orchestrates all sites
│   ├── deye_worker.py    ← Solarman V5 Modbus over WAN/LAN
│   ├── sunsynk_worker.py ← api.sunsynk.net cloud API
│   └── weather_worker.py ← Open-Meteo API (free, no key, every 15 min)
│
├── powerflow_server.py   ← standalone live power flow web app (port 8765)
│
└── PostgreSQL (solarwatch DB)
    ├── sites             ← master registry of all sites, inverters & coordinates
    ├── solar_readings    ← all inverter data, tagged by site_name
    └── weather_readings  ← weather data per site (temp, cloud, rain, UV, etc.)
```

Everything runs from one server. Deye sites are reached directly over WAN IP. Sunsynk sites are polled via the cloud API. Weather is fetched from Open-Meteo using each site's GPS coordinates. Adding a new site requires only a database insert — no code changes, no restarts.

---

## Sites

| Site | Type | Inverters |
|------|------|-----------|
| Selati | Deye SUN-5K-SG01LP1-EU | 2 × Deye (WAN: 192.168.10.x) |
| Lanner | Deye SUN-5K-SG01LP1-EU | 2 × Deye (WAN: 100.100.6.x) |
| Penguin | Sunsynk (cloud API) | 1 × Sunsynk (SN: 2506303417) |

---

## File Structure

```
solar-watch-dev/
├── collector.py            # Main process — polls all sites + weather, writes to DB
├── deye_worker.py          # Deye/Solarman V5 Modbus poller (stateless)
├── sunsynk_worker.py       # Sunsynk cloud API poller (stateless)
├── weather_worker.py       # Open-Meteo weather poller (stateless, new)
├── powerflow_server.py     # Standalone live power flow web app (port 8765)
├── setup.sql               # DB creation, schema, site seeds — run once (fresh deploy)
├── migrate_weather.sql     # Live DB migration — run once on existing deployments
├── SolarWatch_Dashboard_v1.json  # Grafana dashboard — import via UI
├── solarwatch.service      # systemd unit file
├── requirements.txt
└── .env.example            # Copy to .env and fill in credentials
```

---

## Prerequisites

- Python 3.11+
- PostgreSQL 14+ (with HAProxy or direct connection)
- Grafana 10+
- Network access to inverter dongle IPs (LAN or WAN)
- Internet access for Sunsynk cloud API and Open-Meteo weather API

---

## Fresh Deployment

### 1. Database setup

Run as the `postgres` superuser on your DB host:

```bash
psql -h postgres-ha.hfisystems.com -U postgres -f setup.sql
```

This creates the `solarwatch_user`, `solarwatch` database, all tables (`sites`, `solar_readings`, `weather_readings`), indexes, and seeds the Selati and Lanner sites with GPS coordinates.

> **Note:** Lanner `Inverter_1` serial number is pending confirmation. Update it once confirmed:
> ```sql
> UPDATE sites
> SET inverters = jsonb_set(inverters, '{0,inverter_sn}', '"YOUR_ACTUAL_SN"')
> WHERE site_name = 'Lanner';
> ```

### 2. Deploy the collector

```bash
# Create system user
sudo useradd -r -s /bin/false solarwatch

# Deploy files
sudo mkdir -p /opt/solarwatch/logs
sudo cp collector.py deye_worker.py sunsynk_worker.py weather_worker.py \
         powerflow_server.py requirements.txt /opt/solarwatch/
sudo cp .env.example /opt/solarwatch/.env
sudo chown -R solarwatch:solarwatch /opt/solarwatch

# Python venv
sudo python3 -m venv /opt/solarwatch/venv
sudo /opt/solarwatch/venv/bin/pip install -r /opt/solarwatch/requirements.txt

# Edit credentials
sudo nano /opt/solarwatch/.env

# Systemd
sudo cp solarwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solarwatch

# Check logs
sudo journalctl -u solarwatch -f
```

### 3. Power flow web app (optional but recommended)

`powerflow_server.py` is a standalone HTTP server serving a full-screen live dashboard on port 8765. Run it separately or add a second systemd service:

```bash
sudo /opt/solarwatch/venv/bin/python powerflow_server.py
# Open: http://your-server:8765
```

### 4. Grafana

1. Add a new **PostgreSQL** datasource in Grafana:
   - Host: `postgres-ha.hfisystems.com:5432`
   - Database: `solarwatch`
   - User: `solarwatch_user`
   - UID: `solarwatch_pg` ← must match exactly
2. Import `SolarWatch_Dashboard_v1.json` via **Dashboards → Import**
3. A **Site** dropdown appears at the top — defaults to All, can filter to any individual site

---

## Upgrading an Existing Deployment

If upgrading from a version **before weather support** was added, run the migration once:

```bash
psql -h postgres-ha.hfisystems.com -U postgres -d solarwatch -f migrate_weather.sql
```

This safely adds `latitude`/`longitude` columns to `sites` and creates the `weather_readings` table. It is idempotent — safe to re-run.

Then deploy updated files and restart:

```bash
sudo cp collector.py weather_worker.py powerflow_server.py /opt/solarwatch/
sudo /opt/solarwatch/venv/bin/pip install -r requirements.txt
sudo systemctl restart solarwatch
```

---

## Adding a New Site

No code changes needed. Just insert into the `sites` table and the collector picks it up within 5 minutes.

**Deye site:**
```sql
INSERT INTO sites (site_name, display_name, source_type, location, latitude, longitude, inverters)
VALUES (
    'NewSite', 'HFI New Site', 'deye', 'Location, ZA',
    -25.7461, 28.1881,
    '[
        {"name":"Inverter_1","ip":"x.x.x.x","dongle_serial":1234567890,"inverter_sn":"XXXXXXXXXXXX"},
        {"name":"Inverter_2","ip":"x.x.x.x","dongle_serial":1234567891,"inverter_sn":"XXXXXXXXXXXX"}
    ]'::jsonb
);
```

**Sunsynk site:**
```sql
INSERT INTO sites (site_name, display_name, source_type, sunsynk_username, sunsynk_password,
                   latitude, longitude)
VALUES ('MySite', 'HFI My Site', 'sunsynk', 'user@example.com', 'password',
        -26.2041, 28.0473);

-- Then add the inverter SN (required — collector cannot poll without it):
UPDATE sites
SET inverters = '[{"name": "Inverter_1", "inverter_sn": "YOUR_INVERTER_SN"}]'::jsonb
WHERE site_name = 'MySite';
```

> ⚠️ GPS coordinates are optional — sites without them skip weather polling silently.
>
> ⚠️ Store Sunsynk credentials only in the database, never in this repo or `.env`.
>
> The inverter SN is required. Find it on the inverter sticker or the Sunsynk app under **Device → Inverter Info**.

---

## Setting Coordinates for Existing Sites

```sql
UPDATE sites SET latitude = -XX.XXXX, longitude = XX.XXXX WHERE site_name = 'Lanner';
UPDATE sites SET latitude = -XX.XXXX, longitude = XX.XXXX WHERE site_name = 'Penguin';
```

Use Google Maps: right-click any location → the first item in the context menu is the lat/lon.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PG_HOST` | `postgres-ha.hfisystems.com` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_DB` | `solarwatch` | Database name |
| `PG_USER` | `solarwatch_user` | DB user |
| `PG_PASS` | *(required)* | DB password |
| `PG_SSLMODE` | `prefer` | SSL mode |
| `POLL_INTERVAL` | `60` | Seconds between inverter poll cycles |
| `MAX_RETRIES` | `3` | Retries per inverter per cycle |
| `RETRY_DELAY` | `5` | Seconds between retries |
| `CONFIG_RELOAD` | `300` | Seconds between site config reloads from DB |
| `WEATHER_INTERVAL` | `900` | Seconds between weather fetches per site (15 min) |
| `PORT` | `8765` | Port for the powerflow web app |

---

## Data Collected

### Inverter data (per inverter, every 60s)

| Category | Fields |
|----------|--------|
| PV | voltage, current, power (strings 1 & 2) |
| Battery | voltage, current, power, SOC %, temperature |
| Grid | voltage, frequency, power (±import/export), current |
| Load | power, voltage |
| Temperatures | inverter, DC heatsink, battery |
| Daily energy | PV, load, grid import/export, battery charge/discharge |
| Lifetime | total PV energy |
| CT clamp | main incomer power, load-side power |
| Meta | poll duration (ms), poll success flag |

> Sunsynk sites: per-string voltage/current and CT clamp fields are not available via the cloud API and will be `NULL`.

### Weather data (per site, every 15 min)

| Field | Description |
|-------|-------------|
| `temp_c` | Air temperature °C |
| `feels_like_c` | Apparent temperature °C |
| `cloud_cover` | Cloud cover 0–100% |
| `precipitation` | Rainfall mm (last hour) |
| `wind_speed` | Wind speed km/h |
| `wind_direction` | Wind direction degrees |
| `humidity` | Relative humidity % |
| `weather_code` | WMO weather interpretation code |
| `uv_index` | UV index 0–11+ |
| `sunrise` / `sunset` | Local sunrise/sunset times |
| `solar_rad` | Shortwave solar radiation W/m² |
| `is_day` | Boolean daylight indicator |

Weather is sourced from [Open-Meteo](https://open-meteo.com) — completely free, no API key required, updated hourly by the provider.

---

## Modbus Register Map (Deye SUN-5K-SG01LP1-EU)

Register addresses confirmed against [kellerza/sunsynk](https://github.com/kellerza/sunsynk) single-phase register map.

| Register | Address | Scale | Notes |
|----------|---------|-------|-------|
| PV1 Voltage | 109 | ×0.1 | V |
| PV1 Current | 110 | ×0.1 | A |
| PV1 Power | 186 | ×1 | W, signed |
| PV2 Voltage | 111 | ×0.1 | V |
| PV2 Current | 112 | ×0.1 | A |
| PV2 Power | 187 | ×1 | W, signed |
| Battery Voltage | 183 | ×0.01 | V |
| Battery Current | 191 | ×0.01 | A, signed (+charge/-discharge) |
| Battery Power | 190 | ×1 | W, signed |
| Battery SOC | 184 | ×1 | % |
| Battery Temp | 182 | ×0.1 | °C, offset-encoded |
| Grid Voltage | 150 | ×0.1 | V |
| Grid Frequency | 79 | ×0.01 | Hz |
| Grid Power | 169 | ×1 | W, signed (+import/-export) |
| Inverter Temp | 90 | ×0.1 | °C, offset-encoded |
| DC Temp | 91 | ×0.1 | °C, offset-encoded |

Temperature registers are offset-encoded: `raw > 1000` → subtract 1000. `raw > 900` → invalid sentinel, treated as 0.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `Poll failed: Connection refused` | Is the dongle IP reachable? `nc -zv <ip> 8899` |
| `All registers return None` | Wrong dongle serial — verify against sticker |
| `DB write failed` | Check HAProxy health, DB credentials in `.env` |
| Inverter skipped with `CONFIRM_PENDING` | Update `inverter_sn` in the `sites` table |
| Sunsynk login failed | Credentials wrong or password needs updating in DB |
| Site not being polled | Check `enabled = TRUE` in `sites` table |
| Config change not picked up | Wait up to `CONFIG_RELOAD` seconds (default 5 min), or restart service |
| Weather strip shows "Loading weather…" | Coordinates not set — run `UPDATE sites SET latitude/longitude` |
| No rows in `weather_readings` | Verify `migrate_weather.sql` was run; check logs for weather fetch errors |
| `zoneinfo` / timezone error | Run `pip install tzdata` in the venv |

---

## Security Notes

- Never commit `.env` to git — it is listed in `.gitignore`
- Sunsynk credentials are stored in the `sites` table — restrict DB access accordingly
- The `solarwatch_user` DB role has access only to the `solarwatch` database
- Consider encrypting the `sunsynk_password` column at rest for production use
- Open-Meteo requires no credentials — no secrets to manage
