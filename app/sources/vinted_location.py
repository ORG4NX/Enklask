"""Where a Vinted seller is, for the distance filter.

The catalogue API only names the seller (``user.id``). Their public profile
(``/api/v2/users/{id}``) carries a city when they chose to show it, which in a
sample was about 6 sellers in 10. The city is a bare name, so it is geocoded:
the French communes API (geo.api.gouv.fr) first, Nominatim as a fallback for
merged communes and foreign sellers.

Everything is cached for the life of the process (a seller rarely moves, a
city never does) and profile lookups are capped per search and paused after a
429, because Vinted rate-limits profile reads after a few dozen in a row.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import httpx

from ..config import get_settings
from .vinted_api import VintedApiClient, VintedApiError

logger = logging.getLogger(__name__)

# New profile reads allowed per search; the rest stay unknown until a later
# poll (the cache fills up over a few polls, so steady state costs ~nothing).
MAX_LOOKUPS_PER_SEARCH = 25
_LOOKUP_SPACING_SECONDS = 0.5
_RATE_LIMIT_PAUSE_SECONDS = 600.0
_SELLER_TTL_SECONDS = 7 * 24 * 3600

_COMMUNES_URL = "https://geo.api.gouv.fr/communes"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "Enklask/1.0 (self-hosted listing tracker)"}
_NOMINATIM_SPACING_SECONDS = 1.1  # Nominatim usage policy: max 1 request/s

_lock = threading.Lock()
# user id -> (fetched_at, (city, country_code) or None when hidden)
_sellers: dict[str, tuple[float, tuple[str, str] | None]] = {}
# "FR|rennes" -> (lat, lon), or None when the city does not exist. Kept on
# disk next to the database: the set of cities is small and never changes.
_cities: dict[str, tuple[float, float] | None] = {}
_cities_loaded = False
# key -> monotonic time before which a failed geocode is not retried
_failed: dict[str, float] = {}
_FAILED_RETRY_SECONDS = 3600.0
_paused_until = 0.0
_last_nominatim = 0.0


def locate_sellers(
    client: VintedApiClient, user_ids: list[str]
) -> dict[str, tuple[str, float, float]]:
    """``user id -> (city, lat, lon)`` for the sellers that can be placed.

    Sellers who hide their city, cannot be geocoded, or were not looked up
    this time (budget, rate limit, error) are simply absent.
    """
    global _paused_until
    out: dict[str, tuple[str, float, float]] = {}
    budget = MAX_LOOKUPS_PER_SEARCH
    now = time.time()
    for user_id in dict.fromkeys(uid for uid in user_ids if uid):
        with _lock:
            cached = _sellers.get(user_id)
        if cached is None or now - cached[0] > _SELLER_TTL_SECONDS:
            if budget <= 0 or time.monotonic() < _paused_until:
                # A stale answer beats none: dropping a known seller's ads
                # for one poll would make them look sold.
                if cached is None:
                    continue
                place = cached[1]
                if place is not None and (coords := geocode_city(*place)) is not None:
                    out[user_id] = (place[0], *coords)
                continue
            budget -= 1
            try:
                user = client.get_user(user_id)
            except VintedApiError as exc:
                if exc.status == 429:
                    logger.info("Vinted profile reads rate-limited; pausing them")
                    _paused_until = time.monotonic() + _RATE_LIMIT_PAUSE_SECONDS
                else:
                    logger.debug("Vinted profile %s failed: %s", user_id, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - network hiccup
                logger.debug("Vinted profile %s failed: %s", user_id, exc)
                continue
            city = (user.get("city") or "").strip()
            country = (user.get("country_iso_code") or user.get("country_code") or "").upper()
            cached = (time.time(), (city, country) if city else None)
            with _lock:
                _sellers[user_id] = cached
            time.sleep(_LOOKUP_SPACING_SECONDS)
        place = cached[1]
        if place is None:
            continue
        coords = geocode_city(*place)
        if coords is not None:
            out[user_id] = (place[0], *coords)
    return out


def geocode_city(city: str, country: str) -> tuple[float, float] | None:
    """Centre of ``city``, cached on disk; a failure is retried after an hour."""
    key = f"{country}|{city.strip().lower()}"
    with _lock:
        _load_city_cache()
        if key in _cities:
            return _cities[key]
        if time.monotonic() < _failed.get(key, 0.0):
            return None
    coords: tuple[float, float] | None = None
    failed = False
    if country in ("FR", ""):
        try:
            coords = _communes(city)
        except Exception as exc:  # noqa: BLE001 - slow/flaky: fall back
            logger.debug("Communes API failed for %r: %s", city, exc)
    if coords is None:
        try:
            coords = _nominatim(city, country)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Nominatim failed for %r: %s", city, exc)
            failed = True
    with _lock:
        if failed:
            _failed[key] = time.monotonic() + _FAILED_RETRY_SECONDS
        else:
            _cities[key] = coords
            _save_city_cache()
    return coords


def _cache_path() -> Path:
    db_url = get_settings().database_url
    if db_url.startswith("sqlite:///"):
        return Path(db_url[len("sqlite:///"):]).parent / "vinted_cities.json"
    return Path("data") / "vinted_cities.json"


def _load_city_cache() -> None:
    """Fill ``_cities`` from disk once (call with ``_lock`` held)."""
    global _cities_loaded
    if _cities_loaded:
        return
    _cities_loaded = True
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        for key, value in data.items():
            _cities.setdefault(key, tuple(value) if value else None)
    except (OSError, ValueError, TypeError):
        pass


def _save_city_cache() -> None:
    """Write ``_cities`` to disk (call with ``_lock`` held). Best effort."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cities, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("Could not save the city cache: %s", exc)


def _communes(city: str) -> tuple[float, float] | None:
    resp = httpx.get(
        _COMMUNES_URL,
        params={"nom": city.replace("’", "'"), "fields": "centre", "boost": "population", "limit": 1},
        headers=_HEADERS,
        timeout=3.0,
    )
    resp.raise_for_status()
    found = resp.json()
    if not found:
        return None
    lon, lat = found[0]["centre"]["coordinates"]
    return float(lat), float(lon)


def _nominatim(city: str, country: str) -> tuple[float, float] | None:
    global _last_nominatim
    with _lock:
        wait = _last_nominatim + _NOMINATIM_SPACING_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_nominatim = time.monotonic()
    params = {"city": city, "format": "jsonv2", "limit": 1}
    if country:
        params["countrycodes"] = country.lower()
    resp = httpx.get(_NOMINATIM_URL, params=params, headers=_HEADERS, timeout=10.0)
    resp.raise_for_status()
    found = resp.json()
    if not found:
        return None
    return float(found[0]["lat"]), float(found[0]["lon"])
