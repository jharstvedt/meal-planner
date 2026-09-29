"""Targeted tests for StructuredIngredient models and deterministic parsing service."""

from datetime import UTC, datetime
from typing import Any

import ingredient_parser
import pytest
from pydantic import ValidationError

from api.models.structured_ingredient import (
    Measurement,
    ParserMetadata,
    StructuredIngredient,
    StructuredIngredientsMeta,
)
from api.services import structured_ingredient_parser as sip
from api.services.structured_ingredient_parser import (
    WRAPPER_VERSION,
    compute_ingredients_hash,
    parse_ingredient_list,
    parse_structured_ingredient,
)

TEST_ID = "test-id"


def _parse(raw: str, **kwargs: Any) -> StructuredIngredient:
    return parse_structured_ingredient(raw, ingredient_id=TEST_ID, **kwargs)


class TestStructuredIngredientModels:
    """Validate Pydantic schema validation and immutability."""

    def test_structured_ingredient_defaults(self) -> None:
        ing = StructuredIngredient(id="line-1", raw_text="salt")
        assert ing.id == "line-1"
        assert ing.raw_text == "salt"
        assert ing.source_text is None
        assert ing.name is None
        assert ing.alternative is None
        assert ing.measurements == []
        assert ing.size_descriptor is None
        assert ing.preparation is None
        assert ing.optional is False
        assert ing.to_taste is False
        assert ing.notes is None
        assert ing.group is None
        assert ing.parser_metadata is None

    def test_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            StructuredIngredient(raw_text="salt")  # type: ignore[call-arg]

    def test_model_immutability(self) -> None:
        ing = StructuredIngredient(id="line-1", raw_text="salt", name="salt")
        with pytest.raises(ValidationError):
            ing.name = "pepper"  # type: ignore[misc]

    def test_measurement_schema(self) -> None:
        m = Measurement(quantity=1.5, quantity_max=2.0, quantity_display="1.5-2", unit="cup", unit_display="cups")
        assert m.quantity == 1.5
        assert m.quantity_max == 2.0
        assert m.quantity_display == "1.5-2"
        assert m.unit == "cup"
        assert m.unit_display == "cups"
        assert not hasattr(m, "raw")
        assert not hasattr(m, "original_form")
        assert not hasattr(m, "is_range")
        assert {"raw", "original_form", "is_range"}.isdisjoint(Measurement.model_fields)

    def test_parser_metadata_is_per_line_only(self) -> None:
        meta = ParserMetadata(confidence=0.985, is_partial=False)
        assert meta.confidence == 0.985
        assert set(ParserMetadata.model_fields) == {"confidence", "is_partial"}

    def test_structured_ingredients_meta_fields(self) -> None:
        now = datetime.now(UTC)
        meta = StructuredIngredientsMeta(
            parser_name="ingredient-parser-nlp",
            parser_version="2.7.0",
            wrapper_version="1",
            ingredients_hash="abc",
            parsed_at=now,
        )
        assert meta.parsed_at.tzinfo == UTC
        assert meta.wrapper_version == "1"


