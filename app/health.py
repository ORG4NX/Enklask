"""Per-source health from what each search actually returned.

Every search outcome (poller, live feed, instant search) is recorded per
source: results, zero results, blocked by an anti-bot, or another error, plus
whether the app had to re-filter because the site ignored a filter (Easy Cash
drops the price range on its category redirects). The home page turns the
recent outcomes into one readable state per source.

Kept small in memory and mirrored to a JSON file next to the database, so the
lights survive a restart instead of falling back to guesses.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import get_settings

logger = logging.getLogger(__name__)

RECENT = 8  # outcomes kept per source
MIN_EMPTY_STREAK = 3  # empty searches in a row before the light turns orange
FILTER_NOTE_HOURS = 24  # how long "filters ignored" stays on the light

OK, EMPTY, BLOCKED, ERROR = "ok", "empty", "blocked", "error"

_BLOCKED_RE = re.compile(
    r"datadome|captcha|challenge|anti.?bot|bot.?check|blocked|bloqu|forbidden|"
    r"\b40[13]\b|\b429\b|rate.?limit|cooldown|interruption",
    re.I,
)

_lock = threading.Lock()
_state: dict[str, dict] = {}
_loaded = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def classify_error(exc: BaseException | str) -> str:
    """BLOCKED for anti-bot / refusal errors, ERROR for anything else."""
    return BLOCKED if _BLOCKED_RE.search(str(exc) or type(exc).__name__) else ERROR


def record(
    source: str,
    *,
    count: int | None = None,
    error: BaseException | str | None = None,
    filters_fixed: int = 0,
) -> None:
    """Record one search outcome for ``source``. Never raises."""
    try:
        if error is not None:
            kind = classify_error(error)
            message = (str(error) or type(error).__name__)[:300]
        else:
            kind = OK if count else EMPTY
            message = ""
        now = _now().isoformat()
        with _lock:
            _load()
            entry = _state.setdefault(source, {"recent": []})
            entry["recent"] = (entry.get("recent", []) + [kind])[-RECENT:]
            entry["last_at"] = now
            entry["last_kind"] = kind
            if kind == OK:
                entry["last_results_at"] = now
            if error is not None:
                entry["last_error"] = message
                entry["last_error_at"] = now
            if filters_fixed:
                entry["filters_fixed_at"] = now
                entry["filters_fixed"] = filters_fixed
            _save()
    except Exception:  # noqa: BLE001 - health is best effort
        logger.debug("Could not record health for %s", source, exc_info=True)


def snapshot(source: str) -> dict | None:
    """The recorded state of ``source`` for display, or None if never seen.

    ``state`` is one of ok / empty / blocked / error, or None:
    - blocked/error: the latest search failed that way;
    - empty: the site answers but the last MIN_EMPTY_STREAK+ searches had
      no results;
    - ok: at least one recent search returned results;
    - None: only a few empty answers so far, not enough to judge.
    ``filters_ignored`` is set when the app had to re-filter results lately.
    """
    with _lock:
        _load()
        entry = _state.get(source)
        if not entry or not entry.get("recent"):
            return None
        entry = dict(entry)
    recent = entry["recent"]
    last = recent[-1]
    if last in (BLOCKED, ERROR):
        state = last
    elif OK in [k for k in recent if k in (OK, EMPTY)][-5:]:
        state = OK
    elif _streak(recent, EMPTY) >= MIN_EMPTY_STREAK:
        state = EMPTY
    else:
        # A couple of empty searches prove nothing (small catalogue,
        # narrow query): let the caller fall back to its own guess.
        state = None
    filters_at = _parse(entry.get("filters_fixed_at"))
    return {
        "state": state,
        "last_at": _parse(entry.get("last_at")),
        "last_results_at": _parse(entry.get("last_results_at")),
        "last_error": entry.get("last_error", ""),
        "empty_streak": _streak(recent, EMPTY),
        "filters_ignored": bool(
            filters_at and _now() - filters_at < timedelta(hours=FILTER_NOTE_HOURS)
        ),
        "filters_fixed": entry.get("filters_fixed", 0),
    }


def reset() -> None:
    """Forget everything (tests)."""
    global _loaded
    with _lock:
        _state.clear()
        _loaded = True


def _streak(recent: list[str], kind: str) -> int:
    n = 0
    for k in reversed(recent):
        if k != kind:
            break
        n += 1
    return n


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _path() -> Path:
    db_url = get_settings().database_url
    if db_url.startswith("sqlite:///"):
        return Path(db_url[len("sqlite:///"):]).parent / "source_health.json"
    return Path("data") / "source_health.json"


def _load() -> None:
    """Read the JSON mirror once (call with ``_lock`` held)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _state.update({k: v for k, v in data.items() if isinstance(v, dict)})
    except (OSError, ValueError):
        pass


def _save() -> None:
    """Write the JSON mirror (call with ``_lock`` held). Best effort."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("Could not save source health: %s", exc)
