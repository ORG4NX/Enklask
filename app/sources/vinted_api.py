"""Fast Vinted catalogue transport: the JSON API the website itself calls.

Vinted's web app no longer reads ``/api/v2/catalog/items``; it asks
``api.<domain>/svc-catalogue/items``. The approach follows Vintrack
(github.com/JakobAIOdev/Vintrack-Vinted-Monitor, MIT):

1. Load a light marketplace page (``/help``, falling back to ``/``) with a
   Chrome TLS fingerprint to receive the anonymous cookie session.
2. Send those cookies to the API host explicitly. Vinted sets them host-only on
   ``www``, so a standards-compliant jar would not forward them and the API
   answers 403.
3. Pin the marketplace with ``Locale`` and ``X-Anon-Id``; without them the API
   falls back to the exit IP's country (foreign feed, foreign currency).

A 401/403 discards the session and retries once with a fresh one. This is a
plain HTTP request (~100 ms), cheap enough for the live feed to run every
minute, where the Camoufox page scrape takes several seconds per search.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# A session older than this is renewed before use; Vinted's anonymous tokens
# are short-lived and a stale one only costs a 401 and a retry.
_SESSION_MAX_AGE_SECONDS = 30 * 60
_TIMEOUT_SECONDS = 15
_PER_PAGE = 20

_ACCEPT_LANGUAGE = {
    "vinted.fr": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "vinted.be": "fr-BE,fr;q=0.9,nl-BE;q=0.8,nl;q=0.7,en;q=0.6",
    "vinted.lu": "fr-LU,fr;q=0.9,de;q=0.8,en;q=0.7",
    "vinted.de": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "vinted.es": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "vinted.it": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "vinted.co.uk": "en-GB,en-US;q=0.9,en;q=0.8",
}


class VintedApiError(RuntimeError):
    """The catalogue API could not be reached or answered unexpectedly."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def accept_language(domain: str) -> str:
    for suffix, value in _ACCEPT_LANGUAGE.items():
        if domain.endswith(suffix):
            return value
    return _ACCEPT_LANGUAGE["vinted.fr"]


def catalog_api_host(domain: str) -> str:
    """``www.vinted.fr`` -> ``api.vinted.fr`` (the US site keeps its www)."""
    if domain == "www.vinted.com":
        return "api.www.vinted.com"
    if domain.startswith("www."):
        return "api." + domain[len("www."):]
    return domain


class VintedApiClient:
    """One anonymous Vinted session, shared by the poller and the live feed."""

    def __init__(self, base_url: str, session_factory=None) -> None:
        self.domain = urlparse(base_url).netloc or "www.vinted.fr"
        self._session_factory = session_factory or _new_session
        self._lock = threading.Lock()
        self._session = None
        self._cookies: dict[str, str] = {}
        self._warmed_at = 0.0

    def search(self, params: dict) -> list[dict]:
        """Newest catalogue items for ``params`` (search_text, order, ...)."""
        with self._lock:
            self._ensure_session()
            status, data = self._get_items(params)
            if status in (401, 403):
                logger.info("Vinted API %s; renewing the anonymous session", status)
                self._reset()
                self._ensure_session()
                status, data = self._get_items(params)
            if status != 200:
                if status in (401, 403, 429):
                    # Do not keep a session Vinted has just refused.
                    self._reset()
                raise VintedApiError(f"Vinted catalogue API answered {status}", status)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise VintedApiError("Vinted catalogue API answered without an items list", 200)
        return [item for item in items if isinstance(item, dict)]

    # -- session -----------------------------------------------------------

    def _reset(self) -> None:
        self._session = None
        self._cookies = {}
        self._warmed_at = 0.0

    def _ensure_session(self) -> None:
        fresh = time.monotonic() - self._warmed_at < _SESSION_MAX_AGE_SECONDS
        if self._session is not None and self._cookies and fresh:
            return
        self._reset()
        session = self._session_factory()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": accept_language(self.domain),
            "Upgrade-Insecure-Requests": "1",
        }
        status = None
        for path in ("/help", "/"):
            resp = session.get(
                f"https://{self.domain}{path}", headers=headers, timeout=_TIMEOUT_SECONDS
            )
            status = resp.status_code
            # Only an unsupported route is worth a second page; a refusal would
            # just be refused again.
            if status not in (404, 410):
                break
        if status != 200:
            raise VintedApiError(f"Vinted warm-up answered {status}", status)
        cookies = _cookie_dict(session)
        if not cookies:
            raise VintedApiError("Vinted warm-up set no session cookie", status)
        self._session = session
        self._cookies = cookies
        self._warmed_at = time.monotonic()

    def get_user(self, user_id: int | str) -> dict:
        """Public profile of one member (city, country...) from ``/api/v2/users``.

        Raises ``VintedApiError`` on any non-200 (429 = rate limited: callers
        should stop asking for a while).
        """
        with self._lock:
            self._ensure_session()
            resp = self._session.get(
                f"https://{self.domain}/api/v2/users/{user_id}",
                headers=self._api_headers(),
                timeout=_TIMEOUT_SECONDS,
            )
            if resp.status_code in (401, 403):
                self._reset()
        if resp.status_code != 200:
            raise VintedApiError(f"Vinted user API answered {resp.status_code}", resp.status_code)
        try:
            user = resp.json().get("user")
        except (ValueError, AttributeError) as exc:
            raise VintedApiError("Vinted user API answered non-JSON", 200) from exc
        if not isinstance(user, dict):
            raise VintedApiError("Vinted user API answered without a user", 200)
        return user

    def _api_headers(self) -> dict[str, str]:
        lang = accept_language(self.domain)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": lang,
            "Origin": f"https://{self.domain}",
            "Referer": f"https://{self.domain}/",
            "Locale": lang.split(",", 1)[0],
            "Platform": "web",
            "X-Next-App": "marketplace-web",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items()),
        }
        anon_id = self._cookies.get("anon_id")
        if anon_id:
            headers["X-Anon-Id"] = anon_id
        return headers

    def _get_items(self, params: dict) -> tuple[int, dict | None]:
        query = {"per_page": _PER_PAGE, "currency": "EUR", **params}
        resp = self._session.get(
            f"https://{catalog_api_host(self.domain)}/svc-catalogue/items",
            params=query,
            headers=self._api_headers(),
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError as exc:
            raise VintedApiError("Vinted catalogue API answered non-JSON", 200) from exc


def _new_session():
    from curl_cffi import requests

    return requests.Session(impersonate="chrome")


def _cookie_dict(session) -> dict[str, str]:
    jar = getattr(session.cookies, "jar", session.cookies)
    out: dict[str, str] = {}
    for cookie in jar:
        name = getattr(cookie, "name", None)
        value = getattr(cookie, "value", None)
        if name and value is not None:
            out[name] = value
    return out


_clients: dict[str, VintedApiClient] = {}
_clients_lock = threading.Lock()


def get_client(base_url: str) -> VintedApiClient:
    """The process-wide client for ``base_url`` (one cookie session)."""
    with _clients_lock:
        client = _clients.get(base_url)
        if client is None:
            client = _clients[base_url] = VintedApiClient(base_url)
        return client


def reset_clients() -> None:
    """Forget every session (tests)."""
    with _clients_lock:
        _clients.clear()
