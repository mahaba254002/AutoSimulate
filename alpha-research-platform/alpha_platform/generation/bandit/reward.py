"""Transparent research ranking, not a forecast of investment performance."""
import math


def reward(metrics):
    def number(key):
        value = metrics.get(key)
        if value is None:
            return 0.0
        value = float(value)
        return value if math.isfinite(value) else 0.0
    components = {
        "sharpe": math.tanh(number("sharpe") / 3),
        "fitness": 0.5 * math.tanh(number("fitness") / 2),
        "turnover": -0.25 * max(0, number("turnover")),
        "self_correlation": -0.5 * abs(number("self_correlation")),
    }
    # Uncalibrated Qdrant scores deliberately do not influence the score.
    return {"score": sum(components.values()), "components": components,
            "missing": [k for k in components if metrics.get(k) is None]}
