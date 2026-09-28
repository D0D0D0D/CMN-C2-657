"""APPI special-care-required categories + fair-hiring prohibited attributes.

docs/02_design.md §4-2 / Open Item OI-1. This is the single source of truth for
the detection dictionary. Every category below is covered by a dedicated TC in
docs/03_test_spec.md (OI-1 mandatory requirement).

Detection is dictionary-based (deterministic, testable, verifiable) — NOT an LLM
"best effort" scrub. Matching is done AFTER NFKC normalization + casefold so that
fullwidth/halfwidth and case variants cannot bypass the filter (docs/02 §4-2,
banned-word guard requirement).

The design intent is *fail-safe drop at the input boundary*: when any of these
lexemes is detected in a candidate field, that field is NOT passed into the
evaluation pipeline. Only the fact that a field was dropped (its category code)
is recorded in intake_warnings — never the offending content.

customer legal review of dictionary coverage & retention is a documented
deployment precondition (docs/07 / OI-2).
"""

from __future__ import annotations

import unicodedata

# ---------------------------------------------------------------------------
# APPI §2 special-care-required personal information (要配慮個人情報) — ALL categories,
# plus fair-hiring (公正採用選考) prohibited attributes per 労働施策総合推進法 (docs/02 §4-3).
# Each category maps to a list of Japanese + English lexemes (lowercase / normalized).
# ---------------------------------------------------------------------------

SENSITIVE_CATEGORIES: dict[str, list[str]] = {
    # --- APPI §2 要配慮個人情報 ---
    "race": ["人種", "民族", "race", "ethnicity", "ethnic origin"],
    "creed": ["信条", "思想", "宗教", "信仰", "creed", "religion", "religious", "belief", "ideology"],
    "social_status": ["社会的身分", "門地", "social status", "social standing"],
    "medical_history": ["病歴", "既往歴", "持病", "medical history", "illness history", "disease history"],
    "disability": ["障害", "障がい", "身体障害", "精神障害", "発達障害", "disability", "handicap", "impairment"],
    "health_checkup": ["健康診断", "健診結果", "検査結果", "health checkup", "medical examination", "health screening"],
    "medical_treatment": ["診療", "調剤", "診療録", "カルテ", "処方", "服薬", "medical treatment", "prescription", "clinical record", "medical record"],
    "criminal_record": ["犯罪歴", "前科", "逮捕歴", "criminal record", "criminal history", "conviction", "arrest record"],
    "crime_victim": ["犯罪被害", "被害事実", "crime victim", "victim of crime"],
    # --- fair-hiring prohibited attributes (公正採用選考上の禁忌属性) ---
    "domicile_birthplace": ["本籍", "出生地", "出身地", "domicile", "birthplace", "place of origin"],
    "family": ["家族構成", "家庭環境", "家族の職業", "生活環境", "family composition", "household", "family background"],
    "labor_union": ["労働組合", "組合加入", "組合活動", "labor union", "trade union", "union membership"],
    "marriage_childbirth": ["結婚予定", "出産予定", "妊娠", "婚姻状況", "marriage plan", "childbirth plan", "pregnancy", "marital status"],
    "nationality": ["国籍", "母国", "在留資格", "nationality", "citizenship", "country of citizenship"],
    "age": ["年齢", "生年月日", "生年", "age", "date of birth", "birth date", "birthdate", "d.o.b"],
    "gender": ["性別", "gender", "sex"],
}


