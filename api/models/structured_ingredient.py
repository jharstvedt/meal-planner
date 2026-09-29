"""Structured ingredient Pydantic models for recipe ingredient parsing."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ImportMethod = Literal["scrape", "parse"]


class SourceIngredientsMeta(BaseModel):
    """Provenance of the ingredient lines received from an import source, captured once at import."""

    extractor: str = Field(description="Component that extracted the lines from the source (e.g. 'recipe-scrapers')")
    import_method: ImportMethod = Field(description="'scrape' (server fetched URL) or 'parse' (client-provided HTML)")
    captured_at: datetime = Field(description="Aware UTC timestamp when the lines were captured")
    line_count: int = Field(ge=0, description="Number of lines the source returned, before any storage bounds")
    truncated: bool = Field(default=False, description="True if storage bounds shortened the stored lines")

    model_config = ConfigDict(frozen=True)


class Measurement(BaseModel):
    """A single measurement (quantity, unit, range) for an ingredient."""

    quantity: float | None = Field(default=None, description="Numeric lower bound or single quantity")
    quantity_max: float | None = Field(default=None, description="Numeric upper bound for quantity ranges")
    quantity_display: str | None = Field(
        default=None, description="Original lexical quantity representation (e.g. '1/2', '1.5-2', '500')"
    )
    unit: str | None = Field(
        default=None, description="Normalized lowercase unit string (e.g. 'cup', 'gram', 'tablespoon')"
    )
    unit_display: str | None = Field(
        default=None, description="Original lexical unit string (e.g. 'cups', 'g', 'tbsp')"
    )

    model_config = ConfigDict(frozen=True)


class ParserMetadata(BaseModel):
    """Per-line parse quality. Engine/version/timestamp live on StructuredIngredientsMeta."""

    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Overall aggregate confidence score [0.0, 1.0]"
    )
    is_partial: bool = Field(
        default=False, description="True if parsing succeeded only partially or encountered ambiguities"
    )

    model_config = ConfigDict(frozen=True)


class StructuredIngredientsMeta(BaseModel):
    """Recipe-level metadata describing how a list of structured ingredients was produced."""

    parser_name: str = Field(description="Identifier of the parser engine")
    parser_version: str = Field(description="Version string of the parser library")
    wrapper_version: str = Field(description="Version of the in-house normalization wrapper")
    ingredients_hash: str = Field(description="Deterministic hash of the ordered raw ingredient lines")
    parsed_at: datetime = Field(description="Aware UTC timestamp when parsing occurred")

    model_config = ConfigDict(frozen=True)


class StructuredIngredient(BaseModel):
    """Structured representation of a single recipe ingredient line."""

    id: str = Field(description="Caller-supplied identifier for the ingredient line")
    raw_text: str = Field(description="Exact input string passed to parser (current fallback/stored representation)")
    source_text: str | None = Field(default=None, description="Unnormalized original scraped/source text if available")
    name: str | None = Field(
        default=None, description="Primary canonical food name, or None if input lacked a food item"
    )
    measurements: list[Measurement] = Field(default_factory=list, description="Ordered list of extracted measurements")
    size_descriptor: str | None = Field(
        default=None, description="Size or thickness descriptor (e.g. 'small', 'large', 'thick')"
    )
    preparation: str | None = Field(
        default=None, description="Preparation or processing instructions (e.g. 'minced', 'chopped')"
    )
    optional: bool = Field(default=False, description="True if ingredient is marked optional")
    to_taste: bool = Field(
        default=False, description="True if ingredient has no fixed quantity and is added to taste/as needed"
    )
    alternative: str | None = Field(
        default=None, description="Alternative food name, only from explicit 'or' or slash syntax"
    )
    notes: str | None = Field(
        default=None,
        description="Additional qualifiers, purpose clauses, or footnotes (e.g. 'preferably lean', 'Note 1')",
    )
    group: str | None = Field(default=None, description="Ingredient section/group heading if present")
    parser_metadata: ParserMetadata | None = Field(default=None, description="Parser execution metadata and confidence")

    model_config = ConfigDict(frozen=True)
