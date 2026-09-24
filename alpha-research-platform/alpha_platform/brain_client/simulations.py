"""
Wraps POST/GET /simulations exactly as documented: submit a simulation
(REGULAR or SUPER, single or multi), poll with Retry-After honored, and
capture the X-Ratelimit-* headers on every submit so the quota ledger
stays accurate without extra API calls.

Callers should always submit through submit_simulation(), never build
raw requests directly, so rate-limit capture and error handling stay
consistent everywhere in the codebase.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from alpha_platform.config.settings import get_settings

logger = logging.getLogger(__name__)


class BrainSimulationError(Exception):
    """Raised when a simulation submission or poll returns a client-side error."""


@dataclass
class RateLimitInfo:
    """Captured straight from response headers — mirrors quota_ledger columns."""
    limit: int | None = None
    remaining: int | None = None
    reset_seconds: int | None = None

    @classmethod
    def from_headers(cls, headers: dict) -> "RateLimitInfo":
        def _to_int(v):
            return int(v) if v is not None else None
        return cls(
            limit=_to_int(headers.get("X-Ratelimit-Limit")),
            remaining=_to_int(headers.get("X-Ratelimit-Remaining")),
            reset_seconds=_to_int(headers.get("X-Ratelimit-Reset")),
        )


@dataclass
class SimulationSubmitResult:
    """What submit_simulation() hands back — enough to poll and to log to simulation_run."""
    progress_url: str
    rate_limit: RateLimitInfo
    raw_response: dict = field(default_factory=dict)


def build_regular_simulation_payload(
    *,
    instrument_type: str,
    region: str,
    universe: str,
    delay: int,
    decay: int,
    neutralization: str,
    truncation: float,
    regular_code: str,
    pasteurization: str = "ON",
    test_period: str = "P1Y6M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    visualization: bool = False,
) -> dict[str, Any]:
    """
    Builds a REGULAR simulation payload matching the /simulations POST
    body shape from the BRAIN API doc. This is the dict that also gets
    hashed by dedupe.hashing.hash_alpha() before submission.
    """
    return {
        "type": "REGULAR",
        "settings": {
            "instrumentType": instrument_type,
            "region": region,
            "universe": universe,
            "delay": delay,
            "decay": decay,
            "neutralization": neutralization,
            "truncation": truncation,
            "pasteurization": pasteurization,
            "testPeriod": test_period,
            "unitHandling": unit_handling,
            "nanHandling": nan_handling,
            "language": language,
            "visualization": visualization,
        },
        "regular": regular_code,
    }


def submit_simulation(
    session: requests.Session,
    simulation_data: dict[str, Any] | list[dict[str, Any]],
) -> SimulationSubmitResult:
    """
    POST /simulations. Accepts either a single simulation dict (REGULAR
    or SUPER) or a list of 2-10 for a multi-simulation (requires the
    MULTI_SIMULATION permission per the doc).

    Returns the Location header (progress URL) and captured rate-limit
    headers. Does NOT poll — call poll_simulation() separately so the
    caller controls whether to block or check back later.
    """
    settings = get_settings()
    response = session.post(f"{settings.brain_api_base_url}/simulations", json=simulation_data)

    rate_limit = RateLimitInfo.from_headers(response.headers)

    if response.status_code == requests.codes.created:
        progress_url = response.headers["Location"]
        logger.info(
            "Simulation submitted. Quota remaining: %s/%s",
            rate_limit.remaining, rate_limit.limit,
        )
        return SimulationSubmitResult(progress_url=progress_url, rate_limit=rate_limit)

    if response.status_code == requests.codes.bad_request:
        raise BrainSimulationError(f"Invalid simulation request: {response.json()}")

    response.raise_for_status()
    raise BrainSimulationError(f"Unexpected response submitting simulation: {response.status_code}")


def poll_simulation_once(session: requests.Session, progress_url: str) -> dict[str, Any]:
    """
    GET the simulation progress URL once, no waiting. Returns the raw
    JSON body. Callers that want blocking behavior should use
    wait_for_simulation() instead.
    """
    response = session.get(progress_url)
    response.raise_for_status()
    return response.json()


def wait_for_simulation(
    session: requests.Session,
    progress_url: str,
    *,
    poll_interval_floor: float = 1.0,
    max_wait_seconds: float | None = None,
) -> dict[str, Any]:
    """
    Polls progress_url, honoring Retry-After exactly as the BRAIN API doc
    describes: sleep for the given number of seconds, then poll again.
    Returns the final JSON body once Retry-After is absent (simulation
    reached a terminal state: COMPLETE, WARNING, ERROR, TIMEOUT, FAIL,
    or CANCELLED).

    This BLOCKS the calling thread/process for the duration — fine for
    a worker process, not fine to call from an API request handler.
    Use max_wait_seconds as a safety valve against a hung poll loop.
    """
    elapsed = 0.0
    while True:
        response = session.get(progress_url)
        response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        if not retry_after or float(retry_after) == 0:
            return response.json()

        wait_seconds = max(float(retry_after), poll_interval_floor)
        logger.info("Simulation in progress, sleeping %.1fs", wait_seconds)
        time.sleep(wait_seconds)
        elapsed += wait_seconds

        if max_wait_seconds is not None and elapsed >= max_wait_seconds:
            raise BrainSimulationError(
                f"Simulation did not complete within {max_wait_seconds}s (progress_url={progress_url})"
            )