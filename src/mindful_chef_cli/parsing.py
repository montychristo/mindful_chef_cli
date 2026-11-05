"""Narrow parsers for Mindful Chef's server-rendered storefront.

Only return meal and delivery data. Raw HTML, account contact details, and form
credentials must not be logged. Forms are internal transport objects: their
hidden fields may contain CSRF tokens and must never be included in CLI output.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str:
    return " ".join(node.stripped_strings) if node else ""


def _number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def csrf(html: str) -> str:
    """Read the live page's CSRF token; never reuse a baked-in token."""
    soup = _soup(html)
    token = soup.select_one('meta[name="csrf-token"]')
    if token and token.get("content"):
        return str(token["content"])
    token = soup.select_one('input[name="authenticity_token"]')
    if token and token.get("value"):
        return str(token["value"])
    raise ValueError("The page has no CSRF token; the form contract may have changed.")


def form_fields(form: Tag, soup: BeautifulSoup | None = None) -> list[tuple[str, str]]:
    """Return successful controls without submit buttons, retaining duplicates.

    This includes controls outside the form which explicitly name its id.
    It excludes disabled and unchecked controls, as a browser submission does.
    """
    root = soup or form
    form_id = form.get("id")
    controls = root.find_all(["input", "select", "textarea"])
    fields: list[tuple[str, str]] = []
    for control in controls:
        owner = control.get("form")
        if owner is not None:
            if not form_id or owner != form_id:
                continue
        elif control.find_parent("form") is not form:
            continue
        name = control.get("name")
        if not name or control.has_attr("disabled") or control.find_parent("fieldset", disabled=True):
            continue
        kind = str(control.get("type", "text")).lower()
        if kind in {"submit", "button", "reset", "image", "file"}:
            continue
        if kind in {"checkbox", "radio"} and not control.has_attr("checked"):
            continue
        if control.name == "select":
            options = [option for option in control.find_all("option") if not option.has_attr("disabled")]
            chosen = [option for option in options if option.has_attr("selected")]
            if not control.has_attr("multiple"):
                chosen = chosen[:1] or options[:1]
            fields.extend((str(name), str(option.get("value", _text(option)))) for option in chosen)
        elif control.name == "textarea":
            fields.append((str(name), control.get_text()))
        else:
            default = "on" if kind in {"checkbox", "radio"} else ""
            fields.append((str(name), str(control.get("value", default))))
    return fields


def _form(form: Tag, soup: BeautifulSoup) -> dict[str, Any]:
    return {
        "id": form.get("id"),
        "action": str(form.get("action", "")),
        "method": str(form.get("method", "get")).lower(),
        "fields": form_fields(form, soup),
    }


def parse_forms(html: str) -> list[dict[str, Any]]:
    soup = _soup(html)
    return [_form(form, soup) for form in soup.find_all("form") if form.get("method", "get").lower() != "dialog"]


def _recipe_cards(root: Tag | BeautifulSoup) -> list[dict[str, Any]]:
    recipes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in root.select('[data-cart-item-type-value="recipe"][data-cart-item-id-value]'):
        recipe_id = str(card["data-cart-item-id-value"])
        if recipe_id in seen:
            continue
        link = card.select_one('a[href*="/healthy-recipes/"]')
        # Saved cart fieldsets carry metadata but are not menu cards.
        if link is None:
            continue
        seen.add(recipe_id)
        card_text = _text(card)
        minutes = re.search(r"\b(\d+)\s*min(?:s|utes)?\b", card_text, re.I)
        calories = re.search(r"\b(\d+)\s*kcal\b", card_text, re.I)
        group = card.get("data-cart-item-food-group-value")
        recipe = {
            "id": recipe_id,
            "name": card.get("data-cart-item-title-value") or _text(card.find(["h2", "h3"])),
            "url": link.get("href") if link else None,
            "image_url": card.get("data-cart-item-image-url-value"),
            "protein": str(group).lower() if group else None,
            "food_group": group,
            "cuisine": card.get("data-cart-item-cuisine-value"),
            "portions": _number(card.get("data-cart-item-portions-value")),
            "price_cents": _number(card.get("data-cart-item-price-cents-value")),
            "cook_time_minutes": int(minutes[1]) if minutes else None,
            "calories": int(calories[1]) if calories else None,
            "tags": [_text(tag) for tag in card.find_all("p") if "--recipe-tag-bg" in " ".join(tag.get("class", []))],
            "contains_gluten": _bool_attr(card, "data-cart-item-contains-gluten-value"),
            "within_preferences": _bool_attr(card, "data-cart-item-within-preferences-value"),
            "ingredients": None,
            "allergens": None,
            "steps": None,
        }
        recipes.append(recipe)
    return recipes


