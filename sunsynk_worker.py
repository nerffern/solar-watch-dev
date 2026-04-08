"""
SolarWatch — sunsynk_worker.py

Sunsynk Cloud API poller.
Handles auth with token caching, fetches live data per inverter,
and returns normalised reading dicts matching the solar_readings schema.
"""

import time
import base64
import hashlib
import logging
from typing import Optional

import requests
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

log = logging.getLogger(__name__)

BASE_URL  = "https://api.sunsynk.net"
CLIENT_ID = "csp-web"
SOURCE    = "sunsynk"
TOKEN_TTL = 3600   # seconds before re-login


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
        return r.json().get("data")

    def get_plants(self) -> list:
        data = self._get("v1/plants?page=1&limit=20")
        return (data or {}).get("infos", [])

    def get_plant_realtime(self, plant_id) -> Optional[dict]:
        return self._get(f"v1/plant/{plant_id}/realtime")

    def get_inverter_flow(self, sn: str) -> Optional[dict]:
        return self._get(f"v1/inverter/{sn}/flow")

    def get_inverter_realtime(self, sn: str) -> Optional[dict]:
        return self._get(f"v1/inverter/{sn}/realtime/input")


def _normalise(flow: Optional[dict], realtime, inverter_sn: str) -> dict:
    """Map Sunsynk API fields to solar_readings column names."""
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

    if flow:
        pvs = flow.get("pv", [])
        if len(pvs) > 0:
            row["pv1_power"]   = pvs[0].get("power")
            row["pv1_voltage"] = pvs[0].get("voltage")
            row["pv1_current"] = pvs[0].get("current")
        if len(pvs) > 1:
            row["pv2_power"]   = pvs[1].get("power")
            row["pv2_voltage"] = pvs[1].get("voltage")
            row["pv2_current"] = pvs[1].get("current")
        row["battery_power"] = flow.get("battPower")
        row["battery_soc"]   = flow.get("soc")
        row["grid_power"]    = flow.get("gridOrMeterPower")
        row["load_power"]    = flow.get("loadOrEpsPower")

    if realtime and isinstance(realtime, list):
        field_map = {
            "Temp":              "inverter_temp",
            "Battery Temp":      "battery_temp",
            "Battery Volt":      "battery_voltage",
            "Battery Curr":      "battery_current",
            "Grid Volt":         "grid_voltage",
            "Grid Freq":         "grid_frequency",
            "Load Volt":         "load_voltage",
            "DC Temp":           "dc_temp",
            "Daily PV":          "daily_pv_energy",
            "Total PV":          "total_pv_energy",
            "Daily Charge":      "daily_battery_charge",
            "Daily Discharge":   "daily_battery_discharge",
            "Daily Import":      "daily_grid_import",
            "Daily Export":      "daily_grid_export",
            "Daily Load":        "daily_load_energy",
        }
        for item in realtime:
            label = item.get("label") or item.get("name", "")
            val   = item.get("value")
            col   = field_map.get(label)
            if col and val is not None:
                try:
                    row[col] = float(val)
                except (ValueError, TypeError):
                    pass

    row["poll_success"]     = flow is not None
    row["poll_duration_ms"] = None
    row["source_type"]      = "sunsynk"
    return row


def poll(site: dict, client: SunsynkClient) -> list[dict]:
    """
    Poll all inverters for one Sunsynk site.
    Returns list of normalised reading dicts (one per inverter).
    """
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
            flow     = client.get_inverter_flow(sn)
            realtime = client.get_inverter_realtime(sn)
            elapsed  = int((time.monotonic() - start) * 1000)

            row = _normalise(flow, realtime, sn)
            row["poll_duration_ms"] = elapsed
            row["inverter_name"]    = f"Inverter_{i}"
            row["inverter_sn"]      = sn

            log.info(
                f"[{label}] SOC={row.get('battery_soc')}% "
                f"PV={((row.get('pv1_power') or 0) + (row.get('pv2_power') or 0)):.0f}W "
                f"Grid={row.get('grid_power')}W Load={row.get('load_power')}W "
                f"({elapsed}ms)"
            )
            results.append(row)
        except Exception as e:
            log.error(f"[{label}] Poll failed: {e}")

    return results
