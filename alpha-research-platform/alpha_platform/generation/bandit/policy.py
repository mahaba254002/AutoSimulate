"""UCB allocation across mutation families from completed research runs."""
import math


def choose_arm(history, arms, *, exploration=1.0):
    if not arms:
        raise ValueError("At least one arm is required")
    for arm in arms:
        if not history.get(arm):
            return arm
    total = sum(len(history.get(arm, [])) for arm in arms)
    return max(arms, key=lambda arm: sum(history[arm]) / len(history[arm]) +
               exploration * math.sqrt(2 * math.log(total) / len(history[arm])))