def _bool_attr(node: Tag, name: str) -> bool | None:
    value = node.get(name)
    return {"true": True, "false": False}.get(str(value).lower())


def parse_menu(html: str) -> dict[str, Any]:
    soup = _soup(html)
    form = soup.select_one("form#web_cart_form") or soup.select_one('form[data-controller~="cart"]')
    formspec = _form(form, soup) if form else None
    fields = formspec["fields"] if formspec else []
    selection: dict[str, dict[str, Any]] = {}
    products: dict[str, dict[str, Any]] = {}
    for name, value in fields:
        match = re.fullmatch(r"delivery\[contents_attributes\]\[([^]]+)\]\[(recipe_id|portions)\]", name)
        if match:
            selection.setdefault(match[1], {})[match[2]] = int(value) if match[2] == "portions" and value.isdigit() else value
        match = re.fullmatch(r"delivery\[(?:expanded_)?product_contents_attributes\]\[([^]]+)\]\[([^]]+)\]", name)
        if match:
            products.setdefault(match[1], {})[match[2]] = value
    flat = dict(fields)
    return {
        "recipes": _recipe_cards(soup),
        "selection": [item for item in selection.values() if item.get("recipe_id")],
        "products_preserved": list(products.values()),
        "portion_count": _number(flat.get("delivery[portion_count_per_meal]", form.get("data-cart-portion-count-value") if form else None)),
        "delivery_date": flat.get("delivery[delivery_date]", form.get("data-cart-delivery-date-value") if form else None),
        "max_recipes": _number(form.get("data-cart-max-recipe-count-value")) if form else None,
        "min_recipes": _number(form.get("data-cart-min-recipe-count-value")) if form else None,
        "min_order_amount_cents": _number(form.get("data-cart-min-order-amount-value")) if form else None,
        "shipping_amount_cents": _number(form.get("data-cart-shipping-amount-value")) if form else None,
        "cart_form": formspec,
    }


def _jsonld_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        if "@graph" in value:
            yield from _jsonld_nodes(value["@graph"])


def _duration_minutes(value: Any) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(value or ""))
    if not match or not any(match.groups()):
        return None
    return int(match[1] or 0) * 60 + int(match[2] or 0) + (1 if int(match[3] or 0) else 0)


def _steps(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if value.get("text"):
            return [str(value["text"])]
        return _steps(value.get("itemListElement", []))
    if isinstance(value, list):
        return [step for item in value for step in _steps(item)]
    return []


def _allergens(soup: BeautifulSoup) -> tuple[list[str] | None, list[str] | None]:
    label = soup.find(string=lambda text: isinstance(text, str) and text.strip() == "Allergens")
    if not label or not isinstance(label.parent, Tag):
        return None, None
    block = label.parent.parent
    if not isinstance(block, Tag):
        return None, None
    values: dict[str, list[str]] = {}
    for node in block.find_all(["span", "p", "strong"]):
        text = _text(node)
        key = text.rstrip(":").strip().lower()
        if key in {"contains", "may contain"}:
            sibling = node.find_next_sibling()
            if sibling:
                text = _text(sibling)
                values[key] = [] if text.lower() in {"none", "no allergens"} else [part.strip() for part in re.split(r",|;", text) if part.strip()]
    return values.get("contains"), values.get("may contain")


def parse_recipe(html: str) -> dict[str, Any]:
    soup = _soup(html)
    recipe = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for node in _jsonld_nodes(value):
            types = node.get("@type", [])
            if types == "Recipe" or isinstance(types, list) and "Recipe" in types:
                recipe = node
                break
        if recipe:
            break
    if recipe is None:
        raise ValueError("The page has no Recipe structured data; the recipe contract may have changed.")
    image = recipe.get("image", [])
    images = [image] if isinstance(image, str) else image if isinstance(image, list) else [image.get("url")] if isinstance(image, dict) else []
    images = [item.get("url") if isinstance(item, dict) else item for item in images]
    images = [item for item in images if isinstance(item, str)]
    canonical = soup.select_one('link[rel="canonical"]')
    url = canonical.get("href") if canonical else str(recipe.get("url", recipe.get("@id", ""))).split("#")[0]
    allergens, may_contain = _allergens(soup)
    keywords = recipe.get("keywords", [])
    tags = [part.strip() for part in keywords.split(",") if part.strip()] if isinstance(keywords, str) else keywords
    return {
        "id": str(recipe["identifier"]) if recipe.get("identifier") is not None else None,
        "name": recipe.get("name"),
        "description": recipe.get("description"),
        "url": url or None,
        "image_url": images[0] if images else None,
        "images": images,
        "ingredients": recipe.get("recipeIngredient"),
        "steps": _steps(recipe.get("recipeInstructions")),
        "cook_time_minutes": _duration_minutes(recipe.get("totalTime") or recipe.get("cookTime")),
        "prep_time_minutes": _duration_minutes(recipe.get("prepTime")),
        "servings": recipe.get("recipeYield"),
        "cuisine": recipe.get("recipeCuisine"),
        "tags": tags if isinstance(tags, list) else [],
        "nutrition": recipe.get("nutrition"),
        "allergens": allergens,
        "may_contain": may_contain,
        "allergen_source": "provider_html" if allergens is not None or may_contain is not None else None,
        "reheat_suitability": None,
        "finishing_effort": None,
    }


def _date_in(value: str) -> str | None:
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)
    return match[1] if match else None


