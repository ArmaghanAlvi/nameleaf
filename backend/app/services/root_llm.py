"""
LLM translation proposer + resolver for root selection's `llm` rung
(Breakdown 4.5). The model PROPOSES candidate target-language words for one
English sense; admission requires resolving through the SAME discipline as
every curated rung: normalize_lemma -> lexeme in target language -> viable
display sense. The model proposes, the database disposes.

Provider-agnostic over HTTP (httpx). Default request shape: Google Gemini
generateContent (free tier), JSON response mode. Swap providers by editing
_request() and _extract_text() only.

Config (env):
  ROOT_LLM_API_KEY      required for live calls
  ROOT_LLM_MODEL        gemini-flash-lite-latest
  ROOT_LLM_RPM          per-process politeness cap, default 8
  ROOT_LLM_QUERY_TIME   '1' to allow live resolution in the query path
                        (decision 1d; default OFF -- backfill is primary)

CLI smoke (from backend/):
  python3 -m app.services.root_llm --lemma light \
      --gloss "electromagnetic radiation that enables sight" \
      --pos noun --target ru
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

from app.config import BACKEND_DIR

load_dotenv(BACKEND_DIR / ".env")
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.generated_name import Language
from app.models.semantic import (
    Lexeme, RootLlmAttempt, Sense, SenseTranslation, Source,
)
from app.utils.text import normalize_lemma

ROOT_LLM_MODEL = os.environ.get("ROOT_LLM_MODEL", "gemini-flash-lite-latest")
ROOT_LLM_RPM = int(os.environ.get("ROOT_LLM_RPM", "8"))
QUERY_TIME_LIVE = os.environ.get("ROOT_LLM_QUERY_TIME", "0") == "1"

_PROMPT = """You are a bilingual lexicographer. Give the standard {language} \
translation(s) of the English word below, in the specific sense given.

English word: {lemma} ({pos})
Sense: {gloss}

Return ONLY a JSON array of 1 to 3 strings: the most standard {language} \
words for exactly this sense, as dictionary lemma (citation) forms in native \
script. Most standard first. No romanization, no explanations. If no good \
translation exists, return []."""

# Batched form of _PROMPT (Stage 16b). Used for TWO OR MORE languages only;
# n == 1 keeps _PROMPT deliberately -- that is the prompt the Breakdown 4.5
# Step 5 precision numbers were measured against, and after Stage 17b the
# query-time trickle will routinely ask for one or two languages. Changing
# the shape of the already-measured path to save one branch would make every
# stored precision figure describe a prompt that no longer runs.
_BATCH_PROMPT = """You are a multilingual lexicographer. Give the standard \
translation(s) of the English word below, in the specific sense given, for \
EACH of the target languages listed.

English word: {lemma} ({pos})
Sense: {gloss}

Target languages:
{language_list}

Return ONLY a JSON object whose keys are exactly the language codes listed \
above -- every code, none omitted, none invented. Each value is an array of \
1 to 3 strings: the most standard words in that language for exactly this \
sense, as dictionary lemma (citation) forms in native script. Most standard \
first. No romanization, no explanations. If no good translation exists for a \
language, return [] for that language."""


_last_call = 0.0


def _throttle() -> None:
    global _last_call
    gap = 60.0 / max(ROOT_LLM_RPM, 1)
    wait = _last_call + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def can_call_now() -> bool:
    """True only if a call right now would NOT have to sleep inside
    _throttle(). The query path (parallel_expand's live trickle, decision
    1d) must take an LLM call opportunistically or skip it -- a root found
    this way is worth ~0ms of added latency, never the 7.5s throttle gap or
    a 20s 429-retry sleep. The backfill script does not use this: it has no
    user waiting and should throttle/retry normally."""
    gap = 60.0 / max(ROOT_LLM_RPM, 1)
    return time.monotonic() >= _last_call + gap


def _request(prompt: str, response_schema: dict | None = None) -> dict:
    key = os.environ.get("ROOT_LLM_API_KEY")
    if not key:
        raise RuntimeError("ROOT_LLM_API_KEY not set")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{ROOT_LLM_MODEL}:generateContent?key={key}")
    gen_cfg: dict = {"temperature": 0,
                     "responseMimeType": "application/json"}
    if response_schema is not None:
        gen_cfg["responseSchema"] = response_schema
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    _throttle()
    r = httpx.post(url, json=body, timeout=30.0)
    if r.status_code == 429:          # one polite retry on rate limit
        time.sleep(20)
        _throttle()
        r = httpx.post(url, json=body, timeout=30.0)
    r.raise_for_status()
    return r.json()


def _extract_text(data: dict) -> str:
    return data["candidates"][0]["content"]["parts"][0]["text"]


def propose_translations(*, lemma: str, pos: str, gloss: str,
                         language_name: str) -> tuple[list[str], str]:
    """Returns (proposals, actual_served_model_version) -- 'latest' aliases
    silently resolve to a concrete version at request time (Breakdown 4.5
    finding, 7/24/26); the served version is what gets persisted, not the
    alias, so a future alias rotation doesn't retroactively blur history."""
    prompt = _PROMPT.format(language=language_name, lemma=lemma,
                            pos=pos or "word", gloss=gloss)
    response = _request(prompt)
    text_out = _extract_text(response).strip()
    if text_out.startswith("```"):
        text_out = text_out.strip("`").removeprefix("json").strip()
    parsed = json.loads(text_out)
    served_model = response.get("modelVersion", ROOT_LLM_MODEL)
    if not isinstance(parsed, list):
        raise ValueError(f"non-list response: {parsed!r}")
    return [str(w).strip() for w in parsed if str(w).strip()][:3], served_model


