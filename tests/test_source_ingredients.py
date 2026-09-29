"""Tests for import-source ingredient provenance (api/storage/source_ingredients.py and its storage wiring)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from api.models.recipe import MAX_INGREDIENTS, Recipe, RecipeCreate, RecipeUpdate
from api.models.structured_ingredient import SourceIngredientsMeta
from api.storage.recipe_storage import (
    EnhancementMetadata,
    _doc_to_recipe,
    remove_enhancement,
    save_recipe,
    update_recipe,
)
from api.storage.source_ingredients import (
    MAX_SOURCE_LINE_LENGTH,
    SourceIngredientCapture,
    capture_source_ingredients,
    load_source_ingredient_fields,
    source_ingredient_fields,
)

_CAPTURED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _meta_dict(**overrides: object) -> dict:
    meta = {
        "extractor": "recipe-scrapers",
        "import_method": "scrape",
        "captured_at": _CAPTURED_AT,
        "line_count": 2,
        "truncated": False,
    }
    meta.update(overrides)
    return meta


def _capture(lines: list[str]) -> SourceIngredientCapture:
    captured = capture_source_ingredients(lines, import_method="parse")
    assert captured is not None
    return captured


def _mock_db(doc_ref: MagicMock) -> MagicMock:
    db = MagicMock()
    db.collection.return_value.document.return_value = doc_ref
    return db


class TestCaptureSourceIngredients:
    """Tests for capture_source_ingredients."""

    def test_preserves_lines_verbatim(self) -> None:
        """Should keep whitespace, unicode fractions, and odd formatting exactly as received."""
        raw = ["  2 ½ dl  grädde ", "Salt\tand pepper", "1 (14-oz.) can tomatoes"]

        result = capture_source_ingredients(raw, import_method="scrape")

        assert result is not None
        assert result.lines == raw
        assert result.lines is not raw
        assert result.meta.extractor == "recipe-scrapers"
        assert result.meta.import_method == "scrape"
        assert result.meta.line_count == 3
        assert result.meta.truncated is False
        assert result.meta.captured_at.tzinfo is not None

    def test_records_import_method(self) -> None:
        """Should record the provided import method."""
        result = capture_source_ingredients(["flour"], import_method="parse")

        assert result is not None
        assert result.meta.import_method == "parse"

    def test_truncates_line_count_and_records_it(self) -> None:
        """Should cap the number of lines and flag truncation with the original count."""
        raw = [f"item {i}" for i in range(MAX_INGREDIENTS + 5)]

        result = capture_source_ingredients(raw, import_method="scrape")

        assert result is not None
        assert len(result.lines) == MAX_INGREDIENTS
        assert result.meta.line_count == MAX_INGREDIENTS + 5
        assert result.meta.truncated is True

    def test_truncates_long_lines_and_records_it(self) -> None:
        """Should cap line length and flag truncation."""
        raw = ["x" * (MAX_SOURCE_LINE_LENGTH + 1), "salt"]

        result = capture_source_ingredients(raw, import_method="scrape")

        assert result is not None
        assert result.lines == ["x" * MAX_SOURCE_LINE_LENGTH, "salt"]
        assert result.meta.line_count == 2
        assert result.meta.truncated is True

    @pytest.mark.parametrize("raw", [None, [], "flour", {"a": 1}, ["flour", 3], ["flour", None]])
    def test_skips_unusable_input(self, raw: object) -> None:
        """Should return None rather than fabricate or coerce lines."""
        assert capture_source_ingredients(raw, import_method="scrape") is None


class TestLoadSourceIngredientFields:
    """Tests for load_source_ingredient_fields."""

    def test_loads_valid_fields(self) -> None:
        """Should return lines and validated meta."""
        result = load_source_ingredient_fields(
            {"source_ingredients": ["a", "b"], "source_ingredients_meta": _meta_dict()}
        )

        assert result["source_ingredients"] == ["a", "b"]
        assert result["source_ingredients_meta"] == SourceIngredientsMeta(**_meta_dict())

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"source_ingredients": ["a"]},
            {"source_ingredients_meta": _meta_dict()},
            {"source_ingredients": "a", "source_ingredients_meta": _meta_dict()},
            {"source_ingredients": ["a", 1], "source_ingredients_meta": _meta_dict()},
            {"source_ingredients": ["a"], "source_ingredients_meta": _meta_dict(import_method="bogus")},
            {"source_ingredients": ["a"], "source_ingredients_meta": "not a dict"},
            "not a dict",
        ],
    )
    def test_drops_absent_or_malformed(self, data: object) -> None:
        """Should return None for both fields when absent, partial, or malformed."""
        assert load_source_ingredient_fields(data) == {"source_ingredients": None, "source_ingredients_meta": None}

    def test_round_trips_write_format(self) -> None:
        """Should load exactly what source_ingredient_fields writes."""
        capture = _capture(["1 cup flour"])

        result = load_source_ingredient_fields(source_ingredient_fields(capture))

        assert result == {"source_ingredients": capture.lines, "source_ingredients_meta": capture.meta}


class TestRecipeSourceFields:
    """Tests for source provenance on the Recipe model and _doc_to_recipe."""

    def test_legacy_doc_has_no_provenance(self) -> None:
        """Should load legacy documents with None provenance."""
        recipe = _doc_to_recipe("doc1", {"title": "Legacy", "ingredients": ["flour"]})

        assert recipe.source_ingredients is None
        assert recipe.source_ingredients_meta is None

    def test_doc_with_provenance_loads_it(self) -> None:
        """Should load stored provenance alongside unchanged authoritative ingredients."""
        data = {
            "title": "New",
            "ingredients": ["2 cups flour"],
            "source_ingredients": ["2 cups  flour"],
            "source_ingredients_meta": _meta_dict(line_count=1),
        }

        recipe = _doc_to_recipe("doc1", data)

        assert recipe.ingredients == ["2 cups flour"]
        assert recipe.source_ingredients == ["2 cups  flour"]
        assert recipe.source_ingredients_meta is not None
        assert recipe.source_ingredients_meta.line_count == 1

    def test_malformed_provenance_does_not_break_load(self) -> None:
        """Should ignore malformed provenance instead of failing."""
        data = {"title": "Bad", "source_ingredients": [1, 2], "source_ingredients_meta": {"x": 1}}

        recipe = _doc_to_recipe("doc1", data)

        assert recipe.source_ingredients is None
        assert recipe.source_ingredients_meta is None

    def test_provenance_excluded_from_serialization(self) -> None:
        """Should not expose provenance in API/JSON output."""
        recipe = Recipe(
            id="r1",
            title="T",
            url="https://example.com",
            source_ingredients=["a"],
            source_ingredients_meta=SourceIngredientsMeta(**_meta_dict(line_count=1)),
        )

        assert "source_ingredients" not in recipe.model_dump()
        assert "source_ingredients_meta" not in recipe.model_dump()
        assert "source_ingredients" not in recipe.model_dump_json()

    def test_meta_is_frozen(self) -> None:
        """Should reject mutation of provenance metadata."""
        meta = SourceIngredientsMeta(**_meta_dict())

        with pytest.raises(ValidationError):
            meta.line_count = 5  # type: ignore[misc]

    def test_client_models_ignore_source_keys(self) -> None:
        """Should not accept provenance through client-facing create/update models."""
        payload = {"title": "T", "url": "https://example.com", "source_ingredients": ["x"]}

        assert "source_ingredients" not in RecipeCreate.model_validate(payload).model_dump()
        assert "source_ingredients" not in RecipeUpdate.model_validate(payload).model_dump()


class TestSaveRecipeSourceIngredients:
    """Tests for source provenance persistence in save_recipe and other write paths."""

    def test_writes_provenance_on_new_document(self) -> None:
        """Should write lines and meta on a new recipe and return them."""
        doc_ref = MagicMock()
        doc_ref.id = "new_id"
        capture = _capture(["2 ½ dl grädde"])
        recipe = RecipeCreate(title="T", url="https://example.com", ingredients=["2 1/2 dl grädde"])

        with patch("api.storage.recipe_storage.get_firestore_client", return_value=_mock_db(doc_ref)):
            result = save_recipe(recipe, source_ingredients=capture)

        written = doc_ref.set.call_args[0][0]
        assert written["ingredients"] == ["2 1/2 dl grädde"]
        assert written["source_ingredients"] == ["2 ½ dl grädde"]
        assert written["source_ingredients_meta"] == capture.meta.model_dump()
        assert result.source_ingredients == ["2 ½ dl grädde"]
        assert result.source_ingredients_meta == capture.meta
        assert result.ingredients == ["2 1/2 dl grädde"]

    def test_omits_provenance_when_not_provided(self) -> None:
        """Should not write provenance keys for manual creates."""
        doc_ref = MagicMock()
        doc_ref.id = "new_id"

        with patch("api.storage.recipe_storage.get_firestore_client", return_value=_mock_db(doc_ref)):
            result = save_recipe(RecipeCreate(title="T", url="https://example.com", ingredients=["a"]))

        written = doc_ref.set.call_args[0][0]
        assert "source_ingredients" not in written
        assert "source_ingredients_meta" not in written
        assert result.source_ingredients is None

    def test_enhancement_resave_preserves_existing_provenance(self) -> None:
        """Should never overwrite provenance on an existing document and should return the stored values."""
        doc_ref = MagicMock()
        doc_ref.id = "existing_id"
        existing = MagicMock()
        existing.exists = True
        existing.to_dict.return_value = {
            "title": "Orig",
            "ingredients": ["flour"],
            "instructions": [],
            "created_at": _CAPTURED_AT,
            "source_ingredients": ["1 c. flour"],
            "source_ingredients_meta": _meta_dict(line_count=1),
        }
        doc_ref.get.return_value = existing
        enhanced = RecipeCreate(title="Better", url="https://example.com", ingredients=["120 g flour"])

        with patch("api.storage.recipe_storage.get_firestore_client", return_value=_mock_db(doc_ref)):
            result = save_recipe(
                enhanced,
                recipe_id="existing_id",
                enhancement=EnhancementMetadata(enhanced=True, enhanced_at=_CAPTURED_AT),
                source_ingredients=_capture(["should not be written"]),
            )

        written = doc_ref.set.call_args[0][0]
        assert "source_ingredients" not in written
        assert "source_ingredients_meta" not in written
        assert doc_ref.set.call_args[1]["merge"] is True
        assert result.source_ingredients == ["1 c. flour"]
        assert result.ingredients == ["120 g flour"]

    def test_update_recipe_does_not_touch_provenance(self) -> None:
        """Should leave provenance out of user edits to ingredients."""
        doc_ref = MagicMock()
        stored = {
            "title": "T",
            "household_id": "h1",
            "ingredients": ["flour"],
            "source_ingredients": ["flour"],
            "source_ingredients_meta": _meta_dict(line_count=1),
        }
        doc_ref.get.return_value = MagicMock(exists=True, id="r1", to_dict=MagicMock(return_value=stored))

        with patch("api.storage.recipe_storage.get_firestore_client", return_value=_mock_db(doc_ref)):
            update_recipe("r1", RecipeUpdate(ingredients=["sugar"]), household_id="h1")

        update_data = doc_ref.update.call_args[0][0]
        assert update_data["ingredients"] == ["sugar"]
        assert "source_ingredients" not in update_data
        assert "source_ingredients_meta" not in update_data

    def test_remove_enhancement_keeps_provenance(self) -> None:
        """Should not delete provenance when restoring the original recipe."""
        doc_ref = MagicMock()
        stored = {
            "title": "Enhanced",
            "household_id": "h1",
            "enhanced": True,
            "ingredients": ["120 g flour"],
            "original": {"title": "Orig", "ingredients": ["1 c. flour"], "instructions": []},
            "source_ingredients": ["1 c. flour"],
            "source_ingredients_meta": _meta_dict(line_count=1),
        }
        doc_ref.get.return_value = MagicMock(exists=True, to_dict=MagicMock(return_value=stored))

        with patch("api.storage.recipe_storage.get_firestore_client", return_value=_mock_db(doc_ref)):
            result = remove_enhancement("r1", household_id="h1")

        update_data = doc_ref.update.call_args[0][0]
        assert "source_ingredients" not in update_data
        assert "source_ingredients_meta" not in update_data
        assert result is not None
        assert result.source_ingredients == ["1 c. flour"]
