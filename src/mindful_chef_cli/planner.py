"""Deterministic recommendations and away-date proposals, without account writes.

Recipe facts come from supplied data. Reheating and finishing-effort assessments
are used only when supplied explicitly; a title is not proof of either property.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta
from math import isfinite
import re
from typing import Any

from .preferences import validate_preferences


_EFFORT = {"low": 0, "medium": 1, "high": 2}
_PROTEIN_ALIASES = {"chicken": "chicken", "beef": "beef", "pork": "pork", "fish": "fish",
                    "seafood": "fish", "salmon": "fish", "cod": "fish", "haddock": "fish",
                    "prawns": "fish", "turkey": "turkey", "lamb": "lamb", "duck": "duck",
                    "vegan": "plant", "vegetarian": "plant", "plant-based": "plant"}


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or item.get("description")
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _contains(text: str, term: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", text.casefold()))


def _known_list(recipe: dict[str, Any], key: str) -> bool:
    """Only an explicit list (including an empty list) is a known source field."""
    value = recipe.get(key)
    return isinstance(value, list) and len(_texts(value)) == len(value)


def _normalize(recipe: dict[str, Any]) -> dict[str, Any]:
    recipe_id = recipe.get("id", recipe.get("recipe_id"))
    if recipe_id is None or isinstance(recipe_id, (dict, list, bool)):
        raise ValueError("Every recipe needs a scalar id or recipe_id")
    name = recipe.get("name", recipe.get("title", ""))
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Every recipe needs a name or title")
    proteins = _texts(recipe.get("proteins", recipe.get("protein")))
    proteins = sorted({_PROTEIN_ALIASES.get(p.casefold(), p.casefold()) for p in proteins})
    protein_source = recipe.get("protein_source") if isinstance(recipe.get("protein_source"), str) else "provider" if proteins else "unknown"
    if "poultry" in proteins:
        species = [bird for bird in ("chicken", "turkey", "duck") if _contains(name, bird)]
        if len(species) == 1:
            proteins = sorted((set(proteins) - {"poultry"}) | {species[0]})
            protein_source = "recipe_title"
    minutes = recipe.get("cook_time_minutes", recipe.get("cooking_time_minutes"))
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not isfinite(minutes) or minutes <= 0:
        minutes = None
    reheat = recipe.get("reheat_suitability")
    if reheat is True:
        reheat = "good"
    elif reheat is False:
        reheat = "poor"
    if reheat not in ("good", "fair", "poor"):
        reheat = "unknown"
    effort = recipe.get("finishing_effort")
    if effort not in _EFFORT:
        effort = "unknown"
    return {
        "id": str(recipe_id), "name": name.strip(), "proteins": proteins, "protein_source": protein_source,
        "ingredients": _texts(recipe.get("ingredients")), "allergens": _texts(recipe.get("allergens")),
        "may_contain": _texts(recipe.get("may_contain")),
        "tags": _texts(recipe.get("tags")), "cook_time_minutes": minutes,
        "image_url": recipe.get("image_url", recipe.get("image")),
        "url": recipe.get("url") if isinstance(recipe.get("url"), str) else None,
        "nutrition": deepcopy(recipe.get("nutrition")) if isinstance(recipe.get("nutrition"), dict) else None,
        "steps": _texts(recipe.get("steps", recipe.get("instructions"))),
        "reheat_suitability": reheat, "finishing_effort": effort,
        "ingredients_known": _known_list(recipe, "ingredients"),
        "allergens_known": _known_list(recipe, "allergens"),
        "may_contain_known": _known_list(recipe, "may_contain"),
    }


def _history_signals(history: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if history is None:
        return result
    if not isinstance(history, list):
        raise ValueError("History must be a list")
    for entry in history:
        if not isinstance(entry, dict):
            raise ValueError("History entries must be objects")
        identifier = entry.get("recipe_id", entry.get("id"))
        if identifier is None:
            continue
        signal = result.setdefault(str(identifier), {"selected": 0, "ratings": []})
        if entry.get("selected") is True:
            signal["selected"] += 1
        rating = entry.get("rating")
        if isinstance(rating, (int, float)) and not isinstance(rating, bool) and isfinite(rating) and 1 <= rating <= 5:
            signal["ratings"].append(rating)
    return result


def _score(recipe: dict[str, Any], prefs: dict[str, Any], signal: dict[str, Any]) -> tuple[float, list[str], list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    excluded: list[str] = []
    score = 0.0
    text = " ".join([recipe["name"], *recipe["ingredients"], *recipe["tags"]]).casefold()
    if not recipe["ingredients_known"]:
        warnings.append("Ingredient list unavailable")
        if prefs["excluded_ingredients"]:
            excluded.append("Cannot verify ingredient exclusions without an ingredient list")
    for term in prefs["excluded_ingredients"]:
        if _contains(text, term):
            excluded.append(f"Excluded ingredient or term: {term}")
    if not recipe["allergens_known"]:
        warnings.append("Allergen data unavailable")
        if prefs["allergens"]:
            excluded.append("Cannot verify allergen exclusions without allergen data")
    if not recipe["may_contain_known"]:
        warnings.append("May-contain/cross-contact allergen data unavailable")
        if prefs["allergens"]:
            excluded.append("Cannot verify allergen exclusions without may-contain data")
    allergen_text = " ".join(recipe["allergens"] + recipe["ingredients"])
    trace_allergen_text = " ".join(recipe["may_contain"])
    for term in prefs["allergens"]:
        if _contains(allergen_text, term):
            excluded.append(f"Declared allergen or ingredient match: {term}")
        if _contains(trace_allergen_text, term):
            excluded.append(f"Declared may-contain allergen match: {term}")
    likes = [term for term in prefs["likes"] if _contains(text, term)]
    dislikes = [term for term in prefs["dislikes"] if _contains(text, term)]
    if likes:
        score += min(15, 3 * len(likes))
        reasons.append("Matches likes: " + ", ".join(likes))
    if dislikes:
        score -= min(24, 6 * len(dislikes))
        reasons.append("Matches dislikes: " + ", ".join(dislikes))
    minutes = recipe["cook_time_minutes"]
    if prefs["max_cook_time_minutes"] is not None:
        if minutes is None:
            excluded.append("Cannot verify maximum cooking time")
        elif minutes > prefs["max_cook_time_minutes"]:
            excluded.append("Exceeds maximum cooking time")
    if recipe["reheat_suitability"] == "unknown":
        warnings.append("Reheating quality unknown; check the recipe and household experience")
        if prefs["require_reheatable"]:
            excluded.append("Cannot verify required reheating quality")
    elif recipe["reheat_suitability"] == "poor" and prefs["require_reheatable"]:
        excluded.append("Reheating quality is marked poor")
    if prefs["prefer_reheatable"] or prefs["require_reheatable"]:
        score += {"good": 6, "fair": 1, "poor": -8, "unknown": 0}[recipe["reheat_suitability"]]
        if recipe["reheat_suitability"] != "unknown":
            reasons.append("Supplied reheating-quality assessment: " + recipe["reheat_suitability"])
    effort = recipe["finishing_effort"]
    if effort == "unknown":
        warnings.append("Final cooking/assembly effort unknown")
        if prefs["max_finishing_effort"] is not None:
            excluded.append("Cannot verify maximum finishing effort")
    elif prefs["max_finishing_effort"] is not None:
        if _EFFORT[effort] > _EFFORT[prefs["max_finishing_effort"]]:
            excluded.append("Exceeds maximum finishing effort")
        else:
            reasons.append("Within supplied finishing-effort limit")
    ratings = signal.get("ratings", [])
    if ratings:
        average = sum(ratings) / len(ratings)
        score += (average - 3) * 9
        reasons.append(f"Explicit household rating: {average:.1f}/5")
    elif signal.get("selected", 0):
        score += min(3, signal["selected"]) * 0.75
        reasons.append("Previously selected; weak signal, not evidence it was liked")
    if not recipe["proteins"]:
        warnings.append("Protein category unknown")
    elif "poultry" in recipe["proteins"]:
        warnings.append("Poultry species unknown; chicken/turkey/duck targets cannot be verified from this recipe")
    return score, reasons, warnings, excluded


def _balance_penalty(counts: Counter[str], targets: dict[str, int], *, final: bool) -> float:
    overshoot = sum(max(0, counts[p] - target) for p, target in targets.items())
    deficit = sum(max(0, target - counts[p]) for p, target in targets.items()) if final else 0
    return 8 * (overshoot + deficit)


def recommend_meals(
    recipes: list[dict[str, Any]],
    preferences: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    count: int = 3,
) -> dict[str, Any]:
    """Rank recipes and propose a distinct combination using stated preferences.

    ``history`` entries accept ``recipe_id``, ``selected: true`` and a 1..5
    ``rating``. Unrated selections are deliberately weak evidence. Explicit
    recipe-level ``reheat_suitability`` (good/fair/poor) and ``finishing_effort``
    (low/medium/high) are supplied assessments, never inferred provider facts.
    Protein combinations use a bounded beam search, so optimality is not claimed.
    """
    if not isinstance(recipes, list) or any(not isinstance(recipe, dict) for recipe in recipes):
        raise ValueError("Recipes must be a list of objects")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 12:
        raise ValueError("count must be an integer from 1 to 12")
    prefs = validate_preferences(preferences)
    signals = _history_signals(history)
    ranked, excluded = [], []
    seen = set()
    for raw in recipes:
        recipe = _normalize(raw)
        if recipe["id"] in seen:
            raise ValueError("Duplicate recipe id: " + recipe["id"])
        seen.add(recipe["id"])
        score, reasons, warnings, disqualifiers = _score(recipe, prefs, signals.get(recipe["id"], {}))
        item = {**recipe, "score": round(score, 3), "reasons": reasons, "warnings": warnings}
        if disqualifiers:
            excluded.append({"id": recipe["id"], "name": recipe["name"], "reasons": disqualifiers})
        else:
            ranked.append(item)
    ranked.sort(key=lambda item: (-item["score"], item["id"]))
    targets = {_PROTEIN_ALIASES.get(p, p): target for p, target in prefs["protein_targets"].items()}
    needed = min(count, len(ranked))
    # (selected indexes, sum of individual scores, protein counts), in index order.
    beam: list[tuple[tuple[int, ...], float, Counter[str]]] = [((), 0, Counter())]
    for depth in range(needed):
        candidates = []
        for indexes, total, counts in beam:
            first = indexes[-1] + 1 if indexes else 0
            for index in range(first, len(ranked) - (needed - depth - 1)):
                new_counts = counts + Counter(ranked[index]["proteins"])
                new_indexes = indexes + (index,)
                new_total = total + ranked[index]["score"]
                objective = new_total - _balance_penalty(new_counts, targets, final=depth == needed - 1)
                candidates.append((objective, new_indexes, new_total, new_counts))
        candidates.sort(key=lambda entry: (-entry[0], tuple(ranked[i]["id"] for i in entry[1])))
        beam = [(indexes, total, counts) for _, indexes, total, counts in candidates[:256]]
    indexes, total, counts = beam[0] if beam else ((), 0.0, Counter())
    selected = [ranked[index] for index in indexes]
    warnings = ["Recommendations are proposals; no account changes were made.",
                "Recipe metadata is not an allergen or food-safety guarantee; verify the current supplier information."]
    if needed < count:
        warnings.append(f"Only {needed} recipes satisfy the supplied constraints; requested {count}")
    for protein, target in targets.items():
        if counts[protein] != target:
            warnings.append(f"Protein target not met: {protein} requested {target}, proposed {counts[protein]}")
    return {"selected": selected, "ranked": ranked, "excluded": excluded,
            "protein_counts": dict(sorted(counts.items())), "protein_targets": targets,
            "requested_count": count, "selection_score": round(total - _balance_penalty(counts, targets, final=True), 3),
            "warnings": warnings}


def _date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date or datetime")
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date or datetime") from exc


def plan_away_weeks(
    deliveries: list[str | dict[str, Any]], events: list[dict[str, Any]], *, days: int = 7
) -> list[dict[str, Any]]:
    """Propose delivery actions from minimal supplied away intervals.

    Intervals use inclusive ``start`` and exclusive ``end`` dates. A confirmed
    away event has ``away: true, confidence: 'confirmed'``. Only full coverage of
    a delivery's meal window produces ``propose_skip``; partial/uncertain overlap
    produces ``review``. Other overlaps are ignored when ``away`` is false.
    Dates are interpreted in their supplied local timezone; callers must first
    convert calendar datetimes to the household timezone and supply complete
    relevant evidence. Calendar titles and locations are neither needed nor
    returned. No calendar or supplier connection is made.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
        raise ValueError("days must be an integer from 1 to 31")
    if not isinstance(deliveries, list) or not isinstance(events, list):
        raise ValueError("deliveries and events must be lists")
    intervals = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Each event must be an object")
        if event.get("away") is False:
            continue
        start, end = _date(event.get("start"), "event start"), _date(event.get("end"), "event end")
        if end <= start:
            raise ValueError("Event end must follow start (end is exclusive)")
        confirmed = event.get("away") is True and event.get("confidence") == "confirmed"
        intervals.append((start, end, confirmed))
    result = []
    for delivery in deliveries:
        raw = delivery.get("delivery_date", delivery.get("date")) if isinstance(delivery, dict) else delivery
        start = _date(raw, "delivery date")
        end = start + timedelta(days=days)
        overlap = [(max(a, start), min(b, end), confirmed) for a, b, confirmed in intervals if a < end and b > start]
        confirmed_days = set()
        for a, b, confirmed in overlap:
            if confirmed:
                confirmed_days.update(a + timedelta(days=n) for n in range((b - a).days))
        if len(confirmed_days) == days:
            action, reasons = "propose_skip", ["Confirmed away intervals cover the whole supplied meal window"]
        elif overlap:
            action, reasons = "review", ["Away evidence is uncertain or covers only part of the supplied meal window"]
        else:
            action, reasons = "keep", ["No supplied away interval overlaps the meal window"]
        item = {"delivery_date": start.isoformat(), "window_end_exclusive": end.isoformat(), "action": action,
                "confirmed_away_days": len(confirmed_days), "window_days": days, "reasons": reasons,
                "account_changed": False}
        if isinstance(delivery, dict) and delivery.get("id") is not None:
            item["delivery_id"] = str(delivery["id"])
        result.append(item)
    return result
