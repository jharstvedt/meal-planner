"""Tests for structured ingredient persistence on recipe write paths."""

from unittest.mock import MagicMock, patch

import ingredient_parser
import pytest
from google.cloud.firestore_v1 import DELETE_FIELD

from api.models.recipe import Recipe, RecipeCreate, RecipeUpdate
from api.services.structured_ingredient_parser import PARSER_NAME, WRAPPER_VERSION, compute_ingredients_hash
from api.storage.recipe_storage import (
    EnhancementMetadata,
    _doc_to_recipe,
    remove_enhancement,
    save_recipe,
    update_recipe,
)
from api.storage.structured_ingredients import (
    assign_stable_ids,
    build_structured_fields,
    is_structured_ingredients_current,
    load_structured_fields,
)

_PARSE_TARGET = "api.storage.structured_ingredients.parse_ingredient_list"
_CLIENT_TARGET = "api.storage.recipe_storage.get_firestore_client"


def _mock_db(existing: dict | None = None) -> tuple[MagicMock, MagicMock]:
    mock_db = MagicMock()
    doc_ref = MagicMock()
    doc_ref.id = "recipe1"
    doc = MagicMock()
    doc.exists = existing is not None
    doc.to_dict.return_value = existing
    doc_ref.get.return_value = doc
    mock_db.collection.return_value.document.return_value = doc_ref
    return mock_db, doc_ref


def _stored_doc(ingredients: list[str], **overrides: object) -> dict:
    fields = build_structured_fields(ingredients)
    assert fields is not None
    return {"title": "T", "url": "", "ingredients": ingredients, "instructions": [], **fields, **overrides}


def _ids_by_text(items: list[dict]) -> list[tuple[str, str]]:
    return [(item["raw_text"], item["id"]) for item in items]


class TestHelpers:
    """Tests for the pure persistence helpers."""

    def test_current_meta_matches(self) -> None:
        """Fresh metadata should be current for the same lines."""
        fields = build_structured_fields(["1 cup flour"])
        assert fields is not None
        assert is_structured_ingredients_current(["1 cup flour"], fields["structured_ingredients_meta"])

    @pytest.mark.parametrize("meta", [None, {}, {"parser_name": "x"}, "garbage"])
    def test_missing_or_invalid_meta_is_stale(self, meta: object) -> None:
        """Missing or malformed metadata should never be treated as current."""
        assert not is_structured_ingredients_current(["1 cup flour"], meta)  # type: ignore[arg-type]

    def test_duplicate_lines_reuse_each_old_id_once(self) -> None:
        """Duplicates should consume previous IDs in order, then get new IDs."""
        previous = [{"id": "a", "raw_text": "salt"}, {"id": "b", "raw_text": "salt"}]
        ids = assign_stable_ids(["salt", "salt", "salt"], previous)
        assert ids[:2] == ["a", "b"]
        assert ids[2] not in {"a", "b"}

    def test_temporary_and_repeated_previous_ids_are_ignored(self) -> None:
        """Placeholder or duplicated stored IDs must not be reused."""
        previous = [{"id": "tmp_0", "raw_text": "salt"}, {"id": "a", "raw_text": "egg"}, {"id": "a", "raw_text": "egg"}]
        ids = assign_stable_ids(["salt", "egg", "egg"], previous)
        assert ids[0] != "tmp_0"
        assert ids[1] == "a"
        assert ids[2] != "a"

    def test_malformed_stored_fields_load_as_none(self) -> None:
        """Malformed stored structured data should not break recipe reads."""
        loaded = load_structured_fields({"structured_ingredients": [{"bad": 1}], "structured_ingredients_meta": {}})
        assert loaded == {"structured_ingredients": None, "structured_ingredients_meta": None}