def _normalize(text: str) -> str:
    """NFKC-normalize + casefold so fullwidth/halfwidth + case variants match.

    Defends against bypass via e.g. fullwidth 'ＲＡＣＥ' or 'Race' vs 'race'.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def _is_ascii(s: str) -> bool:
    return all(ord(ch) < 128 for ch in s)


def _lexeme_matches(norm_text: str, lexeme: str) -> bool:
    """Match a lexeme against normalized text.

    ASCII lexemes match on WORD BOUNDARIES (so 'age' does not fire inside
    'manager' / 'usage' / 'storage' — real false-positive class that would
    over-drop legitimate job text). Japanese/mixed lexemes match as substrings
    (no word boundaries in Japanese).
    """
    norm_lex = _normalize(lexeme)
    if not norm_lex:
        return False
    if _is_ascii(norm_lex):
        import re
        return re.search(rf"(?<![a-z0-9]){re.escape(norm_lex)}(?![a-z0-9])", norm_text) is not None
    return norm_lex in norm_text


def detect_sensitive_categories(text: str) -> list[str]:
    """Return the sorted list of category codes whose lexemes appear in ``text``.

    Deterministic dictionary scan. Empty text -> empty list. The caller (S-2 gate
    / allowlist projection) drops the field and records only the returned category
    codes in intake_warnings — never the matched substring.
    """
    if not text or not isinstance(text, str):
        return []
    norm = _normalize(text)
    hits: set[str] = set()
    for category, lexemes in SENSITIVE_CATEGORIES.items():
        for lex in lexemes:
            if _lexeme_matches(norm, lex):
                hits.add(category)
                break
    return sorted(hits)


def contains_sensitive(text: str) -> bool:
    """True if any sensitive/prohibited lexeme is present (after normalization)."""
    return bool(detect_sensitive_categories(text))


def prefilter_payload(payload: dict) -> tuple[dict, list[str]]:
    """Adapter-boundary prefilter: drop dict fields whose value carries a sensitive lexeme.

    WHY THIS EXISTS (docs/02 §5-0 PB spec): the checkpointer persists raw
    ``user_input`` at the first super-step — BEFORE the in-graph S-2 allowlist
    projection runs. An in-graph-only drop therefore cannot satisfy §5-0's
    "no prohibited-attribute lexeme in ANY checkpoint" invariant. This prefilter
    runs in the API adapter (graph-外) so sensitive lexemes never enter State at
    all; the S-2 projection in pre_process remains as defense-in-depth.

    Returns ``(filtered_payload, warnings)`` — warnings carry only field names +
    category codes, never content. Nested ``rows``/``records`` lists are filtered
    per-row with the same rule.
    """
    if not isinstance(payload, dict):
        return payload, []
    out: dict = {}
    warnings: list[str] = []
    for key, value in payload.items():
        if isinstance(value, str):
            cats = detect_sensitive_categories(value)
            if cats:
                warnings.append(f"prefiltered_sensitive:{key}:{'|'.join(cats)}")
                continue
            out[key] = value
        elif isinstance(value, list) and key in ("rows", "records"):
            rows = []
            for i, row in enumerate(value):
                if isinstance(row, dict):
                    frow, fwarn = prefilter_payload(row)
                    rows.append(frow)
                    warnings.extend(f"row{i}:{w}" for w in fwarn)
                else:
                    rows.append(row)
            out[key] = rows
        else:
            out[key] = value
    return out, warnings


def mask_sensitive(text: str) -> tuple[str, list[str]]:
    """Mask sensitive lexemes in free text with a lexeme-free ``[配慮語句除去]`` marker.

    Used by the adapter for non-JSON free-text input and for HITL resume feedback,
    where field-drop semantics do not apply (dropping would destroy the human's
    instruction). Returns ``(masked_text, category_codes)``.

    The marker deliberately contains NO category code: English codes like "age" /
    "gender" are themselves detection lexemes, so embedding them in the marker
    would re-trigger detection downstream (S-3 re-scan, allowlist projection) and
    over-redact the surrounding text. Category codes are returned separately for
    warnings/audit (codes only — never content).
    """
    if not text or not isinstance(text, str):
        return text, []
    cats = detect_sensitive_categories(text)
    if not cats:
        return text, []
    masked = text
    for category, lexemes in SENSITIVE_CATEGORIES.items():
        if category not in cats:
            continue
        for lex in lexemes:
            # NFKC/casefold-insensitive replace: scan via normalized comparison window.
            masked = _replace_normalized(masked, lex, "[配慮語句除去]")
    return masked, sorted(cats)


def _replace_normalized(text: str, lexeme: str, replacement: str) -> str:
    """Replace occurrences of ``lexeme`` in ``text`` (NFKC+casefold insensitive).

    ASCII lexemes respect word boundaries (same rule as _lexeme_matches) so that
    e.g. 'age' is not masked inside 'manager'.
    """
    norm_lex = _normalize(lexeme)
    if not norm_lex:
        return text
    ascii_lex = _is_ascii(norm_lex)

    def _is_word_char(ch: str) -> bool:
        return ch.isascii() and (ch.isalnum() or ch == "_")

    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Try to match the lexeme starting at i under normalization.
        matched_len = 0
        for j in range(i + 1, min(n, i + len(lexeme) * 3) + 1):
            window_norm = _normalize(text[i:j])
            if window_norm == norm_lex:
                matched_len = j - i
                break
            if not norm_lex.startswith(window_norm):
                break
        if matched_len and ascii_lex:
            # Enforce word boundaries for ASCII lexemes.
            before_ok = i == 0 or not _is_word_char(text[i - 1])
            after_ok = i + matched_len >= n or not _is_word_char(text[i + matched_len])
            if not (before_ok and after_ok):
                matched_len = 0
        if matched_len:
            result.append(replacement)
            i += matched_len
        else:
            result.append(text[i])
            i += 1
    return "".join(result)
