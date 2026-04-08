"""
SolarWatch — deye_worker.py

Stateless Deye/Solarman Modbus poller.
Receives one inverter config dict, returns a normalised reading dict or None.
Used by collector.py for all Deye sites.
"""

import time
import logging
from typing import Optional

from pysolarmanv5 import PySolarmanV5

log = logging.getLogger(__name__)

SOLARMAN_PORT   = 8899
MODBUS_SLAVE_ID = 1

REGISTERS = {
    "pv1_voltage":              (109, 0.1,  False),
    "pv1_current":              (110, 0.1,  False),
    "pv1_power":                (186, 1.0,  True),
    "pv2_voltage":              (111, 0.1,  False),
    "pv2_current":              (112, 0.1,  False),
    "pv2_power":                (187, 1.0,  True),
    "battery_voltage":          (183, 0.01, False),
    "battery_current":          (191, 0.01, True),
    "battery_power":            (190, 1.0,  True),
    "battery_soc":              (184, 1.0,  False),
    "battery_temp":             (182, 0.1,  False),
    "grid_voltage":             (150, 0.1,  False),
    "grid_frequency":           (79,  0.01, False),
    "grid_power":               (169, 1.0,  True),
    "grid_current":             (160, 0.01, True),
    "load_power":               (178, 1.0,  False),
    "load_voltage":             (154, 0.1,  False),
    "inverter_temp":            (90,  0.1,  False),
    "dc_temp":                  (91,  0.1,  False),
    "daily_pv_energy":          (108, 0.1,  False),
    "daily_battery_charge":     (70,  0.1,  False),
    "daily_battery_discharge":  (71,  0.1,  False),
    "daily_grid_import":        (76,  0.1,  False),
    "daily_grid_export":        (77,  0.1,  False),
    "daily_load_energy":        (84,  0.1,  False),
    "total_pv_energy":          (68,  0.1,  False),
    "ct_power":                 (172, 1.0,  True),
    "ct_load_power":            (167, 1.0,  True),
}

TEMP_REGISTERS = {"inverter_temp", "dc_temp", "battery_temp"}


def poll(inv: dict, site_name: str = "unknown") -> Optional[dict]:
    """
    Poll one Deye inverter.
    inv keys: name, ip, dongle_serial, inverter_sn
    Returns normalised reading dict or None on total failure.
    """
    label = f"{site_name}/{inv['name']}"
    start = time.monotonic()
    sm    = None

    try:
        sm = PySolarmanV5(
            address=inv["ip"],
            serial=inv["dongle_serial"],
            port=SOLARMAN_PORT,
            mb_slave_id=MODBUS_SLAVE_ID,
            verbose=False,
            socket_timeout=10,
        )

        row    = {}
        failed = []

        for name, (reg, scale, signed) in REGISTERS.items():
            try:
                raw = sm.read_holding_registers(register_addr=reg, quantity=1)[0]

                if signed and raw > 32767:
                    raw -= 65536

                if name in TEMP_REGISTERS:
                    if raw > 1000:
                        raw -= 1000
                    elif raw > 900:
                        raw = 0

                value = round(raw * scale, 3)

                if name == "battery_temp" and (raw == 0 or value <= -99.0):
                    value = None

                row[name] = value

            except Exception as e:
                log.warning(f"[{label}] reg={reg} ({name}): {e}")
                row[name] = None
                failed.append(name)

        elapsed_ms           = int((time.monotonic() - start) * 1000)
        row["poll_duration_ms"] = elapsed_ms
        row["poll_success"]     = len(failed) == 0
        row["source_type"]      = "deye"

        if failed:
            log.warning(f"[{label}] {len(failed)} registers failed: {failed}")

        log.info(
            f"[{label}] SOC={row.get('battery_soc')}% "
            f"PV={((row.get('pv1_power') or 0) + (row.get('pv2_power') or 0)):.0f}W "
            f"Grid={row.get('grid_power')}W Load={row.get('load_power')}W "
            f"({elapsed_ms}ms)"
        )
        return row

    except Exception as e:
        log.error(f"[{label}] Poll failed: {e}")
        return None

    finally:
        if sm:
            try:
                sm.disconnect()
            except Exception:
                pass
