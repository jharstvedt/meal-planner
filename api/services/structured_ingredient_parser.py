"""Deterministic structured ingredient parsing service using ingredient-parser-nlp."""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from fractions import Fraction

import ingredient_parser
from ingredient_parser.dataclasses import IngredientAmount, ParsedIngredient as NlpParsedIngredient

from api.models.structured_ingredient import (
    Measurement,
    ParserMetadata,
    StructuredIngredient,
    StructuredIngredientsMeta,
)

logger = logging.getLogger(__name__)

PARSER_NAME = "ingredient-parser-nlp"
# Bump when wrapper normalization output changes, independent of the library version.
WRAPPER_VERSION = "1"

_SIZE_WORDS = "thick|thin|large|small|medium|generous|heaping|level"
_ADJECTIVE_COUNT_RE = re.compile(
    r"^(\d+(?:\.\d+)?|\d+/\d+)\s+"
    rf"({_SIZE_WORDS})\s+"
    r"(slices?|pieces?|strips?|cans?|stalks?|cloves?|bunches?|heads?|bottles?|packets?|packages?|jars?|bags?|boxes?)\b",
    re.IGNORECASE,
)
_UNIT_SIZE_PREFIX_RE = re.compile(rf"^({_SIZE_WORDS})\s+(.+)$", re.IGNORECASE)
# Only units observed from the library as plural non-pint strings; pint units are already canonical.
_UNIT_SINGULAR = {
    "cloves": "clove",
    "slices": "slice",
    "cups": "cup",
    "stalks": "stalk",
    "sticks": "stick",
    "bulbs": "bulb",
    "bunches": "bunch",
    "pinches": "pinch",
    "cans": "can",
}
_TRAILING_PAREN_RE = re.compile(r"^(.*\S)\s*\(([^()]+)\)$")
_VULGAR_FRACTIONS = {
    "1/2": "\u00bd",
    "1/4": "\u00bc",
    "3/4": "\u00be",
    "1/3": "\u2153",
    "2/3": "\u2154",
    "1/8": "\u215b",
}

_DECIMAL_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$")
_OPTIONAL_RE = re.compile(r"\boptional\b", re.IGNORECASE)
_TO_TASTE_RE = re.compile(r"\b(?:to taste|as needed|to your taste)\b", re.IGNORECASE)
_DESCRIPTOR_ARTIFACT_RE = re.compile(
    r"\b(?:low[- ]sodium|reduced[- ]sodium|low[- ]fat|fat[- ]free|gluten[- ]free|organic|freshly|divided)\b",
    re.IGNORECASE,
)


