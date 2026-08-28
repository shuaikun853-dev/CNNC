"""Deterministic hard-rule veto for the SAME title-match verdict.

The model prompt is the primary judge, but two historically validated rule
classes fail-close a model SAME:

* R1 model-code single-sided / conflicting — the standard product carries an
  explicit strong model code (AR-EP65, 91403, G50-2TB, DN20, ...) that the
  candidate title does not express in any equivalent spelling;
* R2 multi-pack single-sided / conflicting — one title carries an explicit
  multi-piece packaging count ("1000枚/盒", "50片/盒", "4支/套", "800片/箱",
  "四色套装4支") that the other title neither matches nor expresses.

Both rules normalize case, width, separators, "*/×/x", storage units
(T/TB), metric units (mm/cm/m, g/kg, ml/L) and single-piece sale units
(个/件/把/台/张/片 without a packaging denominator are exempt and never
trigger R2).  The veto NEVER upgrades a verdict to SAME: it only downgrades
model-SAME to NOT_SAME, and only when a rule fires with an explicit reason.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


_UNIT_LETTERS = "MM|CM|KM|KG|MG|ML|TB|GB|MB|W|V|A|MAH|WH|KWH|RPM|PPI|DPI|NM|UM"
_MEASURE_RE = re.compile(
    rf"(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*({_UNIT_LETTERS})(?![A-Z0-9])",
    re.IGNORECASE,
)
# CJK measure/count suffixes: "1100页", "4支", "3米", "50克", "12伏", "60帧"...
_CJK_UNIT_CHARS = (
    "毫米厘米公里千克克毫克毫升升瓦伏安安时毫安时千兆吉兆节页支张片把台个只根卷贴粒颗份次帧"
    "度米磅盎司寸英尺加仑桶平方米平米立方立升"
)
_CJK_QUANTITY_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*[{_CJK_UNIT_CHARS}]")

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

_MODEL_TOKEN_RE = re.compile(r"[A-Z0-9]+(?:[-_/][A-Z0-9]+)*")

# ---------------------------------------------------------------------------
# R1 — model-code single-sided
# ---------------------------------------------------------------------------


def _to_halfwidth(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _normalize_for_tokens(text: str) -> str:
    """Uppercase halfwidth text with measurement/quantity expressions removed.

    Quantities with units (500mm, 50cm, 1T, 1100页, 4支) are specs/counts, not
    model codes; removing them first keeps the token extractor conservative.
    """
    upper = _to_halfwidth(text).upper()
    upper = _MEASURE_RE.sub(" ", upper)
    upper = _CJK_QUANTITY_RE.sub(" ", upper)
    return upper


def _strong_model_tokens(normalized: str) -> list[str]:
    """Tokens strong enough to anchor a single-sided veto.

    Mixed letter+digit codes (AR-EP65, DN20, G502, 128G, MATE60) need length
    >= 4; pure-digit codes need >= 5 digits so counts like 100/800 and years
    never veto; pure-letter words (HERO, PRO, PLUS) are never strong.  Unit
    quantities were already stripped by normalization.
    """
    tokens: list[str] = []
    for raw in _MODEL_TOKEN_RE.findall(normalized):
        token = raw.strip("-_/")
        if _is_strong_token(token):
            tokens.append(token)
    return tokens


def _is_strong_token(token: str) -> bool:
    if not token or not any(character.isdigit() for character in token):
        return False
    has_letter = any(character.isalpha() for character in token)
    if has_letter:
        return len(token) >= 4
    return len(token) >= 5 and not _YEAR_RE.match(token)


def _equivalent_model_forms(token: str) -> set[str]:
    """Spelling-equivalent forms of one model code: separators removed,
    storage suffix flexed (2T/2TB, G50-2TB vs G50-2T) and a short purely
    alphabetic brand prefix shed (SATA-91403 ≡ 91403, AR-EP65 ⊇ EP65) when
    the remainder is itself a strong code."""
    compact = token.replace("-", "").replace("_", "").replace("/", "")
    forms = {compact}
    prefix_match = re.match(r"^[A-Z]{2,4}(?=[0-9])", compact)
    if prefix_match is not None:
        remainder = compact[prefix_match.end():]
        if _is_strong_token(remainder):
            forms.add(remainder)
    for suffix in ("TB", "GB", "MB"):
        if compact.endswith(suffix):
            stem = compact[: -len(suffix)]
            forms.add(stem)
            if suffix == "TB":
                forms.add(stem + "T")
    if compact.endswith("T") and not compact.endswith(("AT", "ET", "GT", "ST", "XT")):
        forms.add(compact[:-1] + "TB")
    return forms


def _brand_fused(longer: str, shorter: str) -> bool:
    """True when ``longer`` is exactly a 2-8 letter brand word fused onto
    ``shorter`` (SATA91403 ⊇ 91403, BIZHUB225I ⊇ 225I).  The boundary must be
    letter-to-code, so DN200 never fuses with DN20 and 914030 never fuses
    with 91403: the extra characters there are digits, i.e. a different
    model code, not a brand prefix."""
    if len(longer) <= len(shorter) or not longer.endswith(shorter):
        return False
    prefix = longer[: len(longer) - len(shorter)]
    return 2 <= len(prefix) <= 8 and prefix.isalpha()


def _model_codes_equivalent(query_token: str, sku_token: str) -> bool:
    """Token-level exact-equivalence: same normalized code, storage-suffix or
    brand-prefix variant of the same code — never a mere substring.

    This is what keeps DN20 from matching DN200 and 91403 from matching
    914030 while SATA-91403 still matches 91403."""
    query_forms = _equivalent_model_forms(query_token)
    sku_forms = _equivalent_model_forms(sku_token)
    if query_forms & sku_forms:
        return True
    return any(
        _brand_fused(longer, shorter)
        for longer in sku_forms
        for shorter in query_forms
    ) or any(
        _brand_fused(longer, shorter)
        for longer in query_forms
        for shorter in sku_forms
    )


def standard_model_codes_present(query_str: str, sku_name: str) -> bool:
    """True when every strong model code of the standard title has an
    exactly-equivalent code in the candidate title.

    This is the deterministic form of prompt rule 7's "除非标题已明确当前
    SKU 就是 query_str 的目标型号" branch; the shared risk-word guard uses it
    instead of trusting the word list alone.
    """
    query_tokens = _strong_model_tokens(_normalize_for_tokens(query_str))
    if not query_tokens:
        return False
    sku_tokens = _strong_model_tokens(_normalize_for_tokens(sku_name))
    return all(
        any(_model_codes_equivalent(token, sku_token) for sku_token in sku_tokens)
        for token in query_tokens
    )


def _model_code_veto(query_str: str, sku_name: str) -> str | None:
    query_tokens = _strong_model_tokens(_normalize_for_tokens(query_str))
    if not query_tokens:
        return None
    # Candidate-side token extraction plus exact-equivalence comparison: a
    # raw substring hit (DN20 inside DN200) is a different model, not the
    # same one, so containment alone must never satisfy the rule.
    if standard_model_codes_present(query_str, sku_name):
        return None
    return f"标准侧明确型号 {','.join(query_tokens)} 在候选标题缺失或冲突"


# ---------------------------------------------------------------------------
# R2 — multi-pack single-sided
# ---------------------------------------------------------------------------

_PIECE_UNITS = "枚片张支个只把根贴粒颗卷份双对把台张"
_PACKAGE_LEVELS = "盒箱包套袋罐瓶桶提袋装"
# "1000枚/盒" / "50片每箱" / "4支/套"
_PACK_DENOMINATOR_RE = re.compile(
    rf"(\d+)\s*[{_PIECE_UNITS}]\s*[/每]\s*[{_PACKAGE_LEVELS}]"
)
# "50片装" / "100张装"
_PACK_SUFFIX_RE = re.compile(rf"(\d+)\s*[{_PIECE_UNITS}]\s*装")
# "四色套装4支" / "整箱24瓶" — pack-level word then a count+piece unit nearby
_PACK_WORD_BEFORE_RE = re.compile(
    rf"(?:套装|组合|整箱|整盒|大包装|家庭装)\D{{0,6}}?(\d+)\s*[{_PIECE_UNITS}]"
)


def _pack_counts(text: str) -> set[int]:
    normalized = _to_halfwidth(text)
    counts: set[int] = set()
    for pattern in (_PACK_DENOMINATOR_RE, _PACK_SUFFIX_RE, _PACK_WORD_BEFORE_RE):
        for match in pattern.findall(normalized):
            try:
                value = int(match)
            except (TypeError, ValueError):
                continue
            if value > 1:
                counts.add(value)
    return counts


def _expressed_same_count(text: str, count: int) -> bool:
    """The count exists with ANY packaging-level word nearby (等价包装表达).

    e.g. query "4支/套" vs sku "四色套装4支": the sku has no explicit
    denominator, but "套装" plus the same piece count expresses the same pack.
    """
    normalized = _to_halfwidth(text)
    pattern = re.compile(rf"(\d+)\s*[{_PIECE_UNITS}]")
    for match in pattern.finditer(normalized):
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value != count:
            continue
        window = normalized[max(0, match.start() - 6) : match.end() + 6]
        if re.search(rf"[{_PACKAGE_LEVELS}]|套装|组合|大包装|家庭装", window):
            return True
    return False


def _multipack_veto(query_str: str, sku_name: str) -> str | None:
    query_counts = _pack_counts(query_str)
    sku_counts = _pack_counts(sku_name)
    # Single-piece sale units (1个/单件) never constitute pack evidence.
    if not query_counts and not sku_counts:
        return None
    if query_counts:
        for count in sorted(query_counts):
            if count in sku_counts or _expressed_same_count(sku_name, count):
                return None
        return f"多件包装数量单边(标准={sorted(query_counts)},候选={sorted(sku_counts) or '未表达'})"
    for count in sorted(sku_counts):
        if _expressed_same_count(query_str, count):
            return None
    return f"多件包装数量单边(候选={sorted(sku_counts)},标准未表达)"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def evaluate_same_hard_rules(query_str: str, sku_name: str) -> str | None:
    """Return a veto reason when a model SAME verdict must fail closed.

    ``None`` means no hard rule fired; the model verdict stands.  The function
    only ever vetoes — it never manufactures a SAME.
    """
    query = str(query_str or "")
    sku = str(sku_name or "")
    if not query.strip() or not sku.strip():
        return None
    reason = _model_code_veto(query, sku)
    if reason is not None:
        return f"R1:{reason}"
    reason = _multipack_veto(query, sku)
    if reason is not None:
        return f"R2:{reason}"
    return None
