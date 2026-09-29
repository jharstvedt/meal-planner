"""Derive and persist structured ingredients alongside authoritative recipe ingredient lines."""

import logging
from collections import deque
from enum import StrEnum
from typing import Any
from uuid import uuid4

import ingredient_parser
from google.cloud.firestore_v1 import DELETE_FIELD
from pydantic import TypeAdapter, ValidationError

from api.models.structured_ingredient import StructuredIngredient, StructuredIngredientsMeta
from api.services.structured_ingredient_parser import (
    PARSER_NAME,
    WRAPPER_VERSION,
    compute_ingredients_hash,
    parse_ingredient_list,
)

logger = logging.getLogger(__name__)

STRUCTURED_INGREDIENTS_FIELD = "structured_ingredients"
STRUCTURED_INGREDIENTS_META_FIELD = "structured_ingredients_meta"
_TEMP_ID_PREFIX = "tmp_"
_ITEMS_ADAPTER = TypeAdapter(list[StructuredIngredient])


class StructuredStatus(StrEnum):
    """State of stored structured ingredients relative to the authoritative ingredient lines."""

    CURRENT = "current"
    MISSING = "missing"
    STALE = "stale"
    MALFORMED = "malformed"


def is_structured_ingredients_current(
    ingredients: list[str], meta: StructuredIngredientsMeta | dict[str, Any] | None
) -> bool:
    """Check whether stored structured-ingredient metadata still matches the recipe ingredients.

    Args:
        ingredients: The authoritative ingredient lines.
        meta: Stored metadata as a model or raw Firestore dict.

    Returns:
        True if the hash, parser name, parser version, and wrapper version all match.
    """
    if isinstance(meta, dict):
        try:
            meta = StructuredIngredientsMeta.model_validate(meta)
        except ValidationError:
            return False
    if not isinstance(meta, StructuredIngredientsMeta):
        return False
    return (
        meta.parser_name == PARSER_NAME
        and meta.parser_version == ingredient_parser.__version__
        and meta.wrapper_version == WRAPPER_VERSION
        and meta.ingredients_hash == compute_ingredients_hash(ingredients)
    )


def structured_ingredients_status(ingredients: list[str], existing_data: object) -> StructuredStatus:
    """Classify stored structured ingredients against the lines they should describe.

    Args:
        ingredients: The authoritative ingredient lines.
        existing_data: The stored Firestore document data.

    Returns:
        MISSING if neither field is stored, MALFORMED if stored data fails validation or does not
        line up with ``ingredients``, STALE if the hash or versions differ, otherwise CURRENT.
    """
    existing = existing_data if isinstance(existing_data, dict) else {}
    items, meta = existing.get(STRUCTURED_INGREDIENTS_FIELD), existing.get(STRUCTURED_INGREDIENTS_META_FIELD)
    if items is None and meta is None:
        return StructuredStatus.MISSING
    try:
        parsed_items = _ITEMS_ADAPTER.validate_python(items)
        parsed_meta = StructuredIngredientsMeta.model_validate(meta)
    except ValidationError:
        return StructuredStatus.MALFORMED
    if not is_structured_ingredients_current(ingredients, parsed_meta):
        return StructuredStatus.STALE
    if [item.raw_text for item in parsed_items] != ingredients:
        return StructuredStatus.MALFORMED
    return StructuredStatus.CURRENT


def assign_stable_ids(lines: list[str], previous_items: object = None) -> list[str]:
    """Assign persistent IDs to lines, reusing each previous ID at most once for an identical raw line.

    Args:
        lines: The new ordered ingredient lines.
        previous_items: Previously stored structured ingredient dicts, if any.

    Returns:
        One unique ID per line, in order.
    """
    available: dict[str, deque[str]] = {}
    seen: set[str] = set()
    for item in previous_items if isinstance(previous_items, list) else []:
        if not isinstance(item, dict):
            continue
        item_id, raw_text = item.get("id"), item.get("raw_text")
        if not isinstance(item_id, str) or not item_id or item_id.startswith(_TEMP_ID_PREFIX) or item_id in seen:
            continue
        if isinstance(raw_text, str):
            seen.add(item_id)
            available.setdefault(raw_text, deque()).append(item_id)

    ids: list[str] = []
    for line in lines:
        queue = available.get(line)
        ids.append(queue.popleft() if queue else uuid4().hex)
    return ids


