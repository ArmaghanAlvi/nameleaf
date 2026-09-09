"""
The LLM origin pass -- prompt, schema, normalisation, reconciliation.

TRANSPORT IS BORROWED, NOT REBUILT. Every call goes through
root_llm._request. Two HTTP clients in one process would each believe it
owned the whole RPM budget, and _throttle's module-level _last_call would
be tracking half the traffic. The daily ceiling is already recorded as
fragile when several call sites stack in one session (§22.13).

THREE PASSES, NOT TWO. A and B are the same question with the batch
re-ordered, which perturbs context without changing the question. C runs
only over rows A and B disagreed on, and its batch is composed ENTIRELY of
rows that already defeated two passes -- a much stronger perturbation than
a shuffle, and a possible bias toward "unknown" from its neighbours. That
is measured in the pilot (20g), not assumed.

ASYMMETRIC TRUST, AND ITS ONE APPARENT EXCEPTION. At the A/B stage an
English verdict wins outright: the row already reads English, so accepting
it changes nothing and risks nothing, while moving a name OFF English is
the change that is visible and can be wrong. One vote to stay, two
agreeing votes to move.

Reaching pass C therefore means NEITHER A nor B said English -- two
independent votes to move. A lone English third vote does not overturn
them. It fails to break the tie, and the row lands in `other`. That is the
same rule applied to a different evidence set, not an exception to it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.services import root_llm

# English's own ancestral stages, folded to "English" for English host rows
# ONLY. See D-5.
#
# WHY THE FLOOR EXISTS. `ang` is one of the 21 corpus languages, so "Old
# English" is INSIDE the closed vocabulary and a truthful deepest-origin
# answer for Smith (OE smith), Baker (baecere) and Ashley (aesc + leah) is
# Old English. Surnames are 78.4% of visible green cards (G-4), so without
# the fold the MODAL English surname card files under a language nobody
# searching in English is looking at. Structurally it is John -> Hebrew,
# except it is the common case and harder to spot because the wrong answer
# is in-vocabulary rather than out.
#
# WHY A FOLD AND NOT PROMPT-ONLY. The prompt still states the floor. The
# fold exists so the shipped column is right either way, which lets the
# 20i-2 gate read the RAW strings and answer "on what share did the prompt
# hold" across every pilot surname, instead of being a pass/fail on four
# seeded controls that cannot see a tail rate.
#
# OLD NORSE IS DELIBERATELY ABSENT. Danelaw surnames genuinely are Old
# Norse and `non` is a corpus language in its own right. Frankish is absent
# for the same reason: it is not an English ancestral stage.
_ANCESTRAL_ENGLISH = frozenset({
    "old english", "middle english", "anglo-saxon", "anglo saxon",
    "proto-west germanic", "proto-germanic",
})

# Filled ONLY from the 20h out-of-vocabulary census, never by hand from
# intuition. An empty map is the correct starting state: every entry has to
# be paid for by a frequency count in real pilot output. A hand-guessed
# alias map is per-row curation wearing a rule's clothes.
_ALIASES: dict[str, str] = {}

_DECLINE_TOKENS = frozenset({
    "", "unknown", "unk", "none", "n/a", "na", "uncertain", "unclear",
})

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


# --- prompts ---------------------------------------------------------------

_SHARED_RULES = """
Answer for EVERY item. Prefer "unknown" over a low-confidence guess -- an
honest "unknown" is more useful here than a plausible wrong answer.

A name coined inside a naming community takes the PRIMARY LANGUAGE of that
community, not the country it was coined in. Nevaeh and Jaylen are English.

Prefer a language from this list:
{vocabulary}

If the true origin is genuinely not on the list, give the real language
name anyway and set "in_vocabulary" to false.

Item fields: "gender" is "m" (masculine), "f" (feminine) or "x" (unisex),
and is absent when unrecorded. "meaning" is the meaning already recorded
for this name. "also_attested_in" lists other languages in which the SAME
spelling is an established name -- evidence about adaptation, not an
answer.

Return ONLY a JSON object keyed by exactly the item ids below: every id,
none omitted, none invented.

Names:
{items}
"""

_GIVEN_PROMPT = """You are an onomastician. For each GIVEN NAME below,
answer the ADAPTATION question: does an English form of this name exist
in the English naming tradition?

Answer "English" when it does, even where the name ultimately derives from
another language. Abigail is English -- Hebrew Avigayil became Abigail.
John, Michael and Sarah are English.