def _batch_schema(codes: list[str]) -> dict:
    """responseSchema for the batched call: an OBJECT with one
    array-of-strings property per requested code, ALL required.

    Declaring the key set is what turns an omitted language from a silent
    miss into an API-side rejection. Codes are validated because a property
    name the API cannot accept would fail the whole call for every language
    in it -- all 21 corpus codes are alphanumeric today, and this assertion
    is what notices if a 22nd is not.
    """
    for c in codes:
        if not c.isalnum():
            raise ValueError(f"language code unsafe as a schema key: {c!r}")
    return {
        "type": "OBJECT",
        "properties": {c: {"type": "ARRAY", "items": {"type": "STRING"}}
                       for c in codes},
        "required": list(codes),
        "propertyOrdering": list(codes),
    }


def _parse_batch_payload(parsed: object,
                         codes: list[str]) -> dict[str, list[str]]:
    """Requested-code-keyed proposals from a decoded batch response.

    NEVER invents a key. The returned dict may be missing requested codes;
    reconciling that against the requested list is the CALLER's job
    (resolve_llm_roots), because a missing key must become an 'unresolved'
    LEDGER ROW, and a function that quietly filled in [] here would hide the
    one failure mode batching actually introduces.
    """
    if not isinstance(parsed, dict):
        raise ValueError(f"non-object response: {parsed!r}")
    wanted = set(codes)
    out: dict[str, list[str]] = {}
    for key, value in parsed.items():
        if key not in wanted or not isinstance(value, list):
            continue
        out[key] = [str(w).strip() for w in value if str(w).strip()][:3]
    return out


def propose_translations_batch(
    *, lemma: str, pos: str, gloss: str,
    languages: list[tuple[str, str]],
) -> tuple[dict[str, list[str]], str]:
    """ONE call, N languages. `languages` is [(code, display name), ...].

    Returns ({code: proposals}, served_model_version). Same served-version
    discipline as propose_translations: the alias resolves at request time
    and the CONCRETE version is what gets persisted.
    """
    codes = [c for c, _ in languages]
    listing = "\n".join(f"  {c} = {name}" for c, name in languages)
    prompt = _BATCH_PROMPT.format(lemma=lemma, pos=pos or "word", gloss=gloss,
                                  language_list=listing)
    response = _request(prompt, response_schema=_batch_schema(codes))
    text_out = _extract_text(response).strip()
    if text_out.startswith("```"):
        text_out = text_out.strip("`").removeprefix("json").strip()
    served_model = response.get("modelVersion", ROOT_LLM_MODEL)
    return _parse_batch_payload(json.loads(text_out), codes), served_model


def _llm_source_id(db: Session) -> int:
    src = db.scalars(select(Source).where(Source.slug == "llm-root")).first()
    if src is None:
        src = Source(
            slug="llm-root",
            name="LLM root translations",
            # source_type is NOT NULL. New category alongside the existing
            # 'wordnet' / 'wiktionary' / 'dictionary_dump' values -- keeps
            # generated evidence sortable apart from curated corpora.
            source_type="llm",
            notes="Gemini-proposed translations, DB-resolution gated "
                  "(Breakdown 4.5). Never curated evidence.",
        )
        db.add(src)
        db.flush()
    return src.id


