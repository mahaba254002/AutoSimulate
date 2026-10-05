"""Missing evidence never qualifies. Thresholds are explicit user inputs."""
import math
from pydantic import BaseModel, ConfigDict, Field


def finite_number(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def clean_json(value):
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class Criteria(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_sharpe: float = Field(default=1.25, ge=0, le=20, allow_inf_nan=False)
    min_fitness: float = Field(default=1, ge=0, le=20, allow_inf_nan=False)
    max_turnover: float = Field(default=0.7, gt=0, le=1, allow_inf_nan=False)
    max_self_correlation: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    max_production_correlation: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    required_checks: list[str] = Field(default_factory=list, max_length=50)


def evaluate(metrics, checks, criteria):
    metrics = {key: finite_number(value) for key, value in metrics.items()}
    checks = clean_json(checks)
    criteria = Criteria.model_validate(criteria)
    reasons, unverified = [], []
    bounds = [("sharpe", criteria.min_sharpe, "min"), ("fitness", criteria.min_fitness, "min"),
              ("turnover", criteria.max_turnover, "max"),
              ("self_correlation", criteria.max_self_correlation, "max"),
              ("production_correlation", criteria.max_production_correlation, "max")]
    for name, limit, direction in bounds:
        if limit is None:
            continue
        value = metrics.get(name)
        if value is None or not math.isfinite(float(value)):
            unverified.append(name)
        elif (direction == "min" and float(value) < limit) or (direction == "max" and float(value) > limit):
            reasons.append(f"{name} does not meet the campaign threshold")
    indexed = {c.get("name", c.get("test")): c.get("result") for c in checks}
    if not checks:
        unverified.append("platform checks")
    for name, result in indexed.items():
        if result == "FAIL":
            reasons.append(f"{name}: FAIL")
        elif result != "PASS":
            unverified.append(f"{name}: {result or 'missing'}")
    for name in criteria.required_checks:
        if name not in indexed:
            unverified.append(name)
    return {"qualifies": not reasons and not unverified, "reasons": reasons,
            "unverified": unverified, "metrics": metrics, "checks": checks}