Answer the other language when the English spelling IS that language's own
form of the name, merely written in Latin script. Amal is Arabic.
Nadezhda is Russian. Akansha is Hindi.
""" + _SHARED_RULES

_SURNAME_PROMPT = """You are an onomastician. For each SURNAME below, give
its DEEPEST LINGUISTIC ORIGIN -- the language its elements come from, not
the language of the country where the family later lived.

ONE FLOOR: English's own ancestral stages COUNT AS ENGLISH. Old English,
Middle English and Proto-Germanic all answer "English". Smith, Baker,
Ashley and Whitfield are English, not Old English.

Old Norse is NOT covered by that floor. A surname from Norse settlement in
England is Old Norse.
""" + _SHARED_RULES


def build_prompt(name_type: str, vocabulary: list[str],
                 items: list[tuple[str, dict]]) -> str:
    """`items` is [(token, item_payload(...)), ...] in batch order.

    PATRONYMICS TAKE THE SURNAME PROMPT (5 rows corpus-wide). They are
    inherited rather than chosen, which is the property the surname
    prompt's deepest-origin question is actually about.

    sort_keys=True so a re-run produces a byte-identical prompt. The whole
    argument for sharing name_origin.item_payload between the pilot and the
    pass is that they ask about the same thing; a dict whose key order
    varies would quietly undermine that.
    """
    template = _GIVEN_PROMPT if name_type == "given" else _SURNAME_PROMPT
    rendered = "\n".join(
        f"  {token} = {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        for token, payload in items
    )
    return template.format(
        vocabulary="\n".join(f"  - {v}" for v in vocabulary
                             if v.lower() != "other"),
        items=rendered,
    )


# --- response schema -------------------------------------------------------

_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "origin": {"type": "STRING"},
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "is_coined": {"type": "BOOLEAN"},
        "in_vocabulary": {"type": "BOOLEAN"},
    },
    "required": ["origin", "confidence", "is_coined", "in_vocabulary"],
}


def origin_schema(tokens: list[str]) -> dict:
    """Keyed by ITEM TOKEN -- not by name, not by database id.

    Same reasoning as root_llm._batch_schema: naming the exact key set in
    `required` turns an omission into an API-SIDE rejection rather than a
    silent short response. Stage 16 measured 0/1,067 omissions once that
    was in place.

    Names cannot be keys: `Hope` exists as both a given name and a surname,
    and lemmas carry characters that are not safe property names. Database
    ids cannot be keys either -- they would leak into the prompt and make
    the pass-B shuffle unreadable in a saved artifact.
    """
    for token in tokens:
        if not token.isalnum():
            raise ValueError(f"unsafe schema key: {token!r}")
    return {
        "type": "OBJECT",
        "properties": {t: _ITEM_SCHEMA for t in tokens},
        "required": list(tokens),
        "propertyOrdering": list(tokens),
    }


# --- verdicts --------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """One pass's answer for one row.

    THREE fields where one might seem enough, on purpose:
      origin   the backend marker -- a vocabulary name, 'other', or None
      display  what a badge would show -- 'English', 'Turkish', or None
      raw      exactly what the model said, before any fold or alias

    `raw` exists so the 20i-2 floor gate and the 20h vocabulary census read
    the model's own words rather than our normalisation of them.
    """
    origin: str | None
    display: str | None
    raw: str | None
    confidence: str | None
    is_coined: bool | None


def normalize_origin(raw: str | None, vocabulary: list[str],
                     host_language_name: str) -> tuple[str | None, str | None]:
    """-> (backend marker, badge string). (None, None) is a decline."""
    s = (raw or "").strip().strip(".").strip()
    lowered = s.lower()
    if lowered in _DECLINE_TOKENS:
        return None, None
    if (host_language_name.lower() == "english"
            and lowered in _ANCESTRAL_ENGLISH):
        return host_language_name, host_language_name
    alias = _ALIASES.get(lowered)
    if alias:
        s, lowered = alias, alias.lower()
    canon = {v.lower(): v for v in vocabulary if v.lower() != "other"}
    if lowered in canon:
        return canon[lowered], canon[lowered]
    return "other", s


def parse_batch(parsed: object, tokens: list[str], vocabulary: list[str],
                host_language_name: str) -> dict[str, Verdict]:
    """Raises on a missing or malformed entry -- the caller decides what a
    partial batch means, because the answer differs between the pilot
    (discard the arm) and the pass (write those rows as retryable errors).
    """
    if not isinstance(parsed, dict):
        raise ValueError("origin batch response was not an object")
    out: dict[str, Verdict] = {}
    for token in tokens:
        entry = parsed.get(token)
        if not isinstance(entry, dict):
            raise ValueError(f"missing or malformed entry for {token}")
        raw = entry.get("origin")
        origin, display = normalize_origin(raw, vocabulary,
                                           host_language_name)
        conf = entry.get("confidence")
        coined = entry.get("is_coined")
        out[token] = Verdict(
            origin=origin, display=display,
            raw=str(raw).strip() if raw is not None else None,
            confidence=conf if conf in CONFIDENCE_RANK else None,
            is_coined=coined if isinstance(coined, bool) else None,
        )
    return out


# --- reconciliation --------------------------------------------------------

@dataclass(frozen=True)
class Reconciled:
    status: str            # resolved | unknown | disagreed
    origin: str | None
    origin_raw: str | None
    confidence: str | None
    is_coined: bool | None
    in_vocabulary: bool | None
    agreement: str         # native|agreed|declined|disagreed|on_c|no_majority


def _same(a: Verdict, b: Verdict) -> bool:
    """Agreement, with the one precision `other` forces.

    Two DIFFERENT out-of-vocabulary answers both normalise to 'other' and
    would otherwise read as agreement. For 'other', agreement means the
    same raw string, case-folded.
    """
    if a.origin is None or b.origin is None or a.origin != b.origin:
        return False
    if a.origin != "other":
        return True
    return (a.display or "").casefold() == (b.display or "").casefold()


def _weaker(*verdicts: Verdict) -> str | None:
    """The LOWER confidence of the agreeing passes. A later strict display
    bar would filter on this column, so the conservative read is the useful
    one -- 'high' from one pass does not survive 'low' from the other."""
    present = [v.confidence for v in verdicts if v.confidence]
    return min(present, key=lambda c: CONFIDENCE_RANK[c]) if present else None


def _coined(*verdicts: Verdict) -> bool | None:
    values = [v.is_coined for v in verdicts]
    if any(v is None for v in values):
        return None
    return all(values)


def reconcile_ab(a: Verdict, b: Verdict, *,
                 host_language_name: str) -> Reconciled:
    if host_language_name in (a.origin, b.origin):
        return Reconciled("resolved", host_language_name, host_language_name,
                          _weaker(a, b), _coined(a, b), True, "native")
    if _same(a, b):
        return Reconciled("resolved", a.origin, a.display, _weaker(a, b),
                          _coined(a, b), a.origin != "other", "agreed")
    if a.origin is None and b.origin is None:
        return Reconciled("unknown", None, None, None, None, None, "declined")
    # Includes "one pass answered, the other declined". That is not a
    # decline -- one pass HAD an answer -- so it is owed a third call, and
    # a matching C resolves it two-of-three.
    return Reconciled("disagreed", None, None, None, None, None, "disagreed")


def reconcile_c(a: Verdict, b: Verdict, c: Verdict) -> Reconciled:
    """Majority or nothing. See the module docstring on English."""
    for earlier in (a, b):
        if _same(earlier, c):
            return Reconciled("resolved", c.origin, c.display,
                              _weaker(earlier, c), _coined(earlier, c),
                              c.origin != "other", "on_c")
    return Reconciled("unknown", None, None, None, None, None, "no_majority")


# --- the call --------------------------------------------------------------

def propose_origins(name_type: str, vocabulary: list[str],
                    items: list[tuple[str, dict]], *,
                    host_language_name: str
                    ) -> tuple[dict[str, Verdict], str, dict]:
    """One batch, one call. Returns ({token: Verdict}, served_model, usage).

    `usage` is the API's own usageMetadata (promptTokenCount,
    candidatesTokenCount) when present, else {} -- real token counts, not
    an estimate, so a dollar ceiling built on this is accurate rather than
    a guess compounding a guess.
    """
    tokens = [t for t, _ in items]
    prompt = build_prompt(name_type, vocabulary, items)
    response = root_llm._request(prompt, origin_schema(tokens))
    text_out = root_llm._extract_text(response).strip()
    if text_out.startswith("```"):
        text_out = text_out.strip("`").removeprefix("json").strip()
    parsed = json.loads(text_out)
    served = response.get("modelVersion", root_llm.ROOT_LLM_MODEL)
    usage = response.get("usageMetadata", {})
    return (parse_batch(parsed, tokens, vocabulary, host_language_name),
            served, usage)