class TestStructuredIngredientParser:
    """Targeted tests for parse_structured_ingredient and normalization rules."""

    def test_caller_supplied_id_is_preserved(self) -> None:
        assert parse_structured_ingredient("1 cup sugar", ingredient_id="custom-id-123").id == "custom-id-123"

    def test_single_line_parser_requires_id(self) -> None:
        with pytest.raises(TypeError):
            parse_structured_ingredient("1 cup sugar")  # type: ignore[call-arg]

    def test_no_parse_ingredient_alias(self) -> None:
        assert not hasattr(sip, "parse_ingredient")

    def test_garlic_cloves_with_punctuation_artifacts(self) -> None:
        raw = "2 garlic cloves (, minced)"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "garlic"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 2.0
        assert result.measurements[0].quantity_max is None
        assert result.measurements[0].quantity_display == "2"
        assert result.measurements[0].unit == "clove"
        assert result.measurements[0].unit_display == "cloves"
        assert result.preparation == "minced"
        assert result.notes is None
        assert result.optional is False
        assert result.to_taste is False
        assert result.parser_metadata is not None
        assert result.parser_metadata.is_partial is False

    def test_dual_measurement_with_parenthetical_comment(self) -> None:
        raw = "500 g/ 1 lb ground beef (mince) (, preferably lean)"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "ground beef"
        assert len(result.measurements) == 2
        assert result.measurements[0].quantity == 500.0
        assert result.measurements[0].quantity_display == "500"
        assert result.measurements[0].unit == "gram"
        assert result.measurements[0].unit_display == "g"
        assert result.measurements[1].quantity == 1.0
        assert result.measurements[1].quantity_display == "1"
        assert result.measurements[1].unit == "pound"
        assert result.measurements[1].unit_display == "lb"
        assert result.notes is not None
        assert "mince" in result.notes
        assert "preferably lean" in result.notes
        assert result.optional is False
        assert result.to_taste is False

    def test_decimal_range_without_name(self) -> None:
        raw = "1.5 - 2 cups (150 - 200g)"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name is None
        assert len(result.measurements) == 2
        assert result.measurements[0].quantity == 1.5
        assert result.measurements[0].quantity_max == 2.0
        assert result.measurements[0].quantity_display == "1.5-2"
        assert result.measurements[0].unit == "cup"
        assert result.measurements[0].unit_display == "cups"
        assert result.measurements[1].quantity == 150.0
        assert result.measurements[1].quantity_max == 200.0
        assert result.measurements[1].quantity_display == "150-200"
        assert result.measurements[1].unit == "gram"
        assert result.measurements[1].unit_display == "g"
        assert result.parser_metadata is not None
        assert result.parser_metadata.is_partial is True

    def test_salt_to_taste(self) -> None:
        raw = "salt to taste"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "salt"
        assert result.measurements == []
        assert result.to_taste is True
        assert result.optional is False
        assert result.notes is None

    def test_alternatives_and_adjust_to_taste(self) -> None:
        raw = "1 tbsp hot sauce or Sriracha sauce (, adjust to taste)"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "hot sauce"
        assert result.alternative == "Sriracha sauce"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 1.0
        assert result.measurements[0].quantity_display == "1"
        assert result.measurements[0].unit == "tablespoon"
        assert result.measurements[0].unit_display == "tbsp"
        assert result.to_taste is False
        assert result.notes == "adjust to taste"

    def test_size_descriptor_and_optional_flag_with_footnote(self) -> None:
        raw = "1 small carrot * (, chopped (optional))"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "carrot"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 1.0
        assert result.measurements[0].quantity_display == "1"
        assert result.measurements[0].unit is None
        assert result.size_descriptor == "small"
        assert result.preparation == "chopped"
        assert result.optional is True
        assert result.to_taste is False
        assert result.notes is None

    def test_slash_alternative_with_descriptor_note(self) -> None:
        raw = "2 cups chicken broth/stock, low sodium"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "chicken broth"
        assert result.alternative == "chicken stock"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 2.0
        assert result.measurements[0].quantity_display == "2"
        assert result.measurements[0].unit == "cup"
        assert result.measurements[0].unit_display == "cups"
        assert result.notes == "low sodium"

    def test_adjective_modified_count_and_size_descriptor(self) -> None:
        raw = "4 thick slices sourdough or white bread"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "sourdough"
        assert result.alternative == "white bread"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 4.0
        assert result.measurements[0].quantity_display == "4"
        assert result.measurements[0].unit == "slice"
        assert result.measurements[0].unit_display == "slices"
        assert result.size_descriptor == "thick"

    def test_normal_fraction_ingredient(self) -> None:
        raw = "1/2 tsp kosher salt"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "kosher salt"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 0.5
        assert result.measurements[0].quantity_display == "1/2"
        assert result.measurements[0].unit == "teaspoon"
        assert result.measurements[0].unit_display == "tsp"
        assert result.optional is False
        assert result.to_taste is False

    def test_normal_simple_ingredient(self) -> None:
        raw = "2 cups all-purpose flour"
        result = _parse(raw, source_text="2 cups all-purpose flour (unbleached)")

        assert result.raw_text == raw
        assert result.source_text == "2 cups all-purpose flour (unbleached)"
        assert result.name == "all-purpose flour"
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 2.0
        assert result.measurements[0].quantity_display == "2"
        assert result.measurements[0].unit == "cup"
        assert result.measurements[0].unit_display == "cups"
        assert result.optional is False
        assert result.to_taste is False
        assert result.notes is None

    def test_or_alternative_with_descriptor(self) -> None:
        result = _parse("2 cups spinach or organic kale")

        assert result.name == "spinach"
        assert result.alternative == "organic kale"
        assert result.notes is None

    def test_or_alternative_hyphenated_descriptors_keep_identity(self) -> None:
        result = _parse("low-fat milk or gluten-free oat milk")

        assert result.name == "low-fat milk"
        assert result.alternative == "gluten-free oat milk"

    def test_and_is_not_alternative(self) -> None:
        result = _parse("salt and pepper to taste")

        assert result.name == "salt"
        assert result.alternative is None
        assert result.to_taste is True
        assert result.notes is not None
        assert "pepper" in result.notes

    def test_descriptor_does_not_discard_food_identity(self) -> None:
        result = _parse("1 cup organic spinach")

        assert result.name == "organic spinach"
        assert result.alternative is None

    def test_size_inside_count_unit(self) -> None:
        raw = "6 large garlic cloves, grated"
        result = _parse(raw)

        assert result.raw_text == raw
        assert len(result.measurements) == 1
        assert result.measurements[0].quantity == 6.0
        assert result.measurements[0].unit == "clove"
        assert result.measurements[0].unit_display == "cloves"
        assert result.size_descriptor == "large"
        assert result.name == "garlic"
        assert result.preparation == "grated"

    def test_lexical_display_abbreviations(self) -> None:
        result = _parse("2 tbsp olive oil")

        assert result.measurements[0].quantity_display == "2"
        assert result.measurements[0].unit == "tablespoon"
        assert result.measurements[0].unit_display == "tbsp"

    def test_unicode_fraction_display_is_lexical(self) -> None:
        raw = "\u00bd tsp salt"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.measurements[0].quantity == 0.5
        assert result.measurements[0].quantity_display == "\u00bd"
        assert result.measurements[0].unit == "teaspoon"
        assert result.measurements[0].unit_display == "tsp"

    def test_word_quantity_display_is_lexical(self) -> None:
        raw = "A pinch of freshly cracked black pepper"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "black pepper"
        assert result.measurements[0].quantity == 1.0
        assert result.measurements[0].quantity_display == "A"
        assert result.measurements[0].unit == "pinch"
        assert result.measurements[0].unit_display == "pinch"

    def test_mixed_number_display(self) -> None:
        result = _parse("1 1/2 cups flour")

        assert result.measurements[0].quantity == 1.5
        assert result.measurements[0].quantity_display == "1 1/2"
        assert result.measurements[0].unit_display == "cups"

    def test_composite_amount_splits_into_measurements(self) -> None:
        result = _parse("1 lb 2 oz ground beef")

        assert result.name == "ground beef"
        assert [(m.quantity, m.unit) for m in result.measurements] == [(1.0, "pound"), (2.0, "ounce")]
        assert [m.unit_display for m in result.measurements] == ["lb", "oz"]

    def test_ranges_with_alternative_prep_and_note(self) -> None:
        raw = "1.5 - 2 cups (150 - 200g) cheddar or tasty cheese, shredded (Note 3)"
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.name == "cheddar"
        assert result.alternative == "tasty cheese"
        assert result.preparation == "shredded"
        assert result.notes is not None
        assert "Note 3" in result.notes
        assert len(result.measurements) == 2
        assert result.measurements[0].quantity == 1.5
        assert result.measurements[0].quantity_max == 2.0
        assert result.measurements[0].quantity_display == "1.5-2"
        assert result.measurements[0].unit == "cup"
        assert result.measurements[1].quantity == 150.0
        assert result.measurements[1].quantity_max == 200.0
        assert result.measurements[1].quantity_display == "150-200"
        assert result.measurements[1].unit == "gram"
        assert result.measurements[1].unit_display == "g"

    @pytest.mark.parametrize(
        ("raw", "unit", "unit_display"),
        [
            ("3 stalks celery", "stalk", "stalks"),
            ("2 sticks butter", "stick", "sticks"),
            ("4 cloves garlic", "clove", "cloves"),
            ("2 slices bread", "slice", "slices"),
            ("1 clove garlic", "clove", "clove"),
        ],
    )
    def test_count_units_singularized(self, raw: str, unit: str, unit_display: str) -> None:
        result = _parse(raw)

        assert result.raw_text == raw
        assert result.measurements[0].unit == unit
        assert result.measurements[0].unit_display == unit_display

    def test_empty_string_handling(self) -> None:
        result = _parse("   ")
        assert result.id == TEST_ID
        assert result.raw_text == "   "
        assert result.name is None
        assert result.measurements == []
        assert result.parser_metadata is not None
        assert result.parser_metadata.is_partial is True
        assert result.parser_metadata.confidence == 0.0