def build_structured_fields(ingredients: list[str], previous_items: object = None) -> dict[str, Any] | None:
    """Parse ingredient lines into Firestore-ready structured fields with stable IDs.

    Args:
        ingredients: The authoritative ingredient lines.
        previous_items: Previously stored structured ingredient dicts used for ID reconciliation.

    Returns:
        A dict with both structured fields, or None if parsing failed entirely.
    """
    try:
        parsed, meta = parse_ingredient_list(ingredients)
        ids = assign_stable_ids(ingredients, previous_items)
        items = [item.model_copy(update={"id": new_id}).model_dump() for item, new_id in zip(parsed, ids, strict=True)]
    except Exception:
        logger.exception("Structured ingredient parsing failed; persisting recipe without structured ingredients")
        return None
    return {STRUCTURED_INGREDIENTS_FIELD: items, STRUCTURED_INGREDIENTS_META_FIELD: meta.model_dump()}


def structured_fields_for_write(ingredients: object, existing_data: object = None, *, is_new: bool = False) -> dict:
    """Compute structured-ingredient fields to merge into a recipe write.

    Args:
        ingredients: The ingredient lines that will be persisted.
        existing_data: The stored Firestore document data, if any.
        is_new: True when writing a brand-new document (nothing stale to remove).

    Returns:
        Empty dict to leave stored fields untouched, regenerated fields, or DELETE_FIELD sentinels.
    """
    existing = existing_data if isinstance(existing_data, dict) else {}
    has_stored = STRUCTURED_INGREDIENTS_FIELD in existing or STRUCTURED_INGREDIENTS_META_FIELD in existing
    if not isinstance(ingredients, list):
        return _deleted_fields() if has_stored and not is_new else {}

    if structured_ingredients_status(ingredients, existing) is StructuredStatus.CURRENT:
        return {}

    fields = build_structured_fields(ingredients, existing.get(STRUCTURED_INGREDIENTS_FIELD))
    if fields is not None:
        return fields
    return {} if is_new else _deleted_fields()


def load_structured_fields(data: dict) -> dict[str, Any]:
    """Validate stored structured fields for model construction, dropping both if either is malformed.

    Args:
        data: Raw Firestore document data.

    Returns:
        Model-ready values for both structured fields (None when absent or invalid).
    """
    items, meta = data.get(STRUCTURED_INGREDIENTS_FIELD), data.get(STRUCTURED_INGREDIENTS_META_FIELD)
    if items is None or meta is None:
        return {STRUCTURED_INGREDIENTS_FIELD: None, STRUCTURED_INGREDIENTS_META_FIELD: None}
    try:
        return {
            STRUCTURED_INGREDIENTS_FIELD: _ITEMS_ADAPTER.validate_python(items),
            STRUCTURED_INGREDIENTS_META_FIELD: StructuredIngredientsMeta.model_validate(meta),
        }
    except ValidationError:
        logger.warning("Ignoring malformed stored structured ingredients")
        return {STRUCTURED_INGREDIENTS_FIELD: None, STRUCTURED_INGREDIENTS_META_FIELD: None}


def apply_structured_write(data: dict, fields: dict) -> dict:
    """Apply a structured-field write (including DELETE_FIELD sentinels) to an in-memory document copy.

    Args:
        data: Document data to mutate in place.
        fields: The result of structured_fields_for_write.

    Returns:
        The same ``data`` dict, for chaining.
    """
    for key, value in fields.items():
        if value is DELETE_FIELD:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def _deleted_fields() -> dict:
    """Return sentinels that remove both structured fields from a Firestore document."""
    return {STRUCTURED_INGREDIENTS_FIELD: DELETE_FIELD, STRUCTURED_INGREDIENTS_META_FIELD: DELETE_FIELD}
