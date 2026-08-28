"""Deterministic hard-rule veto for the SAME title-match verdict.

The model prompt is the primary judge, but two historically validated rule
classes fail-close a model SAME:

* R1 model-code single-sided / conflicting — the standard product carries an
  explicit strong model code (AR-EP65, 91403, G50-2TB, DN20, ...) that the
  candidate title does not express in any equivalent spelling;  the standard
  side extracts codes separator-aware ("DS 320" is the code DS320, not two
  unrelated words) through a conservative pairwise fusion with wholesale-
  collapse guards;
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


_UNIT_LETTERS = "MM|CM|KM|KG|MG|ML|W|V|A|MAH|WH|KWH|RPM|PPI|DPI|NM|UM"
_MEASURE_RE = re.compile(
    rf"(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*({_UNIT_LETTERS})(?![A-Z0-9])",
    re.IGNORECASE,
)
# Standalone storage capacities ("1TB 7200转") are specs, but a storage unit
# hanging off a hyphen/slash-joined code segment (G50-2TB) is part of the
# model code: the wider negative lookbehind keeps code-internal storage
# suffixes intact for the token extractor.
_STORAGE_UNIT_LETTERS = "TB|GB|MB"
_STORAGE_RE = re.compile(
    rf"(?<![A-Z0-9_/\-])(\d+(?:\.\d+)?)\s*({_STORAGE_UNIT_LETTERS})(?![A-Z0-9])",
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
    upper = _STORAGE_RE.sub(" ", upper)
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


# The ONLY model-code prefixes that may ever be shed from a fused token.
# A letter prefix by itself is not brand evidence (AB-12345 vs CD-12345 are
# different products; an unknown ABDN20/XX91403/ADEP65 fusion cannot be
# verified), so prefix shedding is restricted to series prefixes explicitly
# validated by this project's regression suite.  Anything unlisted fails
# closed: the hard rule is the double-insurance, not a brand guesser.
_VALIDATED_SERIES_PREFIXES = ("SATA", "BIZHUB")


def _equivalent_model_forms(token: str) -> set[str]:
    """Spelling-equivalent forms of one model code.

    Universal normalization lives here: separators removed, uppercased, and
    the one validated storage-suffix flex TB<->T (G50-2TB == G50-2T,
    1TB == 1T).  Stripping a storage suffix down to a bare stem (G50-2TB ->
    G502, ABC128GB -> ABC128) is NOT an equivalence — that deletes the
    capacity — and GB/MB have no validated flex at all.  Prefix shedding is
    limited to _VALIDATED_SERIES_PREFIXES with a strong remainder
    (SATA-91403 == 91403, BIZHUB225I == 225I)."""
    compact = token.replace("-", "").replace("_", "").replace("/", "")
    forms = {compact}
    for prefix in _VALIDATED_SERIES_PREFIXES:
        if compact.startswith(prefix):
            remainder = compact[len(prefix):]
            if _is_strong_token(remainder):
                forms.add(remainder)
    if compact.endswith("TB"):
        forms.add(compact[:-2] + "T")
    elif compact.endswith("T") and not compact.endswith(("AT", "ET", "GT", "ST", "XT")):
        forms.add(compact[:-1] + "TB")
    return forms


def _model_codes_equivalent(query_token: str, sku_token: str) -> bool:
    """Token-level exact-equivalence: same normalized code, the validated
    TB<->T storage flex, or a validated series prefix shed — never a mere
    substring and never a guessed brand prefix.

    This is what keeps DN20 from matching DN200, 91403 from matching 914030,
    AB-12345 from matching CD-12345 and G50-2TB from matching G50-2, while
    SATA-91403 still matches 91403 and G50-2TB still matches G50-2T."""
    return bool(_equivalent_model_forms(query_token) & _equivalent_model_forms(sku_token))


# Query-side separator-aware extraction.  _MODEL_TOKEN_RE never spans a
# space, so the standard title "得力 DS 320 订书机" would yield no strong
# token at all (DS has no digit, 320 is too short) — every verdict on such a
# query is a vacuous pass and standard_model_codes_present stays False.
# Closing that hole requires fusing separator-joined pieces, which must stay
# conservative: a limited, pairwise, left-to-right fusion of ADJACENT weak
# pieces, accepted only when the fused form is provably code-shaped.  Each
# guard below rejects one historically real fabrication class:
#   * either piece strong alone -> never re-fuse an already complete code
#     (SATA 91403 / bizhub 225i keep their current extraction untouched);
#   * fused form without letters -> adjacent counts ("1000 800") stay counts;
#   * last piece without a digit -> marketing-word tails ("G502 HERO");
#   * piece shaped like a spaced measure ("70g", "2T") -> specs, not codes;
#   * pure-letter unit piece first ("MM 300") -> unit-before-number spec;
#   * digit-bearing first piece with a pure-digit second ("2TB 7200rpm")
#     -> capacity followed by a spin speed/count, not one code;
#   * any alphabetic run longer than 4 inside the fused form ("HP LaserJet
#     1020") -> English-word series names never wholesale-collapse.
_MAX_FUSED_LETTER_RUN = 4
_SPEC_MEASURE_PIECE_RE = re.compile(r"\d+(?:\.\d+)?[GWTVALMK]")
_UNIT_ABBREVIATION_WORDS = frozenset(_UNIT_LETTERS.split("|")) | {
    "G", "W", "V", "A", "M", "T", "L", "K", "TB", "GB", "MB",
}
_ALNUM_PIECE_RE = re.compile(r"[A-Z0-9]+")
_PIECE_SEPARATOR_RE = re.compile(r"[-_/ ]+")


def _max_letter_run(form: str) -> int:
    return max((len(run) for run in re.findall(r"[A-Z]+", form)), default=0)


def _fusable_pair(first: str, second: str) -> bool:
    if _is_strong_token(first) or _is_strong_token(second):
        return False
    compact = first + second
    if not _is_strong_token(compact):
        return False
    if not any(character.isalpha() for character in compact):
        return False
    if not any(character.isdigit() for character in second):
        return False
    if _SPEC_MEASURE_PIECE_RE.fullmatch(first) or _SPEC_MEASURE_PIECE_RE.fullmatch(second):
        return False
    if first.isalpha() and first in _UNIT_ABBREVIATION_WORDS:
        return False
    if any(character.isdigit() for character in first) and not any(
        character.isalpha() for character in second
    ):
        return False
    return _max_letter_run(compact) <= _MAX_FUSED_LETTER_RUN


def _spaced_fusion_tokens(text: str) -> list[str]:
    """Greedy left-to-right fusion of adjacent separator-joined weak pieces.

    Runs on the raw (unit-preserving) upper text — a spaced trailing
    capacity ("G50 2TB") is stripped as a standalone spec before token
    extraction, so only this pass can see it."""
    raw = _to_halfwidth(text).upper()
    pieces = [
        (match.group(0), match.start(), match.end())
        for match in _ALNUM_PIECE_RE.finditer(raw)
    ]
    tokens: list[str] = []
    index = 0
    while index + 1 < len(pieces):
        first, _first_start, first_end = pieces[index]
        second, second_start, _second_end = pieces[index + 1]
        between = raw[first_end:second_start]
        if between and _PIECE_SEPARATOR_RE.fullmatch(between) and _fusable_pair(first, second):
            tokens.append(first + second)
            index += 2
            continue
        index += 1
    return tokens


def _query_strong_model_tokens(query_str: str) -> list[str]:
    """Strong model codes of the standard title: single tokens plus the
    conservative spaced fusions, deduplicated by canonical form (DS-320 and
    its fused spelling are one code, not two)."""
    tokens = _strong_model_tokens(_normalize_for_tokens(query_str))
    known_forms: set[str] = set()
    for token in tokens:
        known_forms |= _equivalent_model_forms(token)
    for fused in _spaced_fusion_tokens(query_str):
        fused_forms = _equivalent_model_forms(fused)
        if fused_forms & known_forms:
            continue
        tokens.append(fused)
        known_forms |= fused_forms
    return tokens


# Separator tolerance for candidate titles, symmetric over the compact
# canonical form.  A strong query code collapses to its compact form
# (DS-320 -> DS320; validated TB<->T and SATA/BIZHUB prefix flexes included
# via _equivalent_model_forms), and the candidate title proves the code when
# a strictly alnum-bounded run of characters — with any mix of [-_/ ]
# connector characters (or none) between them — deletes down to exactly that
# compact form.  This is direction-symmetric: it does not matter which side
# fused the code and which side inserted separators.  The boundary checks
# are what keep it from being a substring match (DS 320 inside DS 3200, EP65
# inside ADEP65, 91403 inside XX91403, G502TB vs G50-2 all stay conflicts),
# and because the run must equal an already-validated strong query code
# exactly, ordinary adjacent title words can never be fused into a code.
_SEPARATOR_TOLERANCE_RE = r"[-_/ ]*"


def _separator_insensitive_pattern(form: str) -> re.Pattern[str]:
    body = _SEPARATOR_TOLERANCE_RE.join(re.escape(character) for character in form)
    return re.compile(rf"(?<![A-Z0-9]){body}(?![A-Z0-9])")


def _expressed_separator_insensitive(token: str, sku_name: str) -> bool:
    """Does the candidate title express the code as a boundary-delimited run
    whose separator deletion equals one of the code's canonical forms?

    Searched on the raw (unit-preserving) candidate text: a space-joined
    trailing capacity ("G50 2TB") is stripped as a standalone spec before
    token extraction, so only this text-level pass can see it."""
    text = _to_halfwidth(str(sku_name or "")).upper()
    if not text:
        return False
    return any(
        _separator_insensitive_pattern(form).search(text)
        for form in _equivalent_model_forms(token)
    )


def _model_code_expressed(token: str, sku_name: str) -> bool:
    """A strong query code is expressed when a candidate strong token is
    exactly equivalent (fused spellings, TB<->T, validated prefix shed) OR
    the candidate text realizes one of the code's canonical forms as a
    boundary-delimited run with connector characters allowed anywhere
    between its characters."""
    sku_tokens = _strong_model_tokens(_normalize_for_tokens(sku_name))
    if any(_model_codes_equivalent(token, sku_token) for sku_token in sku_tokens):
        return True
    return _expressed_separator_insensitive(token, sku_name)


def standard_model_codes_present(query_str: str, sku_name: str) -> bool:
    """True when every strong model code of the standard title has an
    exactly-equivalent code in the candidate title.

    This is the deterministic form of prompt rule 7's "除非标题已明确当前
    SKU 就是 query_str 的目标型号" branch; the shared risk-word guard uses it
    instead of trusting the word list alone.  The standard side uses the
    separator-aware extractor so a spaced spelling ("DS 320") proves its
    model instead of vacuously extracting nothing.
    """
    query_tokens = _query_strong_model_tokens(query_str)
    if not query_tokens:
        return False
    return all(_model_code_expressed(token, sku_name) for token in query_tokens)


def _model_code_veto(query_str: str, sku_name: str) -> str | None:
    query_tokens = _query_strong_model_tokens(query_str)
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
