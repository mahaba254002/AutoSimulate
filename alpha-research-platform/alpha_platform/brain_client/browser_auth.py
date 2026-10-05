"""Browser-driven BRAIN authentication; pending challenges stay in server memory."""
import secrets
import threading
import time
import logging
import math
from urllib.parse import urljoin, urlsplit

import requests
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.brain_client.session_cache import _load_session_cookies, _save_session_cookies

LOCK = threading.RLock()
PENDING = {}
MAX_AGE = 900
CONNECTED = False
CONNECTED_UNTIL = 0
logger = logging.getLogger(__name__)


def close_attempt(attempt):
    attempt["session"].auth = None
    attempt["session"].close()


def purge():
    for token, attempt in list(PENDING.items()):
        if attempt["expires"] <= time.monotonic():
            close_attempt(attempt)
            del PENDING[token]


def state():
    with LOCK:
        purge()
        if PENDING:
            token, attempt = next(reversed(PENDING.items()))
            return {"status": "BIOMETRIC_REQUIRED", "attempt_id": token,
                    "verification_url": attempt["url"], "expires_in_seconds": int(attempt["expires"] - time.monotonic()),
                    "retry_after": max(5, int(attempt["next_check"] - time.monotonic())),
                    "message": attempt.get("message", "Waiting for BRAIN to confirm verification.")}
    return {"status": "CONNECTED" if CONNECTED and time.monotonic() < CONNECTED_UNTIL else "SAVED" if _load_session_cookies() else "DISCONNECTED"}


def request(session, method, url):
    try:
        for _ in range(5):
            response = getattr(session, method)(url, timeout=(10, 30), allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            if not location:
                raise ValueError("BRAIN returned a redirect without a destination.")
            url = verification_url(urljoin(url, location))
            if response.status_code == 303 or (response.status_code in (301, 302) and method == "post"):
                method = "get"
        raise ValueError("BRAIN authentication returned too many redirects. Please try again.")
    except requests.Timeout:
        raise ValueError("BRAIN did not respond in time. Please try again shortly.") from None
    except requests.exceptions.SSLError:
        raise ValueError("The secure connection to BRAIN could not be verified. Check the server's certificates or network proxy.") from None
    except requests.ConnectionError as exc:
        if "10013" in str(exc):
            raise ValueError("The workspace server is blocked from accessing BRAIN by its network permissions. Restart the server with normal network access.") from None
        raise ValueError("Could not connect to BRAIN. Check the server's network connection and try again.") from None
    except requests.RequestException:
        raise ValueError("Could not reach BRAIN. Check your connection and try again.") from None


def verification_url(location):
    url = urljoin(ace.brain_api_url + "/authentication", location)
    parsed, base = urlsplit(url), urlsplit(ace.brain_api_url)
    if (parsed.scheme != "https" or parsed.netloc != base.netloc or parsed.username or
            not (parsed.path == "/authentication" or parsed.path.startswith("/authentication/"))):
        raise ValueError("BRAIN returned an unexpected verification link. Login was stopped.")
    return url


def connected(session):
    global CONNECTED, CONNECTED_UNTIL
    # A challenge POST alone is insufficient; verify an authenticated session.
    response = request(session, "get", ace.brain_api_url + "/authentication")
    logger.info("BRAIN authenticated-session check: HTTP %s", response.status_code)
    try:
        expiry = float(response.json().get("token", {}).get("expiry", 0))
    except (ValueError, TypeError, AttributeError):
        expiry = 0
    if response.status_code != 200 or not math.isfinite(expiry) or expiry <= 0:
        return None
    session.auth = None
    try:
        _save_session_cookies(session)
    except OSError:
        raise ValueError("The BRAIN session could not be saved locally. Check the workspace's file permissions.") from None
    CONNECTED = True
    CONNECTED_UNTIL = time.monotonic() + expiry
    return {"status": "CONNECTED", "expires_in_seconds": int(expiry)}


def login(email, password):
    global CONNECTED
    session = requests.Session()
    session.auth = (email, password)
    retained = False
    try:
        response = request(session, "post", ace.brain_api_url + "/authentication")
        if response.status_code in (200, 201):
            result = connected(session)
            if result:
                return result
            raise ValueError("BRAIN has not confirmed an authenticated session. Please sign in again.")
        if response.status_code == 401 and response.headers.get("WWW-Authenticate", "").lower() == "persona":
            location = response.headers.get("Location")
            if not location:
                raise ValueError("BRAIN requested biometrics without providing a verification link. Try signing in again.")
            url = verification_url(location)
            token = secrets.token_urlsafe(32)
            with LOCK:
                purge()
                # A local workspace has one active login; superseded credentials are cleared.
                for old in PENDING.values():
                    close_attempt(old)
                PENDING.clear()
                PENDING[token] = {"session": session, "url": url, "expires": time.monotonic() + MAX_AGE,
                                  "next_check": time.monotonic() + 5}
            retained = True
            CONNECTED = False
            return {"status": "BIOMETRIC_REQUIRED", "attempt_id": token,
                    "verification_url": url, "expires_in_seconds": MAX_AGE, "retry_after": 5}
        if response.status_code in (401, 403):
            raise ValueError("BRAIN could not sign you in. Check your email and password, or your account access.")
        if response.status_code == 429:
            raise ValueError("BRAIN has limited login attempts. Wait before trying again.")
        raise ValueError(f"BRAIN login failed (HTTP {response.status_code}). Please try again later.")
    finally:
        if not retained:
            session.auth = None
            session.close()


def check(attempt_id):
    with LOCK:
        purge()
        attempt = PENDING.get(attempt_id)
        if not attempt:
            raise ValueError("This verification session has expired or was replaced. Sign in again.")
        if time.monotonic() < attempt["next_check"]:
            return {"status": "BIOMETRIC_REQUIRED", "retry_after": 5}
        response = request(attempt["session"], "post", attempt["url"])
        logger.info("BRAIN biometric verification check: HTTP %s", response.status_code)
        if response.status_code == 201:
            result = connected(attempt["session"])
            if result:
                close_attempt(attempt)
                del PENDING[attempt_id]
                return result
        if response.status_code in (200, 201, 202, 204, 400, 401, 403, 429):
            try:
                delay = float(response.headers.get("Retry-After", "10"))
                delay = max(5, delay) if math.isfinite(delay) else 10
            except (TypeError, ValueError):
                delay = 5
            attempt["next_check"] = time.monotonic() + delay
            attempt["message"] = ("BRAIN has limited verification checks; waiting before checking again." if response.status_code == 429
                                  else f"BRAIN has not confirmed the login yet (HTTP {response.status_code}). Complete the verification page; this workspace will keep checking.")
            return {"status": "BIOMETRIC_REQUIRED", "retry_after": delay, "message": attempt["message"]}
        close_attempt(attempt)
        del PENDING[attempt_id]
        raise ValueError("BRAIN verification could not be completed. Start a new login.")


def cancel(attempt_id):
    with LOCK:
        attempt = PENDING.pop(attempt_id, None)
        if attempt:
            close_attempt(attempt)
    return state()
