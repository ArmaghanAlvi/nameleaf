"""
Green-card census N2 — MEANING coverage (read-only, DB-side).

The names are cheap; the MEANINGS are the scarce resource. This probe measures,
per name TYPE (given or surname, never combined), how many names can be given
an English meaning by each machine-derivable channel, as an EXCLUSIVE waterfall
(priority order) plus a non-exclusive per-channel total.

Channels, in priority order:
  1 GLOSS_MEANING   gloss carries meaning/literally + a quoted string
  2 GLOSS_EQUIV_EN  gloss says "equivalent to English <n>"
  3 ETYM_MARKER     etymology_text carries meaning/literally + a quoted string
  4 ETYM_QUOTED     etymology_text carries any quoted gloss (looser; measured
                    separately so the precision/recall point can be CHOSEN)
  5 HOMOGRAPH       the name's normalized_lemma is also a VISIBLE non-name
                    lexeme in the same language (meaning arrives by identity)
  6 NONE            residue — ships blank, retrievable only by exact match

USAGE (from backend/):
  python3 scripts/prune/name_meaning_probe.py --name-type given --lang la --examples 12
  python3 scripts/prune/name_meaning_probe.py --name-type given --all
  python3 scripts/prune/name_meaning_probe.py --name-type all --all
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal              # noqa: E402
from app.models.generated_name import Language       # noqa: E402
from app.models.semantic import Lexeme, Sense        # noqa: E402
from scripts.prune.name_inventory_probe import (     # noqa: E402
    NAME_TYPE_CHOICES,
    classify_name_type,
    expand_type_arg,
    shipping_types,
)


# ---------------------------------------------------------------------------
# MOVED to app/services/established_names.py (Breakdown B, Step 5). Re-export
# only -- see the note in name_inventory_probe.py for why.
# ---------------------------------------------------------------------------
from app.services.established_names import (           # noqa: E402,F401
    ANY_QUOTED,
    CHANNELS,
    DIAGNOSTIC_CHANNELS,
    EQUIV_EN,
    ETYM_QUOTED_BLOCKED_LANGS,
    MEANING_MARKER,
    extract_equiv_en,
    extract_etym_marker,
    extract_etym_quoted,
    extract_etym_quoted_legacy,
    extract_gloss_meaning,
)


def visible_non_name_lemmas(db, lang_id: int) -> set[str]:
    """Normalized lemmas of this language that have at least one VISIBLE sense
    on a non-name lexeme — i.e. the shadow-join right-hand side."""
    rows = db.execute(
        select(Lexeme.normalized_lemma)
        .join(Sense, Sense.lexeme_id == Lexeme.id)
        .where(
            Lexeme.language_id == lang_id,
            Lexeme.part_of_speech != "name",
            Sense.visibility_status == "visible",
        )
        .distinct()
    ).scalars().all()
    return set(rows)


def sample_add(bucket: list, item, cap: int) -> None:
    if len(bucket) < cap:
        bucket.append(item)


def run_language(db, lang_id: int, code: str, cap: int, limit: int | None,
                 wanted_types: tuple[str, ...]):
    shadow = visible_non_name_lemmas(db, lang_id)

    waterfall: Counter = Counter()
    per_channel: Counter = Counter()      # non-exclusive
    samples: dict[str, list] = {c: [] for c in CHANNELS}
    channels_per_name: Counter = Counter()
    considered = 0

    # 1c: legacy-vs-repaired ETYM_QUOTED, measured in the same pass so the
    # precision/recall trade is a number rather than an assertion.
    _DELTA_KEYS = ("both_same", "both_differ", "legacy_only", "repaired_only")
    etym_delta: Counter = Counter()
    etym_delta_samples: dict[str, list] = {k: [] for k in _DELTA_KEYS}
    dithematic = 0

    stmt = (
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .options(selectinload(Sense.lexeme))
        .where(
            Lexeme.language_id == lang_id,
            Lexeme.part_of_speech == "name",
        )
        .order_by(Sense.id)
    )

    for sense in db.scalars(stmt).yield_per(2000):
        if limit is not None and considered >= limit:
            break
        gloss = (sense.definition or "").strip()
        tags = list(sense.raw_tags or [])
        if classify_name_type(gloss, tags) not in wanted_types:
            continue
        considered += 1

        lex = sense.lexeme
        etym = sense.etymology_text or ""

        hits: dict[str, str] = {}
        v = extract_gloss_meaning(gloss)
        if v:
            hits["GLOSS_MEANING"] = v
        v = extract_equiv_en(gloss)
        if v:
            hits["GLOSS_EQUIV_EN"] = v
        v = extract_etym_marker(etym)
        if v:
            hits["ETYM_MARKER"] = v
        repaired = extract_etym_quoted(etym, code)
        if repaired:
            hits["ETYM_QUOTED"] = repaired
            if " + " in repaired:
                dithematic += 1
        legacy_v = extract_etym_quoted_legacy(etym)
        if legacy_v:
            per_channel["ETYM_QUOTED_LEGACY"] += 1
        if legacy_v and not repaired:
            _key = "legacy_only"
        elif repaired and not legacy_v:
            _key = "repaired_only"
        elif repaired and legacy_v:
            _key = "both_same" if repaired == legacy_v else "both_differ"
        else:
            _key = None
        if _key:
            etym_delta[_key] += 1
            sample_add(etym_delta_samples[_key],
                       (lex.lemma, legacy_v, repaired, (etym or "")[:70]), cap)
        if lex.normalized_lemma in shadow:
            hits["HOMOGRAPH"] = lex.normalized_lemma

        for c in hits:
            per_channel[c] += 1
        channels_per_name[len(hits)] += 1

        chosen = "NONE"
        for c in CHANNELS[:-1]:
            if c in hits:
                chosen = c
                break
        waterfall[chosen] += 1
        sample_add(
            samples[chosen],
            (lex.lemma, hits.get(chosen, ""), gloss[:46]),
            cap,
        )

    def pct(x, d=considered):
        return f"{100 * x / d:.2f}%" if d else "n/a"

    print("=" * 72)
    print(f"LANG {code}   type={'+'.join(wanted_types)}   "
          f"senses considered: {considered}   shadow set: {len(shadow)}")
    print("=" * 72)
    print("--- EXCLUSIVE WATERFALL (first channel that fires) ---")
    for c in CHANNELS:
        print(f"  {c:<16} {waterfall[c]:>8} ({pct(waterfall[c])})")
        for lemma, val, g in samples[c]:
            print(f"      {lemma!r} -> {val!r}   [{g}]")
    covered = considered - waterfall["NONE"]
    print()
    print(f"  ANY CHANNEL .... {covered} ({pct(covered)})")
    strict = (waterfall["GLOSS_MEANING"] + waterfall["GLOSS_EQUIV_EN"]
              + waterfall["ETYM_MARKER"] + waterfall["HOMOGRAPH"])
    print(f"  WITHOUT the loose ETYM_QUOTED channel .... {strict} "
          f"({pct(strict)})")
    print()
    print("--- NON-EXCLUSIVE per-channel totals (overlap visible here) ---")
    for c in CHANNELS[:-1] + DIAGNOSTIC_CHANNELS:
        print(f"  {c:<22} {per_channel[c]:>8} ({pct(per_channel[c])})")
    print("--- channels available per name ---")
    for k in sorted(channels_per_name):
        print(f"  {k} channel(s): {channels_per_name[k]}")
    print()
    print("--- 1c ETYM_QUOTED REPAIR DELTA (legacy vs repaired) ---")
    print("  both_same    = repair agreed with legacy")
    print("  both_differ  = repair changed the text (dithematic gain lives here)")
    print("  legacy_only  = repair REJECTED what legacy accepted (precision gain)")
    print("  repaired_only= repair found what legacy missed (should be rare)")
    for k in _DELTA_KEYS:
        print(f"  {k:<16} {etym_delta[k]:>8} ({pct(etym_delta[k])})")
        for lemma, lg, rp, e in etym_delta_samples[k]:
            print(f"      {lemma!r}")
            print(f"          legacy   = {lg!r}")
            print(f"          repaired = {rp!r}")
            print(f"          etym     = {e!r}")
    print(f"  multi-span (dithematic) repaired hits ... {dithematic}")
    print()

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--name-type", choices=NAME_TYPE_CHOICES, default="given",
        help="'all' runs GIVEN, SURNAME and PATRONYMIC as fully separate "
             "report passes per language -- never combined into one count.",
    )
    args = ap.parse_args()

    type_args = expand_type_arg(args.name_type)

    with SessionLocal() as db:
        rows = db.execute(
            select(Language.id, Language.code).order_by(Language.code)
        ).all()
        wanted = set(args.lang)
        for type_arg in type_args:
            wanted_types = shipping_types(type_arg)
            for lang_id, code in rows:
                if not code:
                    continue
                if not args.all and code not in wanted:
                    continue
                run_language(db, lang_id, code, args.examples, args.limit,
                             wanted_types)


if __name__ == "__main__":
    main()