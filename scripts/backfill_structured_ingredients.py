"""Backfill or refresh derived structured ingredients on existing recipes.

Reuses the Phase 0G persistence helpers so results match normal recipe writes:
current data is left alone, missing/stale/malformed data is regenerated with stable
IDs preserved for unchanged lines, and parse failures never touch `ingredients`.

Dry-run is the default; nothing is written without --apply.

Usage:
    uv run python scripts/backfill_structured_ingredients.py --project <project_id>
    uv run python scripts/backfill_structured_ingredients.py --project <project_id> --verbose
    uv run python scripts/backfill_structured_ingredients.py --project <project_id> --recipe-id abc123
    uv run python scripts/backfill_structured_ingredients.py --project <project_id> --apply
"""

import argparse
import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.api_core.exceptions import FailedPrecondition
from google.cloud import firestore
from google.cloud.firestore_v1 import DELETE_FIELD, DocumentSnapshot
from google.cloud.firestore_v1.field_path import FieldPath

from api.storage.firestore_client import DATABASE, RECIPES_COLLECTION
from api.storage.structured_ingredients import (
    STRUCTURED_INGREDIENTS_FIELD,
    STRUCTURED_INGREDIENTS_META_FIELD,
    StructuredStatus,
    build_structured_fields,
    structured_ingredients_status,
)

DEFAULT_PAGE_SIZE = 200
MAX_ATTENTION_ROWS = 50


class Outcome(StrEnum):
    """What the backfill does with a single recipe."""

    CURRENT = "current"
    UPDATE = "update"
    CLEAR = "clear"
    PARSE_FAILED = "parse_failed"
    INVALID = "invalid"


@dataclass(frozen=True)
class RecipePlan:
    """Planned backfill action for one recipe document."""

    recipe_id: str
    title: str
    outcome: Outcome
    status: StructuredStatus | None = None
    fields: dict = field(default_factory=dict)
    detail: str = ""
    line_count: int = 0
    partial_lines: tuple[str, ...] = ()


@dataclass
class BackfillStats:
    """Aggregate counts for a backfill run."""

    scanned: int = 0
    current: int = 0
    missing: int = 0
    stale: int = 0
    malformed: int = 0
    invalid: int = 0
    not_found: int = 0
    parse_failed: int = 0
    cleared: int = 0
    to_write: int = 0
    written: int = 0
    conflicts: int = 0
    write_errors: int = 0
    empty_ingredients: int = 0
    lines_total: int = 0
    partial_lines: int = 0
    recipes_with_partial: int = 0
    attention: list[tuple[str, str, str]] = field(default_factory=list)

    def flag(self, recipe_id: str, title: str, reason: str) -> None:
        self.attention.append((recipe_id, title, reason))


def _partial_lines(items: object) -> tuple[str, ...]:
    """Return raw lines the parser could not fully structure (partial or no food name)."""
    if not isinstance(items, list):
        return ()
    partial: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("parser_metadata") or {}
        if item.get("name") is None or (isinstance(meta, dict) and meta.get("is_partial")):
            partial.append(str(item.get("raw_text", "")))
    return tuple(partial)


def plan_recipe(recipe_id: str, data: dict) -> RecipePlan:
    """Decide what the backfill should write for one recipe, using the Phase 0G helpers."""
    title = str(data.get("title") or "")
    ingredients = data.get("ingredients")
    if not isinstance(ingredients, list):
        detail = "missing ingredients field" if ingredients is None else f"ingredients is {type(ingredients).__name__}"
        return RecipePlan(recipe_id, title, Outcome.INVALID, detail=detail)
    if not all(isinstance(line, str) for line in ingredients):
        bad = sorted({type(line).__name__ for line in ingredients if not isinstance(line, str)})
        return RecipePlan(recipe_id, title, Outcome.INVALID, detail=f"non-string ingredient entries: {bad}")

    status = structured_ingredients_status(ingredients, data)
    if status is StructuredStatus.CURRENT:
        return RecipePlan(
            recipe_id,
            title,
            Outcome.CURRENT,
            status,
            line_count=len(ingredients),
            partial_lines=_partial_lines(data.get(STRUCTURED_INGREDIENTS_FIELD)),
        )

    fields = build_structured_fields(ingredients, data.get(STRUCTURED_INGREDIENTS_FIELD))
    if fields is None:
        if status is StructuredStatus.MISSING:
            return RecipePlan(recipe_id, title, Outcome.PARSE_FAILED, status, line_count=len(ingredients))
        clear = {STRUCTURED_INGREDIENTS_FIELD: DELETE_FIELD, STRUCTURED_INGREDIENTS_META_FIELD: DELETE_FIELD}
        return RecipePlan(recipe_id, title, Outcome.CLEAR, status, clear, line_count=len(ingredients))

    return RecipePlan(
        recipe_id,
        title,
        Outcome.UPDATE,
        status,
        fields,
        line_count=len(ingredients),
        partial_lines=_partial_lines(fields[STRUCTURED_INGREDIENTS_FIELD]),
    )


