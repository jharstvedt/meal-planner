"""Tests for the structured ingredient backfill script and shared status classification."""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import FailedPrecondition
from google.cloud.firestore_v1 import DELETE_FIELD

from api.services.structured_ingredient_parser import compute_ingredients_hash
from api.storage.structured_ingredients import (
    StructuredStatus,
    build_structured_fields,
    structured_fields_for_write,
    structured_ingredients_status,
)
from scripts.backfill_structured_ingredients import Outcome, main, plan_recipe, run_backfill

_PARSE_TARGET = "api.storage.structured_ingredients.parse_ingredient_list"


def _doc(lines: object, *, structured: bool = True, **overrides: object) -> dict:
    """Build a recipe doc; structured fields are derived from `lines` before overrides apply."""
    data: dict = {"title": "T", "ingredients": lines}
    if structured and isinstance(lines, list):
        fields = build_structured_fields(cast("list[str]", lines))
        assert fields is not None
        data.update(fields)
    data.update(overrides)
    return data


def _snapshot(recipe_id: str, data: dict | None) -> MagicMock:
    snap = MagicMock()
    snap.id = recipe_id
    snap.exists = data is not None
    snap.to_dict.return_value = data
    return snap


def _db_with_pages(*pages: list[MagicMock]) -> MagicMock:
    db = MagicMock()
    query = db.collection.return_value.order_by.return_value.limit.return_value
    query.stream.return_value = iter(pages[0])
    query.start_after.return_value.stream.side_effect = [iter(page) for page in pages[1:]]
    return db


class TestStructuredStatus:
    """Tests for structured_ingredients_status and its effect on normal writes."""

    def test_current(self) -> None:
        assert structured_ingredients_status(["salt"], _doc(["salt"])) is StructuredStatus.CURRENT

    def test_missing(self) -> None:
        assert structured_ingredients_status(["salt"], _doc(["salt"], structured=False)) is StructuredStatus.MISSING

    def test_stale_hash(self) -> None:
        data = _doc(["salt"], ingredients=["pepper"])
        assert structured_ingredients_status(["pepper"], data) is StructuredStatus.STALE

    def test_stale_parser_version(self) -> None:
        data = _doc(["salt"])
        data["structured_ingredients_meta"] = {**data["structured_ingredients_meta"], "parser_version": "0.0.1"}
        assert structured_ingredients_status(["salt"], data) is StructuredStatus.STALE

    @pytest.mark.parametrize(
        "overrides",
        [
            {"structured_ingredients": None},
            {"structured_ingredients_meta": None},
            {"structured_ingredients": [{"bad": 1}]},
            {"structured_ingredients": "garbage"},
        ],
    )
    def test_malformed(self, overrides: dict) -> None:
        data = {**_doc(["salt"]), **overrides}
        assert structured_ingredients_status(["salt"], data) is StructuredStatus.MALFORMED

    def test_items_not_matching_lines_is_malformed(self) -> None:
        """Valid meta with items that don't correspond to the lines must not be trusted."""
        data = _doc(["salt"])
        data["structured_ingredients"] = []
        assert structured_ingredients_status(["salt"], data) is StructuredStatus.MALFORMED

    def test_normal_write_repairs_malformed_items(self) -> None:
        """Persistence regenerates when meta is current but items are corrupt."""
        data = _doc(["salt"])
        data["structured_ingredients"] = [{"bad": 1}]
        fields = structured_fields_for_write(["salt"], data)
        assert [i["raw_text"] for i in fields["structured_ingredients"]] == ["salt"]


class TestPlanRecipe:
    """Tests for per-recipe backfill decisions."""

    def test_current_is_left_alone(self) -> None:
        data = _doc(["salt"])
        with patch(_PARSE_TARGET) as mock_parse:
            plan = plan_recipe("r1", data)
        mock_parse.assert_not_called()
        assert plan.outcome is Outcome.CURRENT
        assert plan.fields == {}

    def test_missing_is_generated(self) -> None:
        plan = plan_recipe("r1", _doc(["1 cup flour", "salt"], structured=False))
        assert plan.outcome is Outcome.UPDATE
        assert plan.status is StructuredStatus.MISSING
        assert set(plan.fields) == {"structured_ingredients", "structured_ingredients_meta"}
        assert plan.fields["structured_ingredients_meta"]["ingredients_hash"] == compute_ingredients_hash(
            ["1 cup flour", "salt"]
        )

    def test_legacy_source_text_is_not_invented(self) -> None:
        plan = plan_recipe("r1", _doc(["salt"], structured=False))
        assert all(item["source_text"] is None for item in plan.fields["structured_ingredients"])

    def test_stale_preserves_ids_of_unchanged_lines(self) -> None:
        data = _doc(["salt", "1 cup flour"])
        old_ids = {i["raw_text"]: i["id"] for i in data["structured_ingredients"]}
        data["ingredients"] = ["salt", "2 eggs"]

        plan = plan_recipe("r1", data)

        assert plan.status is StructuredStatus.STALE
        items = plan.fields["structured_ingredients"]
        assert items[0]["id"] == old_ids["salt"]
        assert items[1]["id"] not in old_ids.values()

    def test_plan_only_touches_structured_fields(self) -> None:
        plan = plan_recipe("r1", _doc(["salt"], structured=False))
        assert set(plan.fields) == {"structured_ingredients", "structured_ingredients_meta"}

    @pytest.mark.parametrize(
        ("ingredients", "detail"),
        [(None, "missing ingredients field"), ("salt", "ingredients is str"), (["salt", 3], "non-string")],
    )
    def test_invalid_recipes_are_skipped(self, ingredients: object, detail: str) -> None:
        plan = plan_recipe("r1", {"title": "T", "ingredients": ingredients})
        assert plan.outcome is Outcome.INVALID
        assert detail in plan.detail
        assert plan.fields == {}

    def test_parse_failure_on_missing_writes_nothing(self) -> None:
        with patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
            plan = plan_recipe("r1", _doc(["salt"], structured=False))
        assert plan.outcome is Outcome.PARSE_FAILED
        assert plan.fields == {}

    def test_parse_failure_on_stale_clears_structure(self) -> None:
        data = _doc(["salt"], ingredients=["pepper"])
        with patch(_PARSE_TARGET, side_effect=RuntimeError("boom")):
            plan = plan_recipe("r1", data)
        assert plan.outcome is Outcome.CLEAR
        assert plan.fields == {"structured_ingredients": DELETE_FIELD, "structured_ingredients_meta": DELETE_FIELD}

    def test_empty_ingredient_list_is_structured(self) -> None:
        plan = plan_recipe("r1", _doc([], structured=False))
        assert plan.outcome is Outcome.UPDATE
        assert plan.fields["structured_ingredients"] == []


class TestRunBackfill:
    """Tests for the scan/apply loop."""

    def _mixed_db(self) -> tuple[MagicMock, list[MagicMock]]:
        snaps = [
            _snapshot("current", _doc(["salt"])),
            _snapshot("missing", _doc(["salt"], structured=False)),
            _snapshot("stale", _doc(["salt"], ingredients=["pepper"])),
            _snapshot("invalid", {"title": "Bad", "ingredients": "salt"}),
        ]
        return _db_with_pages(snaps), snaps

    def test_dry_run_counts_and_never_writes(self) -> None:
        db, snaps = self._mixed_db()

        stats = run_backfill(db)

        assert (stats.scanned, stats.current, stats.missing, stats.stale, stats.invalid) == (4, 1, 1, 1, 1)
        assert stats.to_write == 2
        assert stats.written == 0
        for snap in snaps:
            snap.reference.update.assert_not_called()
            snap.reference.set.assert_not_called()

    def test_apply_writes_only_structured_fields_with_precondition(self) -> None:
        db, snaps = self._mixed_db()

        stats = run_backfill(db, apply=True)

        assert stats.written == 2
        snaps[0].reference.update.assert_not_called()
        snaps[3].reference.update.assert_not_called()
        for snap in snaps[1:3]:
            payload = snap.reference.update.call_args[0][0]
            assert set(payload) == {"structured_ingredients", "structured_ingredients_meta"}
            db.write_option.assert_any_call(last_update_time=snap.update_time)

    def test_apply_is_idempotent(self) -> None:
        """Documents produced by an apply run are classified current on the next run."""
        db, snaps = self._mixed_db()
        run_backfill(db, apply=True)
        rerun = [_snapshot(s.id, {**s.to_dict.return_value, **s.reference.update.call_args[0][0]}) for s in snaps[1:3]]

        stats = run_backfill(_db_with_pages(rerun), apply=True)

        assert (stats.current, stats.to_write, stats.written) == (2, 0, 0)

    def test_conflicts_and_write_errors_do_not_stop_run(self) -> None:
        snaps = [_snapshot(f"r{i}", _doc(["salt"], structured=False)) for i in range(3)]
        snaps[0].reference.update.side_effect = FailedPrecondition("changed")
        snaps[1].reference.update.side_effect = RuntimeError("boom")

        stats = run_backfill(_db_with_pages(snaps), apply=True)

        assert (stats.conflicts, stats.write_errors, stats.written) == (1, 1, 1)
        assert len(stats.attention) == 2

    def test_parse_failure_does_not_stop_run(self) -> None:
        snaps = [_snapshot("a", _doc(["salt"], structured=False)), _snapshot("b", _doc(["egg"], structured=False))]
        real_build = build_structured_fields

        def flaky(ingredients: list[str], previous: object = None) -> dict | None:
            return None if ingredients == ["salt"] else real_build(ingredients, previous)

        with patch("scripts.backfill_structured_ingredients.build_structured_fields", side_effect=flaky):
            stats = run_backfill(_db_with_pages(snaps), apply=True)

        assert (stats.parse_failed, stats.written) == (1, 1)
        snaps[0].reference.update.assert_not_called()

    def test_pages_through_collection(self) -> None:
        first = [_snapshot(f"r{i}", _doc(["salt"])) for i in range(2)]
        second = [_snapshot("r2", _doc(["salt"]))]
        db = _db_with_pages(first, second)

        stats = run_backfill(db, page_size=2)

        assert stats.scanned == 3
        db.collection.return_value.order_by.return_value.limit.return_value.start_after.assert_called_once_with(
            first[-1]
        )

    def test_limit_stops_scan(self) -> None:
        snaps = [_snapshot(f"r{i}", _doc(["salt"])) for i in range(3)]
        assert run_backfill(_db_with_pages(snaps), limit=2).scanned == 2

    def test_recipe_ids_target_specific_documents(self) -> None:
        db = MagicMock()
        db.collection.return_value.document.return_value.get.side_effect = [
            _snapshot("a", _doc(["salt"], structured=False)),
            _snapshot("missing", None),
        ]

        stats = run_backfill(db, recipe_ids=["a", "missing"])

        assert (stats.scanned, stats.missing, stats.not_found) == (2, 1, 1)
        db.collection.return_value.order_by.assert_not_called()


class TestMain:
    """Tests for CLI guards."""

    def test_requires_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        assert main([]) == 2

    def test_apply_requires_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("builtins.input", lambda _prompt: "wrong")
        with patch("scripts.backfill_structured_ingredients.firestore.Client") as client:
            assert main(["--project", "p", "--apply"]) == 1
        client.assert_not_called()

    def test_dry_run_is_default(self) -> None:
        with (
            patch("scripts.backfill_structured_ingredients.firestore.Client"),
            patch("scripts.backfill_structured_ingredients.run_backfill") as run,
        ):
            run.return_value.attention = []
            run.return_value.to_write = 0
            assert main(["--project", "p"]) == 0
        assert run.call_args.kwargs["apply"] is False