def _attempt_rows(db: Session, sense_id: int, entries: list[dict]) -> None:
    """ONE ledger row per ASKED language, upserted on the pair constraint.

    THIS IS THE RESOLVE-ONCE CONTRACT. _THIN_SQL skips a pair only when a
    row exists with a skip status, so a language that was asked and got no
    row is re-asked on every future run, forever -- batching would then
    INCREASE quota burn while appearing to reduce it. Bulk upsert rather
    than the single path's read-modify-write: with up to 19 rows per call
    the Python-side prior lookup becomes 19 round trips for no benefit, and
    ON CONFLICT is what makes an 'error' retry idempotent.
    """
    if not entries:
        return
    stmt = pg_insert(RootLlmAttempt).values(
        [{"sense_id": sense_id, **e} for e in entries]
    )
    db.execute(stmt.on_conflict_do_update(
        constraint="uq_root_llm_attempts_pair",
        set_={
            "status": stmt.excluded.status,
            "model": stmt.excluded.model,
            "proposed": stmt.excluded.proposed,
            "resolved_lexeme_id": stmt.excluded.resolved_lexeme_id,
            "error_detail": stmt.excluded.error_detail,
        },
    ))


def _resolve_one_language(
    db: Session, *, lang_id: int, lang_code: str, words: list[str],
    en_vector, en_lemma: str, en_definition: str | None,
    english_sense_id: int, source_id: int,
) -> int | None:
    """Lifted VERBATIM from resolve_llm_root's inner loop so the batched and
    single paths cannot drift: the multi-candidate walk, the effective-score
    ranking, the deliberate absence of a swap margin, and the stale-NULL
    curated repair (decision 1e) are all unchanged in behaviour.

    Runs inside the CALLER's savepoint. `source_id` is passed in rather than
    resolved here because _llm_source_id flushes an insert on first use, and
    creating that Source inside a savepoint that later rolls back would make
    every subsequent language re-create it.
    """
    from app.services.root_selection import _display_sense_scored

    resolved: list[tuple[float, str, int]] = []      # (sim, word, lex_id)
    for word in words:
        norm = normalize_lemma(word, lang_code)
        # normalize_lemma casefolds (and NFD-folds Latin macrons), so one
        # normalized form can map to SEVERAL lexemes -- la 'canis' noun
        # (viable) vs 'Canis' name (no visible+embedded sense). Rank every
        # viable candidate; do not take the lowest id blind. NO swap margin
        # here, deliberately: the margin protects a CURATED stored link from
        # a near-tied sibling, and there is no incumbent in this path.
        cand_ids = [i for (i,) in db.execute(
            select(Lexeme.id)
            .where(Lexeme.language_id == lang_id,
                   Lexeme.normalized_lemma == norm)
            .order_by(Lexeme.id)
        )]
        best: tuple[float, float, str, int] | None = None  # (eff,sim,word,id)
        for lex_id in cand_ids:
            scored = _display_sense_scored(db, lex_id, en_vector,
                                           en_lemma, en_definition)
            if scored is None:
                continue
            _disp, eff_score, sim = scored
            if best is None or eff_score > best[0]:
                best = (eff_score, sim, word, lex_id)
        if best is not None:
            resolved.append((best[1], best[2], best[3]))

    if not resolved:
        return None
    _sim, word, lex_id = max(resolved, key=lambda r: r[0])
    db.execute(
        pg_insert(SenseTranslation).values(
            sense_id=english_sense_id, language_id=lang_id,
            target_text=word,
            target_normalized=normalize_lemma(word, lang_code),
            target_lexeme_id=lex_id, attachment="llm",
            source_id=source_id,
        ).on_conflict_do_update(
            constraint="uq_sense_translations_link",
            set_={"target_lexeme_id": lex_id},
            where=SenseTranslation.target_lexeme_id.is_(None),
        )
    )
    return lex_id