class TestIngredientsHash:
    """Deterministic ordered hashing of raw ingredient lines."""

    def test_same_list_same_hash(self) -> None:
        lines = ["1 cup sugar", "2 eggs"]
        assert compute_ingredients_hash(lines) == compute_ingredients_hash(list(lines))

    def test_reorder_changes_hash(self) -> None:
        assert compute_ingredients_hash(["1 cup sugar", "2 eggs"]) != compute_ingredients_hash(
            ["2 eggs", "1 cup sugar"]
        )

    def test_changed_line_changes_hash(self) -> None:
        assert compute_ingredients_hash(["1 cup sugar", "2 eggs"]) != compute_ingredients_hash(
            ["1 cup sugar", "3 eggs"]
        )

    def test_duplicates_affect_hash(self) -> None:
        assert compute_ingredients_hash(["salt"]) != compute_ingredients_hash(["salt", "salt"])

    def test_no_delimiter_collision(self) -> None:
        assert compute_ingredients_hash(["a,b"]) != compute_ingredients_hash(["a", "b"])
        assert compute_ingredients_hash(["a\nb"]) != compute_ingredients_hash(["a", "b"])
        assert compute_ingredients_hash([""]) != compute_ingredients_hash([])

    def test_hash_is_sha256_hex(self) -> None:
        digest = compute_ingredients_hash(["salt"])
        assert len(digest) == 64
        int(digest, 16)


