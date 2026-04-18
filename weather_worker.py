"""
SolarWatch — weather_worker.py

Fetches current weather and daily sunrise/sunset for one site
using the Open-Meteo API (free, no API key, excellent SA coverage).

Called by collector.py every WEATHER_INTERVAL seconds (default 900 = 15 min).
Returns a normalised dict ready to INSERT into weather_readings, or None on failure.

API docs: https://open-meteo.com/en/docs
WMO weather codes: https://open-meteo.com/en/docs#weathervariables
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# WMO Weather Interpretation Codes → (emoji, short description)
# https://open-meteo.com/en/docs#weathervariables
WMO_CODES = {
    0:  ("☀️",  "Clear sky"),
    1:  ("🌤️", "Mainly clear"),
    2:  ("⛅",  "Partly cloudy"),
    3:  ("☁️",  "Overcast"),
    45: ("🌫️", "Foggy"),
    48: ("🌫️", "Icy fog"),
    51: ("🌦️", "Light drizzle"),
    53: ("🌦️", "Moderate drizzle"),
    55: ("🌧️", "Dense drizzle"),
    61: ("🌧️", "Slight rain"),
    63: ("🌧️", "Moderate rain"),
    65: ("🌧️", "Heavy rain"),
    71: ("🌨️", "Slight snow"),
    73: ("🌨️", "Moderate snow"),
    75: ("❄️",  "Heavy snow"),
    80: ("🌦️", "Slight showers"),
    81: ("🌧️", "Moderate showers"),
    82: ("⛈️",  "Violent showers"),
    95: ("⛈️",  "Thunderstorm"),
    96: ("⛈️",  "Thunderstorm w/ hail"),
    99: ("⛈️",  "Thunderstorm w/ heavy hail"),
}


def wmo_label(code: Optional[int]) -> tuple[str, str]:
    """Return (emoji, description) for a WMO weather code."""
    if code is None:
        return ("❓", "Unknown")
    return WMO_CODES.get(code, ("🌡️", f"Code {code}"))


def fetch(site_name: str, latitude: float, longitude: float) -> Optional[dict]:
    """
    Fetch current weather + daily sunrise/sunset for a site.

    Returns a dict matching weather_readings columns, or None on failure.
    All timestamps are timezone-aware UTC.
    """
    label = f"weather/{site_name}"
    try:
        params = {
            "latitude":  latitude,
            "longitude": longitude,
            "current": ",".join([
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation",
                "weather_code",
                "cloud_cover",
                "wind_speed_10m",
                "wind_direction_10m",
                "shortwave_radiation",
                "uv_index",
                "is_day",
            ]),
            "daily": "sunrise,sunset",
            "timezone": "Africa/Johannesburg",
            "forecast_days": 1,
        }

        r = requests.get(BASE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        cur  = data.get("current", {})
        daily = data.get("daily", {})

        def _f(key) -> Optional[float]:
            v = cur.get(key)
            return float(v) if v is not None else None

        def _i(key) -> Optional[int]:
            v = cur.get(key)
            return int(v) if v is not None else None

        def _ts(iso: Optional[str]) -> Optional[datetime]:
            if not iso:
                return None
            try:
                # Open-Meteo returns "YYYY-MM-DDTHH:MM" without tz for daily
                # We treat these as Africa/Johannesburg local time → UTC
                from zoneinfo import ZoneInfo
                tz_jnb = ZoneInfo("Africa/Johannesburg")
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz_jnb)
                return dt.astimezone(timezone.utc)
            except Exception:
                return None

        sunrise_list = daily.get("sunrise") or []
        sunset_list  = daily.get("sunset")  or []
        sunrise = _ts(sunrise_list[0] if sunrise_list else None)
        sunset  = _ts(sunset_list[0]  if sunset_list  else None)

        code = _i("weather_code")
        emoji, desc = wmo_label(code)

        row = {
            "time":          datetime.now(timezone.utc),
            "site_name":     site_name,
            "temp_c":        _f("temperature_2m"),
            "feels_like_c":  _f("apparent_temperature"),
            "cloud_cover":   _i("cloud_cover"),
            "precipitation": _f("precipitation"),
            "wind_speed":    _f("wind_speed_10m"),
            "wind_direction":_i("wind_direction_10m"),
            "humidity":      _i("relative_humidity_2m"),
            "weather_code":  code,
            "uv_index":      _f("uv_index"),
            "sunrise":       sunrise,
            "sunset":        sunset,
            "solar_rad":     _f("shortwave_radiation"),
            "is_day":        bool(_i("is_day")),
            # Derived — handy for logging and the web UI
            "_emoji":        emoji,
            "_description":  desc,
        }

        log.info(
            f"[{label}] {emoji} {desc} | "
            f"{row['temp_c']}°C feels {row['feels_like_c']}°C | "
            f"Cloud {row['cloud_cover']}% | "
            f"Rain {row['precipitation']}mm | "
            f"Wind {row['wind_speed']}km/h | "
            f"UV {row['uv_index']} | "
            f"Rad {row['solar_rad']}W/m²"
        )
        return row

    except Exception as e:
        log.error(f"[{label}] Weather fetch failed: {e}")
        return None
