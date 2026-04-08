#!/usr/bin/env python3
"""
SolarWatch — Import Selati historical data

Reads all rows from the old 'solar' DB and inserts them into the new
'solarwatch' DB, tagging them as site_name='Selati', source_type='deye'.

The old DB is never modified. Safe to re-run — uses ON CONFLICT DO NOTHING
via a unique index on (time, site_name, inverter_name) if you add one,
otherwise run once on a clean solarwatch DB.

Usage:
    pip install psycopg2-binary python-dotenv
    python3 import_selati.py

Set OLD_PG_* and NEW_PG_* env vars in .env or export them before running.
"""

import os
import sys
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("import_selati")

# ── Connection strings ────────────────────────────────────────────────────────

OLD_DSN = (
    f"host={os.getenv('OLD_PG_HOST', 'postgres-ha.hfisystems.com')} "
    f"port={os.getenv('OLD_PG_PORT', '5432')} "
    f"dbname={os.getenv('OLD_PG_DB', 'solar')} "
    f"user={os.getenv('OLD_PG_USER', 'solar_user')} "
    f"password={os.getenv('OLD_PG_PASS', '')} "
    f"connect_timeout=10"
)

NEW_DSN = (
    f"host={os.getenv('NEW_PG_HOST', 'postgres-ha.hfisystems.com')} "
    f"port={os.getenv('NEW_PG_PORT', '5432')} "
    f"dbname={os.getenv('NEW_PG_DB', 'solarwatch')} "
    f"user={os.getenv('NEW_PG_USER', 'solarwatch_user')} "
    f"password={os.getenv('NEW_PG_PASS', '')} "
    f"connect_timeout=10"
)

BATCH_SIZE = 1000   # rows per INSERT batch

COLUMNS = [
    "time", "inverter_name", "inverter_sn",
    "pv1_voltage", "pv1_current", "pv1_power",
    "pv2_voltage", "pv2_current", "pv2_power",
    "battery_voltage", "battery_current", "battery_power", "battery_soc", "battery_temp",
    "grid_voltage", "grid_frequency", "grid_power", "grid_current",
    "load_power", "load_voltage",
    "inverter_temp", "dc_temp",
    "daily_pv_energy", "total_pv_energy",
    "daily_battery_charge", "daily_battery_discharge",
    "daily_grid_import", "daily_grid_export", "daily_load_energy",
    "poll_duration_ms", "poll_success",
    "ct_power", "ct_load_power",
]

INSERT_SQL = f"""
INSERT INTO solar_readings (
    site_name, source_type, {', '.join(COLUMNS)}
) VALUES (
    'Selati', 'deye', {', '.join(['%s'] * len(COLUMNS))}
)
"""


def main():
    log.info("Connecting to old DB (solar)...")
    old_conn = psycopg2.connect(OLD_DSN)

    log.info("Connecting to new DB (solarwatch)...")
    new_conn = psycopg2.connect(NEW_DSN)

    try:
        # Count source rows
        with old_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM solar_readings")
            total = cur.fetchone()[0]
        log.info(f"Source rows to import: {total:,}")

        if total == 0:
            log.warning("Nothing to import — old DB is empty")
            return

        # Stream rows from old DB in batches
        with old_conn.cursor(name="selati_export", cursor_factory=psycopg2.extras.DictCursor) as src:
            src.itersize = BATCH_SIZE
            src.execute(f"SELECT {', '.join(COLUMNS)} FROM solar_readings ORDER BY time")

            imported = 0
            batch = []

            for row in src:
                batch.append(tuple(row[c] for c in COLUMNS))

                if len(batch) >= BATCH_SIZE:
                    with new_conn.cursor() as dst:
                        psycopg2.extras.execute_batch(dst, INSERT_SQL, batch, page_size=BATCH_SIZE)
                    new_conn.commit()
                    imported += len(batch)
                    batch = []
                    log.info(f"  {imported:,} / {total:,} rows imported...")

            # Final partial batch
            if batch:
                with new_conn.cursor() as dst:
                    psycopg2.extras.execute_batch(dst, INSERT_SQL, batch, page_size=BATCH_SIZE)
                new_conn.commit()
                imported += len(batch)

        log.info(f"Import complete — {imported:,} rows written to solarwatch")

        # Verify
        with new_conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS rows, MIN(time) AS earliest, MAX(time) AS latest
                FROM solar_readings WHERE site_name = 'Selati'
            """)
            row = cur.fetchone()
            log.info(f"Verification: {row[0]:,} Selati rows, {row[1]} → {row[2]}")

    except Exception as e:
        new_conn.rollback()
        log.error(f"Import failed: {e}")
        sys.exit(1)
    finally:
        old_conn.close()
        new_conn.close()


if __name__ == "__main__":
    main()