def iter_recipes(
    db: firestore.Client, *, recipe_ids: list[str] | None = None, page_size: int = DEFAULT_PAGE_SIZE
) -> Iterator[tuple[str, DocumentSnapshot]]:
    """Yield recipe snapshots, paging by document ID so long scans don't hold a single stream open."""
    collection = db.collection(RECIPES_COLLECTION)
    if recipe_ids:
        for recipe_id in recipe_ids:
            yield recipe_id, collection.document(recipe_id).get()  # type: ignore[misc]
        return

    base = collection.order_by(FieldPath.document_id()).limit(page_size)
    last: DocumentSnapshot | None = None
    while True:
        page = list((base.start_after(last) if last else base).stream())
        for snapshot in page:
            yield snapshot.id, snapshot
        if len(page) < page_size:
            return
        last = page[-1]


def _record(stats: BackfillStats, plan: RecipePlan) -> None:
    status_counters = {
        StructuredStatus.CURRENT: "current",
        StructuredStatus.MISSING: "missing",
        StructuredStatus.STALE: "stale",
        StructuredStatus.MALFORMED: "malformed",
    }
    if plan.status is not None:
        setattr(stats, status_counters[plan.status], getattr(stats, status_counters[plan.status]) + 1)
    if plan.outcome is Outcome.INVALID:
        stats.invalid += 1
        stats.flag(plan.recipe_id, plan.title, f"skipped: {plan.detail}")
        return
    if plan.outcome in (Outcome.PARSE_FAILED, Outcome.CLEAR):
        stats.parse_failed += 1
        reason = "parse failed"
        if plan.outcome is Outcome.CLEAR:
            stats.cleared += 1
            reason += f"; {plan.status} structure will be removed"
        stats.flag(plan.recipe_id, plan.title, reason)
    if plan.status is StructuredStatus.MALFORMED:
        stats.flag(plan.recipe_id, plan.title, "stored structured data malformed; regenerating")
    if plan.fields:
        stats.to_write += 1
    if plan.line_count == 0:
        stats.empty_ingredients += 1
    stats.lines_total += plan.line_count
    if plan.partial_lines:
        stats.partial_lines += len(plan.partial_lines)
        stats.recipes_with_partial += 1


def _write(db: firestore.Client, snapshot: DocumentSnapshot, plan: RecipePlan, stats: BackfillStats) -> None:
    """Write only the structured fields, refusing if the recipe changed since it was read."""
    try:
        snapshot.reference.update(plan.fields, option=db.write_option(last_update_time=snapshot.update_time))
        stats.written += 1
    except FailedPrecondition:
        stats.conflicts += 1
        stats.flag(plan.recipe_id, plan.title, "changed during backfill; skipped (rerun to pick up)")
    except Exception as exc:
        stats.write_errors += 1
        stats.flag(plan.recipe_id, plan.title, f"write failed: {exc}")


