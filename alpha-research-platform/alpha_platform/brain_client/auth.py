"""
Wraps POST/GET/DELETE /authentication exactly as documented, including
the biometrics (persona) 401 flow. Credentials are read from Settings,
never hardcoded, and the authenticated requests.Session is the single
object every other brain_client module reuses.
"""
import json
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests

from alpha_platform.config.settings import get_settings

logger = logging.getLogger(__name__)


class BrainAuthError(Exception):
    """Raised when authentication fails for a reason the caller must handle."""


class BrainBiometricsRequired(BrainAuthError):
    """
    Raised when the account has biometrics sign-in enabled. The caller
    must open `verification_url` in a browser, complete verification,
    then call `session.post(verification_url)` to finish.
    """
    def __init__(self, verification_url: str):
        self.verification_url = verification_url
        super().__init__(f"Biometrics verification required: {verification_url}")


def _load_credentials() -> tuple[str, str]:
    """
    Credentials come from Settings (.env: BRAIN_EMAIL / BRAIN_PASSWORD).
    Falls back to ~/.brain_credentials (matches the JSON format from the
    BRAIN API doc: ["<email>","<password>"]) if .env values are empty,
    so existing setups using that file still work without duplicating it.
    """
    settings = get_settings()
    if settings.brain_email and settings.brain_password:
        return settings.brain_email, settings.brain_password

    cred_path = Path.home() / ".brain_credentials"
    if cred_path.exists():
        with open(cred_path, "r") as f:
            email, password = json.load(f)
        return email, password

    raise BrainAuthError(
        "No BRAIN credentials found. Set BRAIN_EMAIL/BRAIN_PASSWORD in .env "
        "or create ~/.brain_credentials as documented."
    )


def authenticate(recaptcha: str | None = None, expiry: int | None = None) -> requests.Session:
    """
    POST /authentication with basic auth, per the BRAIN API doc.
    Returns an authenticated requests.Session (JWT cookie cached on it)
    that every other brain_client call should reuse.

    Raises BrainBiometricsRequired if the account needs biometric
    verification — the caller completes that flow, then calls
    complete_biometrics_auth(session, verification_url).
    """
    settings = get_settings()
    email, password = _load_credentials()

    session = requests.Session()
    session.auth = (email, password)

    body = {}
    if recaptcha is not None:
        body["recaptcha"] = recaptcha
    if expiry is not None:
        body["expiry"] = expiry

    response = session.post(f"{settings.brain_api_base_url}/authentication", json=body or None)

    if response.status_code == requests.codes.created:
        logger.info("BRAIN authentication succeeded.")
        return session

    if response.status_code == requests.codes.unauthorized:
        www_auth = response.headers.get("WWW-Authenticate", "")
        if www_auth == "persona":
            location = response.headers.get("Location", "")
            verification_url = urljoin(response.url, location)
            raise BrainBiometricsRequired(verification_url)

        detail = response.json().get("detail", "unknown")
        if "recaptcha" in response.json():
            raise BrainAuthError(f"Authentication failed ({detail}); reCAPTCHA required.")
        raise BrainAuthError(f"Authentication failed: {detail}")

    response.raise_for_status()
    raise BrainAuthError(f"Unexpected response during authentication: {response.status_code}")


def complete_biometrics_auth(session: requests.Session, verification_url: str) -> requests.Session:
    """
    Call after the user has completed biometric verification in a browser
    at verification_url (per the BRAIN API doc's biometrics flow).
    """
    response = session.post(verification_url)
    response.raise_for_status()
    logger.info("Biometrics verification completed.")
    return session


def check_authentication(session: requests.Session) -> dict | None:
    """
    GET /authentication — returns the current auth state, or None if
    not currently authenticated (204 No Content per the doc).
    """
    settings = get_settings()
    response = session.get(f"{settings.brain_api_base_url}/authentication")

    if response.status_code == requests.codes.no_content:
        return None
    response.raise_for_status()
    return response.json()


def logout(session: requests.Session) -> None:
    """DELETE /authentication — invalidates the current JWT."""
    settings = get_settings()
    response = session.delete(f"{settings.brain_api_base_url}/authentication")
    response.raise_for_status()
    logger.info("BRAIN session logged out.")