class TestParseIngredientList:
    """Safe list parsing with positional temporary IDs and list-level metadata."""

    def test_order_and_temporary_ids(self) -> None:
        ingredients, _ = parse_ingredient_list(["1 cup sugar", "2 cups all-purpose flour"])
        assert [i.id for i in ingredients] == ["tmp_0", "tmp_1"]
        assert [i.raw_text for i in ingredients] == ["1 cup sugar", "2 cups all-purpose flour"]

    def test_duplicates_preserved_with_distinct_ids(self) -> None:
        ingredients, _ = parse_ingredient_list(["salt to taste", "salt to taste"])
        assert len(ingredients) == 2
        assert [i.id for i in ingredients] == ["tmp_0", "tmp_1"]
        assert ingredients[0].raw_text == ingredients[1].raw_text

    def test_list_metadata(self) -> None:
        lines = ["1 cup sugar", "2 eggs"]
        _, meta = parse_ingredient_list(lines)
        assert meta.parser_name == "ingredient-parser-nlp"
        assert meta.parser_version == ingredient_parser.__version__
        assert meta.wrapper_version == WRAPPER_VERSION == "1"
        assert meta.ingredients_hash == compute_ingredients_hash(lines)
        offset = meta.parsed_at.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0

    def test_empty_list(self) -> None:
        ingredients, meta = parse_ingredient_list([])
        assert ingredients == []
        assert meta.ingredients_hash == compute_ingredients_hash([])

    def test_line_exception_is_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_parse = ingredient_parser.parse_ingredient

        def flaky_parse(sentence: str, *args: Any, **kwargs: Any) -> Any:
            if "explode" in sentence:
                msg = "boom"
                raise RuntimeError(msg)
            return real_parse(sentence, *args, **kwargs)

        monkeypatch.setattr(ingredient_parser, "parse_ingredient", flaky_parse)
        ingredients, meta = parse_ingredient_list(["1 cup sugar", "2 explode things", "salt to taste"])

        assert [i.id for i in ingredients] == ["tmp_0", "tmp_1", "tmp_2"]
        assert ingredients[0].name == "sugar"
        assert ingredients[2].name == "salt"
        failed = ingredients[1]
        assert failed.raw_text == "2 explode things"
        assert failed.name is None
        assert failed.measurements == []
        assert failed.optional is False
        assert failed.to_taste is False
        assert failed.alternative is None
        assert failed.notes is None
        assert failed.group is None
        assert failed.parser_metadata is not None
        assert failed.parser_metadata.confidence == 0.0
        assert failed.parser_metadata.is_partial is True
        assert meta.ingredients_hash == compute_ingredients_hash(["1 cup sugar", "2 explode things", "salt to taste"])