def resolve_llm_roots(
    db: Session, *, english_sense_id: int, language_codes: list[str],
) -> dict[str, int | None]:
    """
    Batched sibling of resolve_llm_root: ONE API call covering every
    still-unresolved language for one English sense. Returns
    {code: resolved_lexeme_id | None} for EVERY requested code, whether it
    was asked or short-circuited.

    Same resolve-once contract as the single path -- 'resolved' and
    'unresolved' short-circuit with no call, 'error' retries -- but the
    blast radius of a failure is now up to 19 pairs instead of one, so
    failure is scoped three ways:

      transport / parse     every ASKED language -> 'error'
      per-language failure  that language only   -> 'error', via SAVEPOINT
      omitted by the model  that language only   -> 'unresolved'

    THE SAVEPOINT IS NOT DECORATION. db.rollback() is SESSION-wide, so
    reusing the single path's blanket rollback here would discard up to 18
    successful resolutions to salvage one failure.
    """
    out: dict[str, int | None] = {c: None for c in language_codes}
    sense = db.get(Sense, english_sense_id)
    if sense is None or not language_codes:
        return out

    # Snapshot as plain values: a rollback EXPIRES ORM attributes, and
    # re-loading them mid-recovery is what turned a simple DB error into
    # PendingRollbackError (Breakdown 4.5, 7/24/26).
    langs = {row.code: (row.id, row.name) for row in db.scalars(
        select(Language).where(Language.code.in_(language_codes)))}
    lang_ids = {code: v[0] for code, v in langs.items()}

    prior = {row.language_id: row for row in db.scalars(
        select(RootLlmAttempt).where(
            RootLlmAttempt.sense_id == english_sense_id,
            RootLlmAttempt.language_id.in_(list(lang_ids.values()))))}

    ask: list[tuple[str, str]] = []
    for code in language_codes:
        if code not in langs:
            continue
        p = prior.get(lang_ids[code])
        if p is not None and p.status != "error":
            out[code] = p.resolved_lexeme_id
            continue
        ask.append((code, langs[code][1]))
    if not ask:
        return out

    gloss = (sense.definition or "").strip() or \
            (sense.raw_glosses[0] if sense.raw_glosses else "")
    en_lemma = sense.lexeme.lemma
    en_pos = sense.lexeme.part_of_speech
    en_definition = sense.definition

    try:
        if len(ask) == 1:
            # The MEASURED path (Breakdown 4.5 Step 5). Kept for n == 1 so
            # the query-time trickle's common shape after Stage 17b keeps
            # its known precision instead of inheriting an unmeasured one.
            code, name = ask[0]
            words, served_model = propose_translations(
                lemma=en_lemma, pos=en_pos, gloss=gloss, language_name=name)
            proposals = {code: words}
        else:
            proposals, served_model = propose_translations_batch(
                lemma=en_lemma, pos=en_pos, gloss=gloss, languages=ask)
    except Exception as exc:
        db.rollback()
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        print(f"[root_llm] batch sense={english_sense_id} "
              f"codes={[c for c, _ in ask]}: {detail}", file=sys.stderr)
        _attempt_rows(db, english_sense_id, [
            {"language_id": lang_ids[c], "status": "error",
             "model": ROOT_LLM_MODEL, "proposed": [],
             "resolved_lexeme_id": None, "error_detail": detail}
            for c, _ in ask
        ])
        db.commit()
        return out

    from app.services.root_selection import _en_vector
    en_vector = _en_vector(db, english_sense_id)   # ONCE, not per language
    source_id = _llm_source_id(db)                 # outside every savepoint

    entries: list[dict] = []
    for code, _name in ask:
        lang_id = lang_ids[code]
        words = proposals.get(code, [])            # omitted -> [] -> unresolved
        status, lex_id, detail = "unresolved", None, None
        try:
            with db.begin_nested():
                lex_id = _resolve_one_language(
                    db, lang_id=lang_id, lang_code=code, words=words,
                    en_vector=en_vector, en_lemma=en_lemma,
                    en_definition=en_definition,
                    english_sense_id=english_sense_id, source_id=source_id)
            if lex_id is not None:
                status = "resolved"
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:2000]
            status, lex_id = "error", None
            print(f"[root_llm] {code} sense={english_sense_id}: {detail}",
                  file=sys.stderr)
        out[code] = lex_id
        entries.append({
            "language_id": lang_id, "status": status, "model": served_model,
            "proposed": words, "resolved_lexeme_id": lex_id,
            "error_detail": detail,
        })

    # Structural tripwire, not a log line: one entry per asked language is
    # guaranteed by construction TODAY, and this is what notices the day a
    # `continue` gets added to the loop above.
    if len(entries) != len(ask):
        raise RuntimeError(
            f"ledger grain broken: asked {len(ask)}, writing {len(entries)}")

    _attempt_rows(db, english_sense_id, entries)
    db.commit()
    return out


def resolve_llm_root(db: Session, *, english_sense_id: int,
                     language_code: str) -> int | None:
    """Single-language wrapper over resolve_llm_roots.

    Kept so parallel_expansion.py and any external caller do not move in
    this stage. The batched function is now the ONLY implementation, and
    routes n == 1 through the original _PROMPT internally, so this call is
    behaviourally what it always was.
    """
    return resolve_llm_roots(
        db, english_sense_id=english_sense_id,
        language_codes=[language_code],
    ).get(language_code)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lemma", required=True)
    ap.add_argument("--gloss", required=True)
    ap.add_argument("--pos", default="noun")
    ap.add_argument("--target", required=True)
    a = ap.parse_args()
    _NAMES = {"la": "Latin", "ru": "Russian", "ja": "Japanese", "ar": "Arabic"}
    words, served = propose_translations(lemma=a.lemma, pos=a.pos,
                                         gloss=a.gloss,
                                         language_name=_NAMES[a.target])
    print(words, "| served by:", served)