"""Persistent browser fallback for AliExpress's anti-bot page.

The adapter fetches search pages with curl_cffi. When AliExpress answers with
its "punish" page (a slider captcha) instead, the same search page is loaded
in a persistent Camoufox browser, whose real JavaScript and cookies usually
get the normal page. The captcha itself is never solved: if the browser gets
it too, the fallback gives up and the adapter pauses the source.

Camoufox runs on one dedicated thread (its sync Playwright objects are
thread-affine); the profile lives in ``data/`` so the session cookies survive
restarts. Same structure as the Vinted and Leboncoin browser fallbacks.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from .browser_guard import (
    LAUNCH_LOCK,
    LAUNCH_LOCK_TIMEOUT_SECONDS,
    LOW_WRITE_PREFS,
    LaunchBackoff,
    child_pids,
    kill_tree,
)

try:
    from camoufox.pkgman import camoufox_path, launch_path
    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    HAVE_CAMOUFOX = True
except ImportError:  # pragma: no cover - optional fallback for local installs
    PlaywrightError = RuntimeError
    PlaywrightTimeout = TimeoutError
    Camoufox = None
    HAVE_CAMOUFOX = False

logger = logging.getLogger(__name__)

ITEMS_MARKER = '"itemList":{"content":'
_PUNISH_MARKERS = ("punish", "x5sec", "captcha")
# Above a navigation plus warm-up (~2 min); past it the browser is hung.
_CALL_TIMEOUT_SECONDS = 180.0


class AliExpressBrowserError(RuntimeError):
    """The browser could not obtain a usable AliExpress search page."""


class AliExpressBrowserBroker:
    """Serialize all browser work onto one persistent worker thread."""

    def __init__(self, base_url: str, profile_dir: str | Path) -> None:
        self._base = base_url.rstrip("/")
        self._profile_dir = Path(profile_dir)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aliexpress-browser"
        )
        self._camoufox = None
        self._context = None
        self._page = None
        self._warmed = False
        self._closed = False
        self._owned_pids: set[int] = set()
        self._launch_baseline: set[int] | None = None
        self._backoff = LaunchBackoff()

    def fetch(self, url: str) -> str:
        """Rendered HTML of ``url``; safe to call from any application thread."""
        if self._closed:
            raise AliExpressBrowserError("AliExpress browser broker is closed")
        future = self._executor.submit(self._fetch_on_browser_thread, url)
        try:
            return future.result(timeout=_CALL_TIMEOUT_SECONDS)
        except FutureTimeout:
            future.cancel()
            kill_tree(self._browser_pids())
            raise AliExpressBrowserError(
                f"AliExpress browser hung for {_CALL_TIMEOUT_SECONDS:.0f}s; killed it"
            ) from None

    def _browser_pids(self) -> set[int]:
        pids = set(self._owned_pids)
        baseline = self._launch_baseline
        if baseline is not None:
            pids |= child_pids() - baseline
        return pids

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._reset).result(timeout=15)
        except Exception:  # noqa: BLE001 - interpreter/application shutdown
            logger.debug("Could not close AliExpress browser cleanly", exc_info=True)
            kill_tree(self._browser_pids())
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _fetch_on_browser_thread(self, url: str) -> str:
        try:
            return self._navigate(url)
        except AliExpressBrowserError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as exc:
            logger.warning("AliExpress browser failed; restarting once: %s", exc)
            self._reset()
            try:
                return self._navigate(url)
            except (PlaywrightError, PlaywrightTimeout) as retry_exc:
                raise AliExpressBrowserError(
                    f"AliExpress browser failed after restart: {retry_exc}"
                ) from retry_exc

    def _ensure_browser(self) -> None:
        if self._context is not None and self._page is not None:
            return
        if not HAVE_CAMOUFOX or Camoufox is None:
            raise AliExpressBrowserError(
                "Camoufox is unavailable; install project requirements"
            )
        if self._camoufox is not None:
            self._reset()
        remaining = self._backoff.remaining()
        if remaining:
            raise AliExpressBrowserError(
                f"AliExpress browser relaunch paused for {remaining:.0f}s after a failed launch"
            )
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "persistent_context": True,
            "user_data_dir": str(self._profile_dir.resolve()),
            # Linux uses a real headed browser in a private Xvfb display.
            "headless": "virtual" if os.name != "nt" else False,
            "locale": "fr-FR",
            "firefox_user_prefs": dict(LOW_WRITE_PREFS),
            # uBlock Origin rewrites its IndexedDB filter cache non-stop.
            "exclude_addons": [DefaultAddons.UBO],
        }
        if os.name == "nt":
            binary = Path(launch_path(camoufox_path(False))).resolve()
            options["executable_path"] = str(binary)
        if not LAUNCH_LOCK.acquire(timeout=LAUNCH_LOCK_TIMEOUT_SECONDS):
            raise AliExpressBrowserError("Another browser launch is stuck")
        try:
            self._launch_baseline = child_pids()
            try:
                self._camoufox = Camoufox(**options)
                self._context = self._camoufox.__enter__()
                self._page = self._context.pages[0] if self._context.pages else None
                if self._page is None:
                    self._page = self._context.new_page()
                self._page.set_default_navigation_timeout(60_000)
                self._page.set_default_timeout(30_000)
            except Exception:
                self._owned_pids = child_pids() - self._launch_baseline
                self._reset()
                delay = self._backoff.failed()
                logger.warning("AliExpress browser launch failed; next try in %.0fs", delay)
                raise
            self._owned_pids = child_pids() - self._launch_baseline
        finally:
            self._launch_baseline = None
            LAUNCH_LOCK.release()
        self._backoff.succeeded()
        self._warmed = False

    def _warm_up(self) -> None:
        # The home page sets the session cookies the search page checks.
        if self._warmed:
            return
        self._page.goto(self._base + "/", wait_until="domcontentloaded")
        self._page.wait_for_timeout(2_000)
        self._warmed = True

    def _navigate(self, url: str) -> str:
        self._ensure_browser()
        self._warm_up()
        self._page.goto(url, wait_until="domcontentloaded")
        # The product list is inline JSON; the punish page swaps in a script
        # redirect instead. Wait for one or the other.
        try:
            self._page.wait_for_function(
                "() => { const h = document.documentElement.innerHTML;"
                " return h.includes('\"itemList\"') || h.includes('punish') }",
                timeout=30_000,
            )
        except PlaywrightTimeout:
            pass
        html = self._page.content()
        if ITEMS_MARKER in html:
            return html
        lowered = html.lower()
        if any(marker in lowered for marker in _PUNISH_MARKERS):
            # Next time, start from a fresh warm-up.
            self._warmed = False
            raise AliExpressBrowserError("AliExpress captcha in the browser too")
        raise AliExpressBrowserError("AliExpress page without a product list")

    def _reset(self) -> None:
        if self._camoufox is not None:
            try:
                self._camoufox.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("AliExpress browser context close failed", exc_info=True)
        kill_tree(self._owned_pids)
        self._owned_pids = set()
        self._camoufox = self._context = self._page = None
        self._warmed = False


_broker: AliExpressBrowserBroker | None = None
_broker_lock = threading.Lock()


def fetch_aliexpress_html(base_url: str, profile_dir: str | Path, url: str) -> str:
    """Fetch ``url`` through the process-wide persistent AliExpress browser."""
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = AliExpressBrowserBroker(base_url, profile_dir)
        broker = _broker
    return broker.fetch(url)


def close_aliexpress_browser() -> None:
    global _broker
    with _broker_lock:
        broker, _broker = _broker, None
    if broker is not None:
        broker.close()


atexit.register(close_aliexpress_browser)
