---
applyTo: "**/*.py"
description: "Firestore schema conventions for meal planner"
---

# Firestore Instructions

These conventions apply when working with Firestore data in this project.

## Database Configuration

The app uses a single Firestore database: `meal-planner`.

All recipes (original and AI-enhanced) are stored in the same document. Enhanced recipes have `enhanced=True` and the original scraped data preserved in a nested `original` field.

## Recipe Document Schema

All fields must be at the **top level** (no nested objects), with three documented exceptions: the `original` snapshot for enhanced recipes, the derived `structured_ingredients` / `structured_ingredients_meta` fields, and the `source_ingredients_meta` provenance dict.

The `created_at` field is **required** for queries.

```python
{
    # Required fields
    "title": str,                    # Recipe title
    "url": str,                      # Source URL
    "ingredients": list[str],        # List of ingredient strings
    "instructions": list[str],       # List of instruction steps (MUST be list, not string)
    "created_at": datetime,          # Required for order_by queries
    "updated_at": datetime,          # Last modification time

    # Ownership & visibility fields
    "household_id": str | None,      # Owning household (None = legacy/unassigned)
    "visibility": str,               # "household" (private) | "shared" (public)
    "created_by": str | None,        # User ID who created the recipe
    "copied_from": str | None,       # Source recipe ID when copied from shared (for dedup)

    # Optional metadata
    "image_url": str | None,            # Hero image (800x600) for detail screen
    "thumbnail_url": str | None,        # Thumbnail (400x300) for cards/lists
    "servings": int | None,
    "prep_time": int | None,         # Minutes
    "cook_time": int | None,         # Minutes
    "total_time": int | None,        # Minutes
    "cuisine": str | None,           # e.g., "Italian", "Swedish"
    "category": str | None,          # e.g., "Huvudrätt", "Dessert"
    "tags": list[str],               # e.g., ["quick", "vegetarian"]
    "diet_label": str | None,        # "veggie" | "fish" | "meat"
    "meal_label": str | None,        # "breakfast" | "meal" | "dessert" | etc.
    "rating": int | None,            # 1-5 stars

    # AI enhancement fields
    "enhanced": bool,                # True if AI-enhanced
    "enhanced_at": datetime | None,  # When enhancement was performed
    "changes_made": list[str],       # Summary of AI changes
    "show_enhanced": bool,           # True = show enhanced by default, False after user rejects
    "enhancement_reviewed": bool,    # True after user approves/rejects

    # Original scraped data (nested exception — set once on first enhancement)
    "original": {                    # Only present on enhanced recipes
        "title": str,
        "ingredients": list[str],
        "instructions": list[str],
        "servings": int | None,
        "prep_time": int | None,
        "cook_time": int | None,
        "total_time": int | None,
        "image_url": str | None,
    } | None,

    # Derived structured ingredients (nested exception — never authoritative, never set by clients)
    "structured_ingredients": list[dict] | None,       # StructuredIngredient dumps, one per `ingredients` line
    "structured_ingredients_meta": dict | None,        # StructuredIngredientsMeta (parser/wrapper versions, ingredients_hash)

    # Import-source provenance (nested exception for meta — write-once, never authoritative, never set by clients)
    "source_ingredients": list[str] | None,            # Ingredient lines as returned by the scraper, verbatim (bounded)
    "source_ingredients_meta": dict | None,            # SourceIngredientsMeta (extractor, import_method, captured_at, line_count, truncated)
}
```

## Structured Ingredients

- `ingredients: list[str]` is authoritative; structured fields are derived from it and may be absent (legacy docs).
- Any code that writes top-level `ingredients` MUST merge `structured_fields_for_write(...)` from `api/storage/structured_ingredients.py` into the same write. It regenerates when stale, reuses stable IDs, and emits `DELETE_FIELD` on parse failure so data never goes stale.
- Parsing must never block a save. Consumers must check `is_structured_ingredients_current()` before trusting stored data.
- Ingredient IDs are owned by persistence; strip caller-supplied structured fields from external input.

## Source Ingredients

- Captured server-side only, by `/recipes/scrape` and `/recipes/parse`, via `capture_source_ingredients()` in `api/storage/source_ingredients.py`, and passed to `save_recipe(source_ingredients=...)`.
- Write-once: `save_recipe` writes them only when creating a new document. Updates, enhancement, review, and enhancement removal never touch them.
- Never authoritative and never displayed: excluded from API responses; `ingredients` remains the source of truth.
- Never set by clients: `RecipeCreate` / `RecipeUpdate` have no such fields, and `/recipes/preview` + `POST /recipes` saves carry no provenance. The mobile app imports via `/parse`/`/scrape` (the `review-recipe` preview screen is currently unreachable); if a preview-then-save flow is revived, carry provenance server-side (e.g. re-capture or a server-issued token), never from the client payload.
- Absent on legacy docs and on copies; do not backfill or fabricate.

## Enhancement Data Flow

When a recipe is enhanced, the document structure changes:

1. **Before enhancement**: Top-level fields contain the scraped original data
2. **On enhancement**: Original data is snapshotted into `original` nested field, enhanced data replaces top-level fields, `show_enhanced=True` (shows AI version immediately)
3. **User reviews**: Modal shows diff, user keeps AI (`show_enhanced=True`) or reverts to original (`show_enhanced=False`)
4. **App display**: If `enhanced=True` and `show_enhanced=True` → use top-level fields (enhanced version); if `enhanced=True` and `show_enhanced=False` → use `original` snapshot; if `enhanced=False` or `original` is missing → always use top-level fields
5. **Interrupted review**: `enhancement_reviewed=False` → modal reappears next view

**CRITICAL**: Enhancement scripts MUST use Firestore `.update()` (merge), NEVER `.set()` (overwrite). See `tools/recipe_manager.py` for the reference implementation.

## Common Schema Mistakes (DO NOT DO)

| ❌ Wrong                                      | ✅ Correct                                    |
| --------------------------------------------- | --------------------------------------------- |
| `instructions: "Step 1. Do X. Step 2. Do Y."` | `instructions: ["Step 1. Do X", "Step 2..."]` |
| `metadata: { cuisine: "Italian" }`            | `cuisine: "Italian"` (top-level)              |
| Missing `created_at`                          | Always include `created_at: datetime.now()`   |
| `.set(data)` on enhancement                   | `.update(data)` to preserve existing fields   |
| `enhanced_from: str` (separate doc ID)        | `original: { ... }` (nested in same doc)      |