class TestSaveRecipeStructured:
    """Tests for structured fields on save_recipe."""

    def test_new_recipe_gets_structured_fields(self) -> None:
        """Creating a recipe should persist structured fields with a matching hash and stable IDs."""
        mock_db, doc_ref = _mock_db()
        recipe = RecipeCreate(title="T", url="", ingredients=["1 cup flour", "salt", "salt"])

        with patch(_CLIENT_TARGET, return_value=mock_db):
            result = save_recipe(recipe)

        data = doc_ref.set.call_args[0][0]
        items, meta = data["structured_ingredients"], data["structured_ingredients_meta"]
        assert [item["raw_text"] for item in items] == ["1 cup flour", "salt", "salt"]
        assert meta["ingredients_hash"] == compute_ingredients_hash(data["ingredients"])
        assert meta["parser_name"] == PARSER_NAME
        assert meta["wrapper_version"] == WRAPPER_VERSION
        ids = [item["id"] for item in items]
        assert len(set(ids)) == len(ids)
        assert not any(i.startswith("tmp_") for i in ids)
        assert result.structured_ingredients is not None
        assert [i.id for i in result.structured_ingredients] == ids

    def test_structured_fields_not_serialized(self) -> None:
        """Structured fields stay internal and out of API/Gemini serialization."""
        mock_db, _ = _mock_db()
        with patch(_CLIENT_TARGET, return_value=mock_db):
            result = save_recipe(RecipeCreate(title="T", url="", ingredients=["salt"]))
        dumped = result.model_dump()
        assert "structured_ingredients" not in dumped
        assert "structured_ingredients_meta" not in dumped

    def test_parser_failure_does_not_fail_create(self) -> None:
        """A full parser failure should still save the recipe without structured fields."""
        mock_db, doc_ref = _mock_db()
        with patch(_CLIENT_TARGET, return_value=mock_db), patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
            result = save_recipe(RecipeCreate(title="T", url="", ingredients=["salt"]))

        data = doc_ref.set.call_args[0][0]
        assert data["ingredients"] == ["salt"]
        assert "structured_ingredients" not in data
        assert "structured_ingredients_meta" not in data
        assert result.structured_ingredients is None

    def test_enhanced_save_reconciles_ids(self) -> None:
        """Enhanced save with a recipe_id should reuse IDs of unchanged lines."""
        existing = _stored_doc(["salt", "1 cup flour"], created_at=None)
        old_ids = dict(_ids_by_text(existing["structured_ingredients"]))
        mock_db, doc_ref = _mock_db(existing)
        recipe = RecipeCreate(title="T", url="", ingredients=["salt", "2 eggs"])

        with patch(_CLIENT_TARGET, return_value=mock_db):
            save_recipe(recipe, recipe_id="recipe1", enhancement=EnhancementMetadata(enhanced=True))

        items = doc_ref.set.call_args[0][0]["structured_ingredients"]
        assert items[0]["id"] == old_ids["salt"]
        assert items[1]["id"] not in old_ids.values()

    def test_recipe_id_save_parser_failure_deletes_stale_fields(self) -> None:
        """Overwriting an existing doc with new ingredients must not leave stale structure on failure."""
        mock_db, doc_ref = _mock_db(_stored_doc(["salt"]))
        with patch(_CLIENT_TARGET, return_value=mock_db), patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
            result = save_recipe(RecipeCreate(title="T", url="", ingredients=["pepper"]), recipe_id="recipe1")

        data = doc_ref.set.call_args[0][0]
        assert data["structured_ingredients"] is DELETE_FIELD
        assert data["structured_ingredients_meta"] is DELETE_FIELD
        assert result.structured_ingredients is None


class TestUpdateRecipeStructured:
    """Tests for structured fields on update_recipe."""

    def _update(self, existing: dict, updates: RecipeUpdate, *, fail: bool = False) -> dict:
        mock_db, doc_ref = _mock_db(existing)
        with patch(_CLIENT_TARGET, return_value=mock_db), patch("api.storage.recipe_storage.get_recipe"):
            if fail:
                with patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
                    update_recipe("recipe1", updates)
            else:
                update_recipe("recipe1", updates)
        return doc_ref.update.call_args[0][0]

    def test_unrelated_update_preserves_structured_data(self) -> None:
        """Updating a non-ingredient field should not reparse current structured data."""
        existing = _stored_doc(["salt"])
        with patch(_PARSE_TARGET) as mock_parse:
            data = self._update(existing, RecipeUpdate(title="New"))
        mock_parse.assert_not_called()
        assert "structured_ingredients" not in data
        assert "structured_ingredients_meta" not in data

    def test_ingredient_changes_reconcile_ids(self) -> None:
        """Unchanged lines keep IDs, added lines get new IDs, removed lines disappear."""
        existing = _stored_doc(["salt", "1 cup flour", "2 eggs"])
        old_ids = dict(_ids_by_text(existing["structured_ingredients"]))

        data = self._update(existing, RecipeUpdate(ingredients=["2 eggs", "salt", "1 tbsp butter"]))

        items = data["structured_ingredients"]
        assert [i["raw_text"] for i in items] == ["2 eggs", "salt", "1 tbsp butter"]
        assert items[0]["id"] == old_ids["2 eggs"]
        assert items[1]["id"] == old_ids["salt"]
        assert items[2]["id"] not in old_ids.values()
        assert old_ids["1 cup flour"] not in {i["id"] for i in items}
        assert data["structured_ingredients_meta"]["ingredients_hash"] == compute_ingredients_hash(
            ["2 eggs", "salt", "1 tbsp butter"]
        )

    def test_duplicate_reconciliation_never_reuses_id_twice(self) -> None:
        """Adding another duplicate should produce a fresh ID rather than reusing one."""
        existing = _stored_doc(["salt", "salt"])
        old_ids = [i["id"] for i in existing["structured_ingredients"]]

        data = self._update(existing, RecipeUpdate(ingredients=["salt", "salt", "salt"]))

        ids = [i["id"] for i in data["structured_ingredients"]]
        assert ids[:2] == old_ids
        assert len(set(ids)) == 3

    def test_changed_line_regenerates(self) -> None:
        """An edited line should be reparsed and receive a new ID."""
        existing = _stored_doc(["1 cup flour"])
        old_id = existing["structured_ingredients"][0]["id"]

        data = self._update(existing, RecipeUpdate(ingredients=["2 cups flour"]))

        item = data["structured_ingredients"][0]
        assert item["raw_text"] == "2 cups flour"
        assert item["id"] != old_id
        assert item["measurements"][0]["quantity"] == pytest.approx(2.0)

    def test_stale_hash_regenerates_on_unrelated_update(self) -> None:
        """A hash mismatch should regenerate from stored ingredients, keeping matching IDs."""
        existing = _stored_doc(["salt"])
        existing["structured_ingredients_meta"]["ingredients_hash"] = "stale"
        old_id = existing["structured_ingredients"][0]["id"]

        data = self._update(existing, RecipeUpdate(title="New"))

        assert data["structured_ingredients_meta"]["ingredients_hash"] == compute_ingredients_hash(["salt"])
        assert data["structured_ingredients"][0]["id"] == old_id

    @pytest.mark.parametrize("field", ["parser_version", "wrapper_version"])
    def test_version_mismatch_regenerates(self, field: str) -> None:
        """Parser or wrapper version mismatch should count as stale."""
        existing = _stored_doc(["salt"])
        existing["structured_ingredients_meta"][field] = "0-old"

        data = self._update(existing, RecipeUpdate(title="New"))

        meta = data["structured_ingredients_meta"]
        assert meta["parser_version"] == ingredient_parser.__version__
        assert meta["wrapper_version"] == WRAPPER_VERSION

    def test_missing_structure_regenerates(self) -> None:
        """A legacy doc without structured data gains it on the next write."""
        data = self._update({"title": "T", "ingredients": ["salt"]}, RecipeUpdate(title="New"))
        assert data["structured_ingredients"][0]["raw_text"] == "salt"

    def test_parser_failure_on_ingredient_update_removes_stale_data(self) -> None:
        """A full parser failure while changing ingredients must delete old structured fields."""
        existing = _stored_doc(["salt"])
        data = self._update(existing, RecipeUpdate(ingredients=["pepper"]), fail=True)

        assert data["ingredients"] == ["pepper"]
        assert data["structured_ingredients"] is DELETE_FIELD
        assert data["structured_ingredients_meta"] is DELETE_FIELD


