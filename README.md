# ☀️ SolarWatch

Multi-site solar inverter monitoring platform. Polls Deye inverters directly over LAN/WAN via the Solarman V5 Modbus protocol, and Sunsynk inverters via the Sunsynk cloud API. All data is written to a central PostgreSQL database and visualised in Grafana.

---

## Architecture

```
Central Server (one process)
│
├── collector.py          ← orchestrates all sites
│   ├── deye_worker.py    ← Solarman V5 Modbus over WAN/LAN
│   └── sunsynk_worker.py ← api.sunsynk.net cloud API
│
└── PostgreSQL (solarwatch DB)
    ├── sites             ← master registry of all sites & inverters
    └── solar_readings    ← all inverter data, tagged by site_name
```

Everything runs from one server. Deye sites are reached directly over WAN IP. Sunsynk sites are polled via the cloud API from the same server. Adding a new site requires only a database insert — no code changes, no restarts.

---

## Sites

| Site | Type | Inverters |
|------|------|-----------|
| Selati | Deye SUN-5K-SG01LP1-EU | 2 × Deye (WAN: 192.168.10.x) |
| Lanner | Deye SUN-5K-SG01LP1-EU | 2 × Deye (WAN: 100.100.6.x) |

---

## File Structure

```
solar-watch-dev/
├── collector.py          # Main process — polls all sites, writes to DB
├── deye_worker.py        # Deye/Solarman V5 Modbus poller (stateless)
├── sunsynk_worker.py     # Sunsynk cloud API poller (stateless)
├── setup.sql             # DB creation, schema, site seeds — run once
├── import_selati.py      # One-time migration of historical data from old DB
├── import_selati.sql     # Reference SQL for manual migration (see import_selati.py)
├── SolarWatch_Dashboard_v1.json  # Grafana dashboard — import via UI
├── solarwatch.service    # systemd unit file
├── requirements.txt
└── .env.example          # Copy to .env and fill in credentials
```

---

## Prerequisites

- Python 3.11+
- PostgreSQL 14+ (with HAProxy or direct connection)
- Grafana 10+
- Network access to inverter dongle IPs (LAN or WAN)

---

## Deployment

### 1. Database setup

Run as the `postgres` superuser on your DB host:

```bash
psql -h postgres-ha.hfisystems.com -U postgres -f setup.sql
```

This creates the `solarwatch_user`, `solarwatch` database, `sites` and `solar_readings` tables, indexes, and seeds the Selati and Lanner sites.

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
sudo cp collector.py deye_worker.py sunsynk_worker.py requirements.txt /opt/solarwatch/
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

### 3. Grafana

1. Add a new **PostgreSQL** datasource in Grafana:
   - Host: `postgres-ha.hfisystems.com:5432`
   - Database: `solarwatch`
   - User: `solarwatch_user`
   - UID: `solarwatch_pg` ← must match exactly
2. Import `SolarWatch_Dashboard_v1.json` via **Dashboards → Import**
3. A **Site** dropdown appears at the top — defaults to All, can filter to any individual site

---

## Adding a New Site

No code changes needed. Just insert a row into the `sites` table and the collector picks it up within 5 minutes (configurable via `CONFIG_RELOAD`).

**Deye site:**
```sql
INSERT INTO sites (site_name, display_name, source_type, location, inverters)
VALUES (
    'NewSite', 'HFI New Site', 'deye', 'Location, ZA',
    '[
        {"name":"Inverter_1","ip":"x.x.x.x","dongle_serial":1234567890,"inverter_sn":"XXXXXXXXXXXX"},
        {"name":"Inverter_2","ip":"x.x.x.x","dongle_serial":1234567891,"inverter_sn":"XXXXXXXXXXXX"}
    ]'::jsonb
);
```

**Sunsynk site:**
```sql
INSERT INTO sites (site_name, display_name, source_type, sunsynk_username, sunsynk_password)
VALUES ('MySite', 'HFI My Site', 'sunsynk', 'user@example.com', 'password');
```

> ⚠️ Store Sunsynk credentials only in the database, never in this repo or `.env`.

---

## Importing Historical Selati Data

If you want to migrate data from the old `solar` database into `solarwatch`:

```bash
# Add old and new DB credentials to .env:
# OLD_PG_DB=solar  OLD_PG_USER=solar_user  OLD_PG_PASS=xxx
# NEW_PG_DB=solarwatch  NEW_PG_USER=solarwatch_user  NEW_PG_PASS=xxx

python3 import_selati.py
```

The old database is never modified. The script streams data in batches and reports progress.

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
| `POLL_INTERVAL` | `60` | Seconds between poll cycles |
| `MAX_RETRIES` | `3` | Retries per inverter per cycle |
| `RETRY_DELAY` | `5` | Seconds between retries |
| `CONFIG_RELOAD` | `300` | Seconds between site config reloads from DB |

---

## Data Collected (per inverter, every 60s)

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

---

## Security Notes

- Never commit `.env` to git — it is listed in `.gitignore`
- Sunsynk credentials are stored in the `sites` table — restrict DB access accordingly
- The `solarwatch_user` DB role has access only to the `solarwatch` database
- Consider encrypting the `sunsynk_password` column at rest for production use
