"""Capture and persist write-once provenance of ingredient lines received from an import source."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from api.models.recipe import MAX_INGREDIENTS
from api.models.structured_ingredient import ImportMethod, SourceIngredientsMeta

logger = logging.getLogger(__name__)

SOURCE_INGREDIENTS_FIELD = "source_ingredients"
SOURCE_INGREDIENTS_META_FIELD = "source_ingredients_meta"
EXTRACTOR_RECIPE_SCRAPERS = "recipe-scrapers"
MAX_SOURCE_LINE_LENGTH = 1000
_LINES_ADAPTER = TypeAdapter(list[str])


@dataclass(frozen=True)
class SourceIngredientCapture:
    """Source ingredient lines and their provenance, ready to persist on a new recipe."""

    lines: list[str]
    meta: SourceIngredientsMeta


def capture_source_ingredients(
    raw_lines: object, *, import_method: ImportMethod, extractor: str = EXTRACTOR_RECIPE_SCRAPERS
) -> SourceIngredientCapture | None:
    """Capture ingredient lines exactly as the import source returned them.

    Lines are stored verbatim (no whitespace, control-character, or unicode normalization). Only storage
    bounds are applied, and ``meta.truncated`` records whether they changed anything.

    Args:
        raw_lines: The ``ingredients`` value returned by the scraper.
        import_method: How the source HTML was obtained.
        extractor: The component that extracted the lines.

    Returns:
        A capture, or None if the source provided no usable list of strings.
    """
    if not isinstance(raw_lines, list) or not raw_lines:
        return None
    items = cast("list[object]", raw_lines)
    if not all(isinstance(item, str) for item in items):
        logger.warning("Scraper returned non-string ingredient lines; skipping source provenance")
        return None
    source = cast("list[str]", items)

    lines = [line[:MAX_SOURCE_LINE_LENGTH] for line in source[:MAX_INGREDIENTS]]
    meta = SourceIngredientsMeta(
        extractor=extractor,
        import_method=import_method,
        captured_at=datetime.now(tz=UTC),
        line_count=len(source),
        truncated=lines != source,
    )
    return SourceIngredientCapture(lines=lines, meta=meta)


def source_ingredient_fields(capture: SourceIngredientCapture | None) -> dict[str, Any]:
    """Build Firestore-ready source provenance fields from a capture (empty when there is none)."""
    if capture is None:
        return {}
    return {SOURCE_INGREDIENTS_FIELD: list(capture.lines), SOURCE_INGREDIENTS_META_FIELD: capture.meta.model_dump()}


def load_source_ingredient_fields(data: object) -> dict[str, Any]:
    """Validate stored source provenance for model construction, dropping both fields if either is malformed.

    Args:
        data: Raw Firestore document data.

    Returns:
        Model-ready values for both source fields (None when absent or invalid).
    """
    empty: dict[str, Any] = {SOURCE_INGREDIENTS_FIELD: None, SOURCE_INGREDIENTS_META_FIELD: None}
    if not isinstance(data, dict):
        return empty
    stored = cast("dict[str, Any]", data)
    lines, meta = stored.get(SOURCE_INGREDIENTS_FIELD), stored.get(SOURCE_INGREDIENTS_META_FIELD)
    if lines is None or meta is None:
        return empty
    try:
        return {
            SOURCE_INGREDIENTS_FIELD: _LINES_ADAPTER.validate_python(lines, strict=True),
            SOURCE_INGREDIENTS_META_FIELD: SourceIngredientsMeta.model_validate(meta),
        }
    except ValidationError:
        logger.warning("Ignoring malformed stored source ingredients")
        return empty
