"""
SolarWatch — sunsynk_worker.py

Sunsynk Cloud API poller.
All endpoints and field mappings confirmed from HAR capture against
inverter SN 2506303417 (Penguin site).

Endpoints used per poll:
  /flow                              → pv powers, battery power+SOC, grid+load power
  /realtime/input                    → pv1/2 voltage+current, daily/total PV energy
  /inverter/battery/{sn}/realtime    → battery voltage, current, temp, daily charge/discharge
  /inverter/grid/{sn}/realtime       → grid voltage, frequency, daily import/export
  /inverter/load/{sn}/realtime       → load power, daily load energy

Set SUNSYNK_DEBUG=1 in .env to log raw API responses.
"""

import os
import time
import base64
import hashlib
import logging
import json
from typing import Optional

import requests
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

log   = logging.getLogger(__name__)
DEBUG = os.getenv("SUNSYNK_DEBUG", "0") == "1"
_poll_count: dict = {}  # site_name → poll count for throttling

BASE_URL  = "https://api.sunsynk.net"
CLIENT_ID = "csp-web"
SOURCE    = "sunsynk"
TOKEN_TTL = 3600


class SunsynkClient:
    """Authenticated Sunsynk API client with automatic token refresh."""

    def __init__(self, username: str, password: str):
        self.username      = username
        self.password      = password
        self.token: Optional[str] = None
        self._token_expiry = 0.0
        self.session       = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0",
            "Origin":       "https://sunsynk.net",
            "Referer":      "https://sunsynk.net",
        })

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()

    @staticmethod
    def _nonce() -> int:
        return int(time.time() * 1000)

    def _get_public_key(self) -> str:
        nonce = self._nonce()
        sign  = self._md5(f"{nonce}{SOURCE}")
        r = self.session.get(
            f"{BASE_URL}/anonymous/publicKey",
            params={"nonce": nonce, "source": SOURCE, "sign": sign},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["data"]

    def _encrypt_password(self, rsa_b64: str) -> str:
        key    = RSA.import_key(base64.b64decode(rsa_b64))
        cipher = PKCS1_v1_5.new(key)
        return base64.b64encode(cipher.encrypt(self.password.encode())).decode()

    def ensure_logged_in(self) -> bool:
        if self.token and time.time() < self._token_expiry:
            return True
        log.info("Logging in to Sunsynk Cloud...")
        try:
            rsa    = self._get_public_key()
            enc_pw = self._encrypt_password(rsa)
            nonce  = self._nonce()
            sign   = self._md5(f"nonce={nonce}&source={SOURCE}{rsa[:10]}")
            r = self.session.post(
                f"{BASE_URL}/oauth/token/new",
                json={
                    "username":   self.username,
                    "password":   enc_pw,
                    "grant_type": "password",
                    "client_id":  CLIENT_ID,
                    "source":     SOURCE,
                    "nonce":      nonce,
                    "sign":       sign,
                },
                timeout=10,
            )
            data = r.json()
            if data.get("success"):
                self.token         = data["data"]["access_token"]
                self._token_expiry = time.time() + TOKEN_TTL
                log.info("Sunsynk login successful")
                return True
            log.error(f"Sunsynk login failed: {data}")
            return False
        except Exception as e:
            log.error(f"Sunsynk login exception: {e}")
            return False

    def _get(self, endpoint: str) -> Optional[dict]:
        r = self.session.get(
            f"{BASE_URL}/api/{endpoint}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json().get("data")
        if DEBUG:
            log.info(f"[DEBUG] {endpoint}\n{json.dumps(data, indent=2)}")
        return data

    def get_plants(self) -> list:
        data = self._get("v1/plants?page=1&limit=20")
        return (data or {}).get("infos", [])

    def get_plant_realtime(self, plant_id) -> Optional[dict]:
        return self._get(f"v1/plant/{plant_id}/realtime")

    def get_inverter_flow(self, sn: str) -> Optional[dict]:
        """Live power flow — pv[], battPower, soc, gridOrMeterPower, loadOrEpsPower."""
        return self._get(f"v1/inverter/{sn}/flow")

    def get_inverter_realtime_input(self, sn: str) -> Optional[dict]:
        """PV strings — pvIV[{pvNo,vpv,ipv,ppv}], etoday, etotal."""
        return self._get(f"v1/inverter/{sn}/realtime/input")

    def get_battery_realtime(self, sn: str) -> Optional[dict]:
        """Battery detail — voltage, current, temp, soc, daily charge/discharge."""
        return self._get(f"v1/inverter/battery/{sn}/realtime?sn={sn}&lan=en")

    def get_grid_realtime(self, sn: str) -> Optional[dict]:
        """Grid detail — vip[{volt,current,power}], fac, etodayFrom/To."""
        return self._get(f"v1/inverter/grid/{sn}/realtime?sn={sn}")

    def get_load_realtime(self, sn: str) -> Optional[dict]:
        """Load detail — totalPower, dailyUsed, totalUsed."""
        return self._get(f"v1/inverter/load/{sn}/realtime?sn={sn}")


    def get_inverter_temperature(self, sn: str) -> tuple[Optional[float], Optional[float]]:
        """
        Get latest inverter temperatures (AC temp and DC temp).
        Temperature is only available via the day history endpoint — not realtime.
        Returns (ac_temp, dc_temp) in °C, or (None, None) on failure.
        The Sunsynk app calls this the inverter "Detail" tab data.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def last_val(col):
            data = self._get(f"v1/inverter/{sn}/output/day?lan=en&date={today}&column={col}")
            if not data:
                return None
            infos = data.get("infos") or []
            if not infos:
                return None
            records = infos[0].get("records") or []
            if not records:
                return None
            # Last non-zero record
            for rec in reversed(records):
                val = _f(rec.get("value"))
                if val and val > 0:
                    return val
            return None

        dc_temp  = last_val("dc_temp")
        # AC/inverter temp: try 'temp' first (confirmed in Sunsynk source),
        # fall back to 'ac_temp' if empty
        ac_temp  = last_val("temp") or last_val("ac_temp")
        return dc_temp, ac_temp


def _f(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _normalise(flow, inp, battery, grid, load) -> dict:
    """
    Map confirmed Sunsynk API fields to solar_readings columns.
    All field names verified from live HAR capture.
    """
    row: dict = {k: None for k in [
        "pv1_voltage", "pv1_current", "pv1_power",
        "pv2_voltage", "pv2_current", "pv2_power",
        "battery_voltage", "battery_current", "battery_power",
        "battery_soc", "battery_temp",
        "grid_voltage", "grid_frequency", "grid_power", "grid_current",
        "load_power", "load_voltage", "inverter_temp", "dc_temp",
        "daily_pv_energy", "total_pv_energy",
        "daily_battery_charge", "daily_battery_discharge",
        "daily_grid_import", "daily_grid_export", "daily_load_energy",
        "ct_power", "ct_load_power",
    ]}

    # ── /flow ──────────────────────────────────────────────────────────────
    # {"pv":[{"power":1572},{"power":1787}], "battPower":-3331,
    #  "soc":82.0, "gridOrMeterPower":21, "loadOrEpsPower":57}
    if flow and isinstance(flow, dict):
        pvs = flow.get("pv") or []
        if len(pvs) > 0:
            row["pv1_power"] = _f(pvs[0].get("power"))
        if len(pvs) > 1:
            row["pv2_power"] = _f(pvs[1].get("power"))
        row["battery_power"] = _f(flow.get("battPower"))
        row["battery_soc"]   = _f(flow.get("soc"))
        row["grid_power"]    = _f(flow.get("gridOrMeterPower"))
        row["load_power"]    = _f(flow.get("loadOrEpsPower"))

    # ── /realtime/input ────────────────────────────────────────────────────
    # {"pvIV":[{"pvNo":1,"vpv":"251.6","ipv":"6.3","ppv":"1572.0",...},
    #          {"pvNo":2,"vpv":"265.7","ipv":"6.8","ppv":"1787.0",...}],
    #  "etoday":17.2, "etotal":1952.2}
    if inp and isinstance(inp, dict):
        for pv in (inp.get("pvIV") or []):
            no = pv.get("pvNo")
            if no == 1:
                row["pv1_voltage"] = _f(pv.get("vpv"))
                row["pv1_current"] = _f(pv.get("ipv"))
                if pv.get("ppv"):
                    row["pv1_power"] = _f(pv.get("ppv"))  # more precise than flow
            elif no == 2:
                row["pv2_voltage"] = _f(pv.get("vpv"))
                row["pv2_current"] = _f(pv.get("ipv"))
                if pv.get("ppv"):
                    row["pv2_power"] = _f(pv.get("ppv"))
        row["daily_pv_energy"] = _f(inp.get("etoday"))
        row["total_pv_energy"] = _f(inp.get("etotal"))

    # ── /inverter/battery/{sn}/realtime ───────────────────────────────────
    # {"voltage":"54.3", "current":-54.48, "temp":"23.6", "soc":"85.0",
    #  "power":-2960, "etodayChg":"10.2", "etodayDischg":"2.6",
    #  "bmsVolt":54.06, "bmsCurrent":51.0, "bmsTemp":23.6, "bmsSoc":85.0}
    if battery and isinstance(battery, dict):
        row["battery_voltage"] = _f(battery.get("voltage"))
        row["battery_current"] = _f(battery.get("current"))
        row["battery_temp"]    = _f(battery.get("temp"))
        # Use bmsSoc if soc not already set from flow (more precise BMS value)
        if row["battery_soc"] is None:
            row["battery_soc"] = _f(battery.get("soc") or battery.get("bmsSoc"))
        # battery power: use flow value (more current) but fallback to battery endpoint
        if row["battery_power"] is None:
            row["battery_power"] = _f(battery.get("power"))
        # Daily battery energy counters
        row["daily_battery_charge"]    = _f(battery.get("etodayChg"))
        row["daily_battery_discharge"] = _f(battery.get("etodayDischg"))

    # ── /inverter/grid/{sn}/realtime ──────────────────────────────────────
    # {"vip":[{"volt":"231.6","current":"1.6","power":15}],
    #  "fac":50.15, "etodayFrom":"2.1", "etodayTo":"0.0",
    #  "etotalFrom":"563.7", "etotalTo":"0.8"}
    if grid and isinstance(grid, dict):
        vip = grid.get("vip") or []
        if vip:
            row["grid_voltage"] = _f(vip[0].get("volt"))
            row["grid_current"] = _f(vip[0].get("current"))
            # Only override grid_power from flow if not already set
            if row["grid_power"] is None:
                row["grid_power"] = _f(vip[0].get("power"))
        row["grid_frequency"]   = _f(grid.get("fac"))
        row["daily_grid_import"]= _f(grid.get("etodayFrom"))  # bought from grid today
        row["daily_grid_export"]= _f(grid.get("etodayTo"))    # sold to grid today

    # ── /inverter/load/{sn}/realtime ──────────────────────────────────────
    # {"totalPower":348, "dailyUsed":11.8, "totalUsed":2194.0,
    #  "vip":[{"volt":"232.3","current":"0.0","power":348}]}
    if load and isinstance(load, dict):
        # Override load_power with more precise value from dedicated endpoint
        row["load_power"]       = _f(load.get("totalPower") or load.get("upsPowerTotal"))
        row["daily_load_energy"]= _f(load.get("dailyUsed"))
        vip = load.get("vip") or []
        if vip:
            row["load_voltage"] = _f(vip[0].get("volt"))

    row["poll_success"]     = flow is not None
    row["poll_duration_ms"] = None
    row["source_type"]      = "sunsynk"
    return row


def poll(site: dict, client: SunsynkClient) -> list[dict]:
    """Poll all inverters for one Sunsynk site."""
    site_name = site.get("site_name", "unknown")

    if not client.ensure_logged_in():
        log.error(f"[{site_name}] Not logged in — skipping")
        return []


    plant_id = site.get("sunsynk_plant_id")
    if not plant_id:
        plants = client.get_plants()
        if not plants:
            log.error(f"[{site_name}] No plants found")
            return []
        plant_id = plants[0]["id"]
        log.info(f"[{site_name}] Auto-discovered plant: {plants[0].get('name')} (ID {plant_id})")

    plant_data   = client.get_plant_realtime(plant_id) or {}
    inverter_sns = [
        inv.get("sn") or inv.get("serialNum")
        for inv in plant_data.get("inverters", [])
        if inv.get("sn") or inv.get("serialNum")
    ]

    # Fallback to manually configured SNs
    if not inverter_sns:
        inverter_sns = [
            i["inverter_sn"]
            for i in (site.get("inverters") or [])
            if i.get("inverter_sn")
        ]

    if not inverter_sns:
        log.error(f"[{site_name}] No inverter SNs found")
        return []

    results = []
    for i, sn in enumerate(inverter_sns, 1):
        start = time.monotonic()
        label = f"{site_name}/Inverter_{i}"
        try:
            flow    = client.get_inverter_flow(sn)
            inp     = client.get_inverter_realtime_input(sn)
            battery = client.get_battery_realtime(sn)
            grid    = client.get_grid_realtime(sn)
            load    = client.get_load_realtime(sn)

            # Fetch temperatures every 5 polls (~5 min) — only available via day endpoint
            _poll_count[label] = _poll_count.get(label, 0) + 1
            dc_temp = ac_temp = None
            if _poll_count[label] % 5 == 1:  # poll 1, 6, 11, ...
                dc_temp, ac_temp = client.get_inverter_temperature(sn)
                if dc_temp or ac_temp:
                    log.info(f"[{label}] Temps: DC={dc_temp}°C AC={ac_temp}°C")

            elapsed = int((time.monotonic() - start) * 1000)

            row = _normalise(flow, inp, battery, grid, load)
            if dc_temp is not None:
                row["dc_temp"]       = dc_temp
            if ac_temp is not None:
                row["inverter_temp"] = ac_temp
            row["poll_duration_ms"] = elapsed
            row["inverter_name"]    = f"Inverter_{i}"
            row["inverter_sn"]      = sn

            pv_total = (row.get("pv1_power") or 0) + (row.get("pv2_power") or 0)
            log.info(
                f"[{label}] SOC={row.get('battery_soc')}% "
                f"PV={pv_total:.0f}W "
                f"({row.get('pv1_voltage')}V/{row.get('pv2_voltage')}V) "
                f"Batt={row.get('battery_voltage')}V/{row.get('battery_current')}A "
                f"Grid={row.get('grid_power')}W@{row.get('grid_voltage')}V/{row.get('grid_frequency')}Hz "
                f"Load={row.get('load_power')}W "
                f"BattTemp={row.get('battery_temp')}°C "
                f"DailyPV={row.get('daily_pv_energy')}kWh "
                f"DailyLoad={row.get('daily_load_energy')}kWh "
                f"DailyImport={row.get('daily_grid_import')}kWh "
                f"({elapsed}ms)"
            )
            results.append(row)
        except Exception as e:
            log.error(f"[{label}] Poll failed: {e}", exc_info=True)

    return results