def run_backfill(  # noqa: PLR0913
    db: firestore.Client,
    *,
    apply: bool = False,
    recipe_ids: list[str] | None = None,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    verbose: bool = False,
) -> BackfillStats:
    """Scan recipes and plan (or apply) structured-ingredient updates."""
    stats = BackfillStats()
    for recipe_id, snapshot in iter_recipes(db, recipe_ids=recipe_ids, page_size=page_size):
        if limit is not None and stats.scanned >= limit:
            break
        stats.scanned += 1
        if not snapshot.exists:
            stats.not_found += 1
            stats.flag(recipe_id, "", "recipe not found")
            continue

        plan = plan_recipe(recipe_id, snapshot.to_dict() or {})
        _record(stats, plan)

        if verbose and plan.outcome is not Outcome.CURRENT:
            label = f"{plan.outcome}" + (f" ({plan.status})" if plan.status else "")
            print(f"  {recipe_id}: {label} - {plan.title[:60]}")
        if verbose and plan.partial_lines:
            for line in plan.partial_lines:
                print(f"      partial: {line!r}")

        if apply and plan.fields:
            _write(db, snapshot, plan, stats)
    return stats


def print_summary(stats: BackfillStats, *, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY RUN"
    print(f"\n=== Structured ingredient backfill ({mode}) ===")
    print(f"Scanned:                 {stats.scanned}")
    print(f"  Already current:       {stats.current}")
    print(f"  Missing structure:     {stats.missing}")
    print(f"  Stale structure:       {stats.stale}")
    print(f"  Malformed structure:   {stats.malformed}")
    print(f"  Invalid (skipped):     {stats.invalid}")
    print(f"  Not found:             {stats.not_found}")
    print(f"Parse failures:          {stats.parse_failed} (clearing stale structure on {stats.cleared})")
    print(f"{'Writes planned' if apply else 'Would update'}:{'':10}{stats.to_write}")
    if apply:
        print(f"  Written:               {stats.written}")
        print(f"  Conflicts (skipped):   {stats.conflicts}")
        print(f"  Write errors:          {stats.write_errors}")
    print("Parse quality:")
    print(f"  Ingredient lines:      {stats.lines_total}")
    print(f"  Partial/unnamed lines: {stats.partial_lines} across {stats.recipes_with_partial} recipes")
    print(f"  Empty ingredient list: {stats.empty_ingredients}")

    if stats.attention:
        print(f"\nNeeds attention ({len(stats.attention)}):")
        for recipe_id, title, reason in stats.attention[:MAX_ATTENTION_ROWS]:
            print(f"  {recipe_id}: {reason}" + (f" - {title[:50]}" if title else ""))
        if len(stats.attention) > MAX_ATTENTION_ROWS:
            print(f"  ... {len(stats.attention) - MAX_ATTENTION_ROWS} more (use --verbose)")

    if not apply and stats.to_write:
        print(f"\nRe-run with --apply to write {stats.to_write} recipes.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill derived structured ingredients on recipes")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT"), help="GCP project ID")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is a read-only dry run)")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation for --apply")
    parser.add_argument("--recipe-id", action="append", dest="recipe_ids", help="Limit to recipe ID (repeatable)")
    parser.add_argument("--limit", type=int, help="Stop after scanning N recipes")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Documents read per page")
    parser.add_argument("--verbose", action="store_true", help="Print per-recipe actions and partial lines")
    args = parser.parse_args(argv)

    if not args.project:
        print("ERROR: --project is required (or set GOOGLE_CLOUD_PROJECT).", file=sys.stderr)
        return 2
    if args.page_size < 1 or (args.limit is not None and args.limit < 1):
        print("ERROR: --page-size and --limit must be positive.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print(f"Project: {args.project}  Database: {DATABASE}  Mode: {'APPLY' if args.apply else 'DRY RUN (read-only)'}")
    if args.apply and not args.yes:
        answer = input(f"Type the project ID ({args.project}) to confirm writes: ")
        if answer.strip() != args.project:
            print("Aborted.")
            return 1

    db = firestore.Client(project=args.project, database=DATABASE)
    stats = run_backfill(
        db,
        apply=args.apply,
        recipe_ids=args.recipe_ids,
        limit=args.limit,
        page_size=args.page_size,
        verbose=args.verbose,
    )
    print_summary(stats, apply=args.apply)
    return 1 if args.apply and (stats.write_errors or stats.conflicts) else 0


if __name__ == "__main__":
    sys.exit(main())
