from __future__ import annotations

import math

from ..config import ACTION_COST, MAX_ACTION_TOKENS_PER_ROUND


def available_action_tokens(endowment: int, contribution: int) -> int:
    """Return tokens that may be spent on actions before round income is paid."""
    pocket_tokens = max(0, int(endowment) - int(contribution))
    return min(MAX_ACTION_TOKENS_PER_ROUND, pocket_tokens)


def action_cost_for_points(points: int) -> float:
    return ACTION_COST * int(points)


def max_affordable_action_points(endowment: int, contribution: int) -> int:
    if ACTION_COST <= 0:
        return 0
    return math.floor(available_action_tokens(endowment, contribution) / ACTION_COST)