class TestLegacyAndRemoveEnhancement:
    """Tests for legacy compatibility and remove_enhancement consistency."""

    def test_legacy_recipe_without_fields_is_valid(self) -> None:
        """Docs without structured fields should still produce a valid Recipe."""
        recipe = _doc_to_recipe("r1", {"title": "T", "url": "", "ingredients": ["salt"]})
        assert isinstance(recipe, Recipe)
        assert recipe.structured_ingredients is None
        assert recipe.structured_ingredients_meta is None

    def test_stored_fields_load_into_model(self) -> None:
        """Valid stored structured fields should be loaded internally."""
        recipe = _doc_to_recipe("r1", _stored_doc(["salt"]))
        assert recipe.structured_ingredients is not None
        assert recipe.structured_ingredients[0].raw_text == "salt"

    def _enhanced_doc(self) -> dict:
        return _stored_doc(
            ["salt", "enhanced line"],
            enhanced=True,
            household_id="hh1",
            original={"title": "Orig", "ingredients": ["salt", "original line"], "instructions": []},
        )

    def test_remove_enhancement_regenerates_for_restored_ingredients(self) -> None:
        """Restoring originals should re-derive structure and reuse IDs for shared lines."""
        existing = self._enhanced_doc()
        old_ids = dict(_ids_by_text(existing["structured_ingredients"]))
        mock_db, doc_ref = _mock_db(existing)

        with patch(_CLIENT_TARGET, return_value=mock_db):
            result = remove_enhancement("recipe1", household_id="hh1")

        data = doc_ref.update.call_args[0][0]
        items = data["structured_ingredients"]
        assert [i["raw_text"] for i in items] == ["salt", "original line"]
        assert items[0]["id"] == old_ids["salt"]
        assert items[1]["id"] not in old_ids.values()
        assert data["structured_ingredients_meta"]["ingredients_hash"] == compute_ingredients_hash(
            ["salt", "original line"]
        )
        assert result is not None
        assert result.structured_ingredients is not None
        assert [i.id for i in result.structured_ingredients] == [i["id"] for i in items]

    def test_remove_enhancement_parser_failure_removes_stale_data(self) -> None:
        """A parser failure during restore must not leave enhanced-version structure behind."""
        mock_db, doc_ref = _mock_db(self._enhanced_doc())

        with patch(_CLIENT_TARGET, return_value=mock_db), patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
            result = remove_enhancement("recipe1", household_id="hh1")

        data = doc_ref.update.call_args[0][0]
        assert data["structured_ingredients"] is DELETE_FIELD
        assert data["structured_ingredients_meta"] is DELETE_FIELD
        assert result is not None
        assert result.ingredients == ["salt", "original line"]
        assert result.structured_ingredients is None
