"""Tests for structured ingredient maintenance in admin tools and scripts that write Firestore directly."""

import warnings
from unittest.mock import MagicMock, patch

from google.cloud.firestore_v1 import DELETE_FIELD

from api.services.structured_ingredient_parser import compute_ingredients_hash
from api.storage.structured_ingredients import build_structured_fields
from tools import recipe_manager

with warnings.catch_warnings():
    # google-genai triggers a Python 3.14 typing DeprecationWarning at import time (third-party, not ours)
    warnings.filterwarnings("ignore", message=".*_UnionGenericAlias.*", category=DeprecationWarning)
    from scripts import recipe_enhancer

_PARSE_TARGET = "api.storage.structured_ingredients.parse_ingredient_list"


def _mock_db(existing: dict | None) -> tuple[MagicMock, MagicMock]:
    mock_db = MagicMock()
    doc_ref = MagicMock()
    doc = MagicMock()
    doc.exists = existing is not None
    doc.to_dict.return_value = existing
    doc_ref.get.return_value = doc
    mock_db.collection.return_value.document.return_value = doc_ref
    return mock_db, doc_ref


def _stored_doc(ingredients: list[str]) -> dict:
    fields = build_structured_fields(ingredients)
    assert fields is not None
    return {"title": "T", "ingredients": ingredients, "instructions": [], **fields}


class TestRecipeManagerUpdate:
    """Tests for tools/recipe_manager.update_recipe (admin enhancement upload path)."""

    def _run(self, existing: dict, updates: dict, *, parse_error: bool = False) -> dict:
        mock_db, doc_ref = _mock_db(existing)
        with (
            patch.object(recipe_manager, "get_db", return_value=mock_db),
            patch.object(recipe_manager, "mark_processed"),
        ):
            if parse_error:
                with patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
                    recipe_manager.update_recipe("recipe1", updates)
            else:
                recipe_manager.update_recipe("recipe1", updates)
        return doc_ref.update.call_args[0][0]

    def test_new_ingredients_regenerate_and_reuse_ids(self) -> None:
        """Enhanced ingredients get fresh structure; unchanged lines keep their IDs."""
        existing = _stored_doc(["salt", "1 cup flour"])
        old_ids = {i["raw_text"]: i["id"] for i in existing["structured_ingredients"]}

        payload = self._run(existing, {"ingredients": ["salt", "2 cups sugar"]})

        items = payload["structured_ingredients"]
        assert [i["raw_text"] for i in items] == ["salt", "2 cups sugar"]
        assert items[0]["id"] == old_ids["salt"]
        assert items[1]["id"] not in old_ids.values()
        assert payload["structured_ingredients_meta"]["ingredients_hash"] == compute_ingredients_hash(
            ["salt", "2 cups sugar"]
        )

    def test_unchanged_current_ingredients_leave_structure_untouched(self) -> None:
        """Non-ingredient updates on a current document do not rewrite structured fields."""
        payload = self._run(_stored_doc(["salt"]), {"title": "New title"})

        assert "structured_ingredients" not in payload
        assert "structured_ingredients_meta" not in payload

    def test_legacy_document_gains_structure_on_update(self) -> None:
        """A legacy document without structure is populated from its current ingredients."""
        existing = {"title": "T", "ingredients": ["salt"], "instructions": []}

        payload = self._run(existing, {"title": "New title"})

        assert [i["raw_text"] for i in payload["structured_ingredients"]] == ["salt"]

    def test_caller_supplied_structured_fields_are_stripped(self) -> None:
        """Structured fields are derived by persistence and cannot be injected from upload JSON."""
        payload = self._run(
            _stored_doc(["salt"]),
            {
                "ingredients": ["pepper"],
                "structured_ingredients": [{"id": "evil"}],
                "structured_ingredients_meta": {"ingredients_hash": "x"},
            },
        )

        assert [i["raw_text"] for i in payload["structured_ingredients"]] == ["pepper"]
        assert payload["structured_ingredients"][0]["id"] != "evil"
        assert payload["structured_ingredients_meta"]["ingredients_hash"] == compute_ingredients_hash(["pepper"])

    def test_parser_failure_deletes_stale_structure_and_still_saves(self) -> None:
        """Parsing failure never blocks the update and never leaves stale structure behind."""
        payload = self._run(_stored_doc(["salt"]), {"ingredients": ["pepper"]}, parse_error=True)

        assert payload["ingredients"] == ["pepper"]
        assert payload["structured_ingredients"] is DELETE_FIELD
        assert payload["structured_ingredients_meta"] is DELETE_FIELD


class TestRecipeEnhancerScriptSave:
    """Tests for scripts/recipe_enhancer.save_recipe."""

    def test_save_regenerates_structure_with_stable_ids(self) -> None:
        """The legacy enhancer script keeps structured fields in sync with the ingredients it writes."""
        existing = _stored_doc(["salt"])
        old_id = existing["structured_ingredients"][0]["id"]
        mock_db, doc_ref = _mock_db(existing)

        with patch.object(recipe_enhancer, "get_firestore_client", return_value=mock_db):
            assert recipe_enhancer.save_recipe("recipe1", {"ingredients": ["salt", "pepper"]})

        data = doc_ref.set.call_args[0][0]
        assert [i["raw_text"] for i in data["structured_ingredients"]] == ["salt", "pepper"]
        assert data["structured_ingredients"][0]["id"] == old_id

    def test_parser_failure_deletes_stale_structure(self) -> None:
        """Parser failure still saves and removes stale structure."""
        mock_db, doc_ref = _mock_db(_stored_doc(["salt"]))

        with (
            patch.object(recipe_enhancer, "get_firestore_client", return_value=mock_db),
            patch(_PARSE_TARGET, side_effect=RuntimeError("boom")),
        ):
            assert recipe_enhancer.save_recipe("recipe1", {"ingredients": ["pepper"]})

        data = doc_ref.set.call_args[0][0]
        assert data["ingredients"] == ["pepper"]
        assert data["structured_ingredients"] is DELETE_FIELD
        assert data["structured_ingredients_meta"] is DELETE_FIELD