def parse_orders(html: str) -> list[dict[str, Any]]:
    """Extract delivery cards without returning arbitrary account-page text."""
    soup = _soup(html)
    orders: list[dict[str, Any]] = []
    for group in soup.find_all(attrs={"aria-label": re.compile(r"^Delivery details\b", re.I)}):
        label = str(group.get("aria-label", ""))
        day = _date_in(label)
        scope = group
        # The details disclosure and its header are siblings. Expand only while
        # the container still belongs to one delivery, never the whole account.
        for parent in group.parents:
            if not isinstance(parent, Tag) or parent.name in {"html", "body"}:
                break
            groups = parent.find_all(attrs={"aria-label": re.compile(r"^Delivery details\b", re.I)})
            if len(groups) != 1:
                break
            scope = parent
            if parent.find(["h2", "h3"]) and parent.find("a", href=re.compile(r"/my-account/deliveries/\d{4}-\d{2}-\d{2}")):
                break
        # Live order lists can alternate separate header and details-card divs.
        # Include only a preceding header whose details link names this date.
        header = scope.find_previous_sibling()
        header_links = []
        if isinstance(header, Tag) and header.find("h2"):
            detail_link = header.find("a", href=re.compile(r"/my-account/deliveries/\d{4}-\d{2}-\d{2}/details(?:\?|$)"))
            header_day = _date_in(str(detail_link.get("href", ""))) if detail_link else None
            if header_day and (day is None or day == header_day):
                day = day or header_day
                header_links = header.find_all("a", href=True)
            else:
                header = None
        else:
            header = None
        # ISO route dates are less ambiguous than locale-formatted headings.
        links = header_links + scope.find_all("a", href=True)
        if not day:
            day = next((_date_in(str(link["href"])) for link in links if _date_in(str(link["href"]))), None)
        if not day:
            continue
        body = (_text(header) + " " + _text(scope)).lower()
        if re.search(r"\bskipped\b", body):
            status = "skipped"
        elif re.search(r"\b(delivered|past delivery)\b", body):
            status = "delivered"
        elif re.search(r"\b(processing|being prepared|on its way|dispatched)\b", body):
            status = "processing"
        elif re.search(r"\b(edit|skip|upcoming|scheduled|chosen)\b", body):
            status = "scheduled"
        else:
            status = "unknown"
        chosen_by = "customer" if re.search(r"(?:chosen|selected) by you|you (?:chose|selected)|your (?:chosen|selected) recipes", body) else "provider" if re.search(r"curated by us|(?:chosen|selected|picked) for you|(?:chosen|selected) by mindful chef|chef(?:'s)? choice", body) else "unknown"
        edit = next((str(link["href"]) for link in links if re.search(r"\bedit\b", _text(link), re.I) or urlparse(str(link["href"])).path.endswith("/edit")), None)
        skip = next((str(link["href"]) for link in links if re.search(r"\bskip\b", _text(link), re.I) and not re.search(r"unskip", _text(link), re.I)), None)
        unskip_form = next((_form(form, soup) for form in scope.find_all("form") if re.search(r"unskip|restore delivery", str(form.get("action", "")) + " " + _text(form), re.I)), None)
        meals = []
        for link in group.find_all("a", href=True):
            if "/healthy-recipes/" not in str(link["href"]):
                continue
            image = link.find("img")
            name = _text(link) or (image.get("alt") if image else "")
            meals.append({"name": name, "url": str(link["href"]), "image_url": image.get("src") if image else None})
        orders.append({"delivery_date": day, "status": status, "chosen_by": chosen_by, "edit_url": edit, "skip_url": skip, "unskip_form": unskip_form, "recipes": meals})
    return orders
