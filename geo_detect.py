"""
Auto-detect server location and timezone via IP geolocation.

Used at startup when USER_LOCATION / USER_TIMEZONE are not set in .env.
Makes a single HTTP request to https://ipapi.co/json/ (free, no key needed,
up to 1000 req/day).  The result is cached for the process lifetime.

Falls back gracefully to empty location / UTC if the request fails.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Tuple

logger = logging.getLogger(__name__)

_GEO_URL = "https://ipapi.co/json/"
_TIMEOUT  = 5  # seconds

_cached: Tuple[str, str] | None = None   # (location, timezone)


def detect() -> Tuple[str, str]:
    """
    Return (location_str, timezone_str) for the server's public IP.

    Results are cached after the first successful call.
    On failure returns ("", "UTC").
    """
    global _cached
    if _cached is not None:
        return _cached

    try:
        req = urllib.request.Request(
            _GEO_URL,
            headers={"User-Agent": "alice-claude-skill/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())

        city     = data.get("city", "")
        region   = data.get("region", "")
        country  = data.get("country_name", "")
        timezone = data.get("timezone", "UTC")

        parts = [p for p in [city, region, country] if p]
        location = ", ".join(parts) if parts else ""

        logger.info("Auto-detected location=%r timezone=%r", location, timezone)
        _cached = (location, timezone)
        return _cached

    except Exception as exc:
        logger.warning("Geo-detection failed (%s), using defaults", exc)
        _cached = ("", "UTC")
        return _cached
