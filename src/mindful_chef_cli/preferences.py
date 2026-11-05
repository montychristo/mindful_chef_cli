"""Portable household preferences. No account data or household defaults live here."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any


def default_preferences() -> dict[str, Any]:
    """Return a fresh, neutral preferences object suitable for a private config file."""
    return {
        "likes": [],
        "dislikes": [],
        "excluded_ingredients": [],
        "allergens": [],
        "protein_targets": {},
        "max_cook_time_minutes": None,
        "prefer_reheatable": False,
        "require_reheatable": False,
        "max_finishing_effort": None,
    }


def validate_preferences(preferences: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and copy known settings, rejecting typos rather than ignoring them.

    Protein targets are desired recipe counts, not nutritional requirements. The
    finishing-effort scale is ``low``, ``medium``, ``high``. Unknown recipe facts
    cannot satisfy a hard requirement.
    """
    if preferences is None:
        return default_preferences()
    if not isinstance(preferences, dict):
        raise ValueError("Preferences must be a JSON object")
    result = default_preferences()
    unknown = set(preferences) - set(result)
    if unknown:
        raise ValueError("Unknown preference setting: " + ", ".join(sorted(unknown)))
    result.update(deepcopy(preferences))
    for key in ("likes", "dislikes", "excluded_ingredients", "allergens"):
        value = result[key]
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError(f"{key} must be a list of nonempty strings")
        result[key] = list(dict.fromkeys(x.strip().casefold() for x in value))
    targets = result["protein_targets"]
    if not isinstance(targets, dict) or any(
        not isinstance(key, str) or not key.strip()
        or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12
        for key, value in targets.items()
    ):
        raise ValueError("protein_targets must map protein names to counts from 0 to 12")
    result["protein_targets"] = {key.strip().casefold(): value for key, value in targets.items()}
    minutes = result["max_cook_time_minutes"]
    if minutes is not None and (
        isinstance(minutes, bool) or not isinstance(minutes, (int, float))
        or not isfinite(minutes) or minutes <= 0
    ):
        raise ValueError("max_cook_time_minutes must be a positive number or null")
    for key in ("prefer_reheatable", "require_reheatable"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be true or false")
    if result["max_finishing_effort"] not in (None, "low", "medium", "high"):
        raise ValueError("max_finishing_effort must be low, medium, high, or null")
    return result
