"""
Persists the authenticated ace_lib session's cookies (JWT) to disk
between separate script runs, so you don't have to re-authenticate --
and potentially re-do biometric verification -- every single time you
run a script. Biometrics in particular may be rate-limited by BRAIN,
so avoiding unnecessary re-auth matters beyond just convenience.

This does NOT change ace_lib.py at all. It wraps ace.start_session()
with a cache check first.
"""
import json
import logging
from pathlib import Path

import requests

from alpha_platform.brain_client.vendor import ace_lib as ace

logger = logging.getLogger(__name__)

_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "brain_session_cookies.json"
_LEGACY_CACHE_PATH = Path.home() / "secrets" / "brain_session_cookies.json"


def _save_session_cookies(session: requests.Session) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cookie_dict = requests.utils.dict_from_cookiejar(session.cookies)
    temporary = _CACHE_PATH.with_suffix(".tmp")
    with open(temporary, "w") as f:
        json.dump(cookie_dict, f)
    temporary.replace(_CACHE_PATH)
    logger.info("Saved BRAIN session cookies to %s", _CACHE_PATH)


def _load_session_cookies() -> dict | None:
    cache_path = _CACHE_PATH if _CACHE_PATH.exists() else _LEGACY_CACHE_PATH
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_authenticated_session() -> "ace.SingleSession":
    """
    Tries to restore a cached session first (no network call, no
    biometrics). Only falls back to a full ace.start_session() --
    which may trigger biometric verification -- if no cache exists
    or the restored session turns out to be expired/invalid.

    Always call this instead of ace.start_session() directly in
    scripts, so cached sessions get reused automatically.
    """
    session = ace.SingleSession()
    cached_cookies = _load_session_cookies()

    if cached_cookies:
        session.cookies.update(cached_cookies)
        auth_state = ace.check_session_timeout(session)
        if auth_state and auth_state > 0:
            logger.info("Restored cached BRAIN session, %ss until expiry -- no re-auth needed.", auth_state)
            return session
        logger.info("Cached session expired or invalid, re-authenticating...")

    logger.info("No usable cached session -- running full ace.start_session() (may require biometrics).")
    session = ace.start_session()
    _save_session_cookies(session)
    return session


def invalidate_cache() -> None:
    """Call if you know the cached session is bad and want to force a fresh login next time."""
    if _CACHE_PATH.exists():
        _CACHE_PATH.unlink()
        logger.info("Cleared cached BRAIN session.")


def get_cached_session():
    """Dashboard auth never opens an interactive biometric prompt in a worker."""
    session = ace.SingleSession()
    cookies = _load_session_cookies()
    if not cookies:
        raise ValueError("Connect BRAIN first using the login form in Sync with BRAIN.")
    session.cookies.update(cookies)
    response = session.get(ace.brain_api_url + "/authentication", timeout=(10, 30))
    if response.status_code != 200:
        raise ValueError("BRAIN session expired. Sign in again in Sync with BRAIN.")
    return session