def _clean_text(text: str | None) -> str | None:
    """Clean punctuation artifacts from extracted text fragments."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^[,\s*]+", "", cleaned)
    cleaned = re.sub(r"[,\s*]+$", "", cleaned)
    cleaned = re.sub(r"^\(,\s*", "", cleaned)

    if cleaned.startswith("(") and cleaned.endswith(")"):
        inner = cleaned[1:-1].strip()
        if inner.count("(") == inner.count(")"):
            cleaned = inner

    if cleaned.count(")") > cleaned.count("("):
        cleaned = re.sub(r"\)+$", "", cleaned)
    if cleaned.count("(") > cleaned.count(")"):
        cleaned = re.sub(r"^\(+", "", cleaned)

    cleaned = re.sub(r"^[,\s*]+", "", cleaned)
    cleaned = re.sub(r"[,\s*]+$", "", cleaned)
    return cleaned.strip() if cleaned.strip() else None


def _split_merged_alternative(name: str, raw_text: str) -> tuple[str, str] | None:
    """Split a library-merged name (e.g. 'cheddar tasty cheese') when the raw text shows 'A or B' / 'A/B'."""
    words = name.split()
    for k in range(1, len(words)):
        left = r"\s+".join(map(re.escape, words[:k]))
        right = r"\s+".join(map(re.escape, words[k:]))
        if re.search(rf"\b{left}\s*(?:\bor\b|/)\s*{right}\b", raw_text, re.IGNORECASE):
            return " ".join(words[:k]), " ".join(words[k:])
    return None


def _split_name_parenthetical(name: str) -> tuple[str, str | None]:
    """Move a short, purely alphabetic trailing parenthetical (e.g. '(mince)') out of the name."""
    match = _TRAILING_PAREN_RE.match(name)
    if match and re.fullmatch(r"[A-Za-z][A-Za-z -]{0,30}", match.group(2).strip()):
        return match.group(1).strip(), match.group(2).strip()
    return name, None


def _connector_before(extra: str, primary_words: set[str], raw_lower: str, cursor: int) -> tuple[str | None, int]:
    """Classify the raw-text connector preceding an extra name as 'or', 'and', or None (no evidence)."""
    words = extra.lower().split()
    distinctive = [w for w in words if w not in primary_words] or words
    start: int | None = None
    end = cursor
    for word in distinctive:
        match = re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", raw_lower[end:])
        if not match:
            return None, cursor
        if start is None:
            start = end + match.start()
        end += match.end()
    gap = raw_lower[cursor:start].strip(" ,()")
    if gap in ("or", "/"):
        return "or", end
    if gap in ("and", "&", ""):
        return "and", end
    return None, end


def _is_descriptor_only(extra: str, primary_words: set[str]) -> bool:
    """True when an extra name adds only descriptor words (e.g. 'chicken low sodium' after 'chicken broth')."""
    if not _DESCRIPTOR_ARTIFACT_RE.search(extra):
        return False
    remainder = _DESCRIPTOR_ARTIFACT_RE.sub(" ", extra).lower().split()
    return all(w in primary_words for w in remainder)


def _extract_names_and_alternatives(
    parsed: NlpParsedIngredient, raw_text: str
) -> tuple[str | None, str | None, list[str]]:
    """Extract primary name, an explicit 'or'/slash alternative, and notes for any other name fragments."""
    raw_names = [c for c in (_clean_text(n.text) for n in parsed.name) if c]
    if not raw_names:
        return None, None, []

    primary_name, name_note = _split_name_parenthetical(raw_names[0])
    alternative_name: str | None = None
    extra_notes: list[str] = [name_note] if name_note else []
    if len(raw_names) == 1:
        merged = _split_merged_alternative(primary_name, raw_text)
        if merged:
            primary_name, alternative_name = merged
        return primary_name, alternative_name, extra_notes

    raw_lower = raw_text.lower()
    primary_words = set(primary_name.lower().split())
    found = raw_lower.find(primary_name.lower())
    cursor = found + len(primary_name) if found >= 0 else 0

    for extra in raw_names[1:]:
        kind, cursor = _connector_before(extra, primary_words, raw_lower, cursor)
        if kind == "or" and alternative_name is None:
            alternative_name = extra
        elif kind == "or":
            extra_notes.append(f"or {extra}")
        elif _is_descriptor_only(extra, primary_words):
            extra_notes.extend(m.group(0).lower() for m in _DESCRIPTOR_ARTIFACT_RE.finditer(extra))
        elif kind == "and":
            extra_notes.append(f"and {extra}")
        else:
            extra_notes.append(extra)

    return primary_name, alternative_name, extra_notes


def _parse_amount_quantities(amt: IngredientAmount) -> tuple[float | None, float | None, bool]:
    """Parse quantity, quantity_max, and is_range from an IngredientAmount."""
    qty: float | None = None
    qty_max: float | None = None
    is_range = False

    if isinstance(amt.quantity, (Fraction, int, float)):
        qty = float(amt.quantity)
    elif isinstance(amt.quantity, str):
        range_match = _DECIMAL_RANGE_RE.match(amt.quantity)
        if range_match:
            qty = float(range_match.group(1))
            qty_max = float(range_match.group(2))
            is_range = True
        else:
            try:
                qty = float(amt.quantity)
            except ValueError:
                qty = None

    if amt.RANGE and qty_max is None:
        if isinstance(amt.quantity_max, (Fraction, int, float)):
            qty_max = float(amt.quantity_max)
        is_range = True

    if qty_max is not None and qty is not None and qty_max == qty and not is_range:
        qty_max = None

    return qty, qty_max, is_range


def _strip_size_word(text: str | None) -> tuple[str | None, str | None]:
    """Split a leading size adjective off a unit string: 'large cloves' -> ('cloves', 'large')."""
    if not text:
        return text, None
    match = _UNIT_SIZE_PREFIX_RE.match(text.strip())
    if match:
        return match.group(2).strip(), match.group(1).lower()
    return text, None


def _normalize_unit(unit: object) -> str | None:
    """Return a canonical unit string, singularizing only the small observed set of count units."""
    if not unit:
        return None
    u_str = str(unit).lower().strip()
    if not u_str or u_str in ("none", "<unit: dimensionless>"):
        return None
    return _UNIT_SINGULAR.get(u_str, u_str)


def _unit_token_key(token: str) -> str:
    return token.lower().rstrip(".").removesuffix("s")


def _qty_part_pattern(part: str) -> str:
    """Regex for one quantity token, also matching its Unicode vulgar-fraction form as written in source text."""
    vulgar = _VULGAR_FRACTIONS.get(part)
    return f"(?:{re.escape(part)}|{vulgar})" if vulgar else re.escape(part)


def _lexical_display(
    qty_display: str | None, unit_display: str | None, raw_text: str, cursor: int
) -> tuple[str | None, str | None, int]:
    """Recover quantity/unit display strings as written in the raw text, falling back to library text."""
    if not qty_display:
        return qty_display, unit_display, cursor
    q = qty_display.strip()
    sep, parts = (r"\s*[-\u2013]\s*", q.split("-")) if "-" in q else (r"\s+", q.split())
    body = sep.join(_qty_part_pattern(p.strip()) for p in parts)
    pattern = r"(?<![\d./])" + body + r"(?![\d/]|\.\d)"
    qty_match = re.compile(pattern).search(raw_text, cursor)
    if not qty_match and q == "1":
        qty_match = re.compile(r"\b(?:an?|one)\b", re.IGNORECASE).search(raw_text, cursor)
    if not qty_match:
        return qty_display, unit_display, cursor

    lexical_qty = re.sub(r"\s*[-\u2013]\s*", "-", qty_match.group(0))
    end = qty_match.end()
    if not unit_display:
        return lexical_qty, None, end

    unit_keys = [_unit_token_key(w) for w in unit_display.split()]
    tokens = list(re.finditer(r"[A-Za-z]+\.?", raw_text[end:]))[:4]
    for i in range(len(tokens) - len(unit_keys) + 1):
        window = tokens[i : i + len(unit_keys)]
        if [_unit_token_key(t.group(0)) for t in window] == unit_keys:
            lexical_unit = raw_text[end + window[0].start() : end + window[-1].end()].rstrip(".")
            return lexical_qty, lexical_unit, end + window[-1].end()
    return lexical_qty, unit_display, end


def _extract_measurements(parsed: NlpParsedIngredient, raw_text: str) -> tuple[list[Measurement], str | None]:
    """Convert parser amounts into Measurement models; also return any size word found inside a unit."""
    measurements: list[Measurement] = []
    unit_size: str | None = None
    cursor = 0
    for amt in parsed.amount:
        amt_text = amt.text.strip()
        qty, qty_max, _ = _parse_amount_quantities(amt)

        m_qty = re.match(r"^([\d\s/.-]+?)(?:\s+[a-zA-Z]|$)", amt_text)
        qty_display = m_qty.group(1).strip() if m_qty else None
        if not qty_display and amt.quantity:
            qty_display = str(amt.quantity) if isinstance(amt.quantity, str) else str(qty) if qty is not None else None

        lib_unit_display = amt_text[len(m_qty.group(1)) :].strip() if m_qty else amt_text
        lib_unit_display, size_from_display = _strip_size_word(lib_unit_display or None)
        unit_raw, size_from_unit = _strip_size_word(str(amt.unit) if amt.unit else None)
        unit_size = unit_size or size_from_unit or size_from_display

        qty_display, unit_display, cursor = _lexical_display(qty_display, lib_unit_display, raw_text, cursor)
        measurements.append(
            Measurement(
                quantity=qty,
                quantity_max=qty_max,
                quantity_display=qty_display,
                unit=_normalize_unit(unit_raw),
                unit_display=unit_display,
            )
        )
    return measurements, unit_size


def _extract_optionality_and_notes(
    parsed: NlpParsedIngredient, prep: str | None, extra_notes: list[str], raw_text: str, *, has_fixed_measurement: bool
) -> tuple[bool, bool, str | None, str | None]:
    """Extract optionality flag, to-taste flag, updated prep, and combined notes."""
    is_optional = False
    notes_list: list[str] = []

    for text_source in (
        parsed.comment.text if parsed.comment else None,
        parsed.purpose.text if parsed.purpose else None,
    ):
        if not text_source:
            continue
        if _OPTIONAL_RE.search(text_source):
            is_optional = True
            cleaned = _clean_text(_OPTIONAL_RE.sub("", text_source))
        else:
            cleaned = _clean_text(text_source)
        if cleaned:
            notes_list.append(cleaned)

    if prep and _OPTIONAL_RE.search(prep):
        is_optional = True
        prep = _clean_text(_OPTIONAL_RE.sub("", prep))

    to_taste = False
    if not has_fixed_measurement:
        if _TO_TASTE_RE.search(raw_text):
            to_taste = True
            notes_list = [n for n in notes_list if not _TO_TASTE_RE.search(n)]
    else:
        to_taste = False

    for extra in extra_notes:
        if extra not in notes_list:
            notes_list.append(extra)

    final_notes = "; ".join(notes_list) if notes_list else None
    return is_optional, to_taste, prep, final_notes


def _calculate_metadata(parsed: NlpParsedIngredient, *, is_partial: bool) -> ParserMetadata:
    """Aggregate confidence scores across tokens and build ParserMetadata."""
    confidences: list[float] = []
    if parsed.name:
        confidences.extend(n.confidence for n in parsed.name)
    if parsed.amount:
        confidences.extend(a.confidence for a in parsed.amount)
    if parsed.size:
        confidences.append(parsed.size.confidence)
    if parsed.preparation:
        confidences.append(parsed.preparation.confidence)
    if parsed.comment:
        confidences.append(parsed.comment.confidence)
    if parsed.purpose:
        confidences.append(parsed.purpose.confidence)

    avg_conf = round(sum(confidences) / len(confidences), 4) if confidences else None
    return ParserMetadata(confidence=avg_conf, is_partial=is_partial)


def _unparsed_ingredient(raw_text: str, ingredient_id: str, source_text: str | None = None) -> StructuredIngredient:
    """Build a raw-text-only ingredient for empty or unparseable input."""
    return StructuredIngredient(
        id=ingredient_id,
        raw_text=raw_text,
        source_text=source_text,
        parser_metadata=ParserMetadata(confidence=0.0, is_partial=True),
    )


def compute_ingredients_hash(lines: list[str]) -> str:
    """Return a SHA-256 hex digest of the ordered lines; JSON array encoding avoids delimiter collisions."""
    encoded = json.dumps(lines, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_structured_ingredient(
    raw_text: str, *, ingredient_id: str, source_text: str | None = None
) -> StructuredIngredient:
    """Parse a single raw ingredient string into a StructuredIngredient with a caller-supplied ID."""
    if not raw_text or not raw_text.strip():
        return _unparsed_ingredient(raw_text, ingredient_id, source_text)

    text_to_parse = raw_text.strip()
    extracted_size: str | None = None
    adj_match = _ADJECTIVE_COUNT_RE.match(text_to_parse)
    if adj_match:
        qty_part = adj_match.group(1)
        extracted_size = adj_match.group(2).lower()
        unit_part = adj_match.group(3)
        text_to_parse = f"{qty_part} {unit_part} {text_to_parse[adj_match.end() :].strip()}"

    parsed = ingredient_parser.parse_ingredient(text_to_parse)
    primary_name, alternative_name, extra_notes = _extract_names_and_alternatives(parsed, raw_text)
    measurements, unit_size = _extract_measurements(parsed, raw_text)

    size_desc = extracted_size or unit_size or (_clean_text(parsed.size.text) if parsed.size else None)
    prep = _clean_text(parsed.preparation.text) if parsed.preparation else None

    has_fixed_qty = len(measurements) > 0 and any(m.quantity is not None for m in measurements)
    is_optional, to_taste, prep, notes = _extract_optionality_and_notes(
        parsed, prep, extra_notes, raw_text, has_fixed_measurement=has_fixed_qty
    )

    is_partial = primary_name is None or (len(measurements) == 0 and not to_taste and not primary_name)
    metadata = _calculate_metadata(parsed, is_partial=is_partial)

    return StructuredIngredient(
        id=ingredient_id,
        raw_text=raw_text,
        source_text=source_text,
        name=primary_name,
        alternative=alternative_name,
        measurements=measurements,
        size_descriptor=size_desc,
        preparation=prep,
        optional=is_optional,
        to_taste=to_taste,
        notes=notes,
        parser_metadata=metadata,
    )


def parse_ingredient_list(lines: list[str]) -> tuple[list[StructuredIngredient], StructuredIngredientsMeta]:
    """Parse ordered lines, isolating per-line failures.

    IDs are positional placeholders (tmp_0, tmp_1, ...) and must never be persisted as stable recipe IDs.
    """
    ingredients: list[StructuredIngredient] = []
    for index, line in enumerate(lines):
        temp_id = f"tmp_{index}"
        try:
            ingredients.append(parse_structured_ingredient(line, ingredient_id=temp_id))
        except Exception:
            logger.exception("Structured ingredient parsing failed for line index %d", index)
            ingredients.append(_unparsed_ingredient(line, temp_id))

    meta = StructuredIngredientsMeta(
        parser_name=PARSER_NAME,
        parser_version=ingredient_parser.__version__,
        wrapper_version=WRAPPER_VERSION,
        ingredients_hash=compute_ingredients_hash(lines),
        parsed_at=datetime.now(UTC),
    )
    return ingredients, meta
