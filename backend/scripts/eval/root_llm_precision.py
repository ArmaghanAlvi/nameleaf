"""
LLM-rung precision probe. READ-ONLY -- no root_llm_attempts rows, no
sense_translations writes.

METHOD (unchanged from Breakdown 4.5 Step 5): sample English senses that HAVE
a resolved curated translation link (correct answers KNOWN), ask the LLM as if
no link existed, resolve its proposals in memory, and check agreement with the
linked lexemes. Same favorable-sample caveat -- which is the point: an
identical exam makes the numbers comparable.

Breakdown I Step 5 adds --compare, the batching A/B. Both arms run against the
SAME sampled senses in the SAME session, so model-version drift and sampling
luck cancel:

  single   one propose_translations call per LINKED language (the measured
           path, unchanged)
  batched  ONE propose_translations_batch call per sense, over the linked
           languages PADDED out to --pad-to, so the batch size resembles
           production's thin-language set rather than the 1-3 languages that
           happen to carry a curated link

Only the LINKED languages are scored; the padding exists to make the model's
job realistically hard, not to be measured.

RESOLUTION IS DELIBERATELY THE OLD, SIMPLE ONE (lowest-id lexeme +
_display_sense), not production's ranked multi-candidate pick. Both arms use
it, so the delta is sound, and the absolute numbers stay comparable to the
Breakdown 4.5 baseline they will be read against.

USAGE (from backend/):
  python3 scripts/eval/root_llm_precision.py --n 60
  python3 scripts/eval/root_llm_precision.py --compare --n 60 --pad-to 19 --yes
"""
from __future__ import annotations

import argparse, os, sys
from collections import defaultdict

from sqlalchemy import select, text

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal                          # noqa: E402
from app.models.generated_name import Language                   # noqa: E402
from app.models.semantic import Lexeme, Sense                    # noqa: E402
from app.services.root_llm import (                              # noqa: E402
    propose_translations, propose_translations_batch,
)
from app.services.root_selection import _display_sense           # noqa: E402
from app.utils.text import normalize_lemma                       # noqa: E402

_SAMPLE_SQL = text("""
SELECT s.sense_id FROM (
  SELECT st.sense_id
  FROM sense_translations st
  JOIN languages l ON l.id = st.language_id
  JOIN senses sn ON sn.id = st.sense_id
  JOIN sense_embeddings se ON se.sense_id = sn.id
  WHERE l.code = ANY(:targets)
    AND st.target_lexeme_id IS NOT NULL
    AND st.attachment <> 'llm'
    AND sn.visibility_status = 'visible'
  GROUP BY st.sense_id
) s
ORDER BY random() LIMIT :n
""")

_LINKS_SQL = text("""
SELECT st.sense_id, l.code, array_agg(DISTINCT st.target_lexeme_id) AS lex_ids
FROM sense_translations st
JOIN languages l ON l.id = st.language_id
WHERE st.sense_id = ANY(:sense_ids)
  AND l.code = ANY(:targets)
  AND st.target_lexeme_id IS NOT NULL
  AND st.attachment <> 'llm'
GROUP BY st.sense_id, l.code
""")


def _resolve(db, code: str, lang_id: int, words: list[str]) -> list[int]:
    out: list[int] = []
    for w in words:
        lex_id = db.scalar(
            select(Lexeme.id)
            .where(Lexeme.language_id == lang_id,
                   Lexeme.normalized_lemma == normalize_lemma(w, code))
            .order_by(Lexeme.id).limit(1))
        if lex_id is not None and _display_sense(db, lex_id):
            out.append(lex_id)
    return out


def _score(stats, code: str, resolved: list[int], linked: set[int]) -> None:
    stats[(code, "asked")] += 1
    if not resolved:
        return
    stats[(code, "resolved_any")] += 1
    if resolved[0] in linked:
        stats[(code, "top1_hit")] += 1
    if set(resolved) & linked:
        stats[(code, "any_hit")] += 1


def _report(label: str, stats, codes: list[str]) -> dict[str, float]:
    print(f"\n--- {label} ---")
    out: dict[str, float] = {}
    tot_r = tot_any = 0
    for code in codes:
        a = max(stats[(code, "asked")], 1)
        r = max(stats[(code, "resolved_any")], 1)
        p_any = stats[(code, "any_hit")] / r
        out[code] = p_any
        tot_r += stats[(code, "resolved_any")]
        tot_any += stats[(code, "any_hit")]
        print(f"{code}: asked={stats[(code, 'asked')]} "
              f"resolve_rate={stats[(code, 'resolved_any')] / a:.0%} "
              f"precision_top1={stats[(code, 'top1_hit')] / r:.0%} "
              f"precision_any={p_any:.0%}")
    out["__pooled__"] = tot_any / max(tot_r, 1)
    print(f"POOLED precision_any = {out['__pooled__']:.1%}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=60, help="senses sampled")
    ap.add_argument("--targets", nargs="+",
                    default=["la", "ru", "ja", "ar"])
    ap.add_argument("--compare", action="store_true",
                    help="run BOTH arms and print the batching delta")
    ap.add_argument("--pad-to", type=int, default=19,
                    help="batch size the batched arm is padded out to")
    ap.add_argument("--yes", action="store_true",
                    help="skip the cost confirmation")
    args = ap.parse_args()

    with SessionLocal() as db:
        db.execute(text("SET lock_timeout = '30s'"))
        langs = {row.code: row for row in db.scalars(
            select(Language).where(Language.code.isnot(None)))
            if row.code is not None}
        pad_pool = [c for c in sorted(langs)
                    if c != "en" and c not in args.targets]

        sense_ids = [i for (i,) in db.execute(
            _SAMPLE_SQL, {"targets": args.targets, "n": args.n})]
        links: dict[int, dict[str, set[int]]] = defaultdict(dict)
        for sid, code, lex_ids in db.execute(
                _LINKS_SQL, {"sense_ids": sense_ids,
                             "targets": args.targets}):
            links[sid][code] = set(lex_ids)

        n_single = sum(len(v) for v in links.values())
        n_batch = len(sense_ids) if args.compare else 0
        print(f"sampled {len(sense_ids)} senses, {n_single} linked pairs")
        print(f"estimated API calls: single={n_single} batched={n_batch} "
              f"total={n_single + n_batch}")
        if not args.yes:
            if input("proceed? [y/N] ").strip().lower() != "y":
                return

        single, batched = defaultdict(int), defaultdict(int)
        omissions = misses = 0

        for sid in sense_ids:
            sense = db.get(Sense, sid)
            assert sense is not None, f"sense {sid} vanished mid-run"
            gloss = ((sense.definition or "").strip()
                     or (sense.raw_glosses[0] if sense.raw_glosses else ""))
            lemma = sense.lexeme.lemma
            pos = sense.lexeme.part_of_speech
            linked_codes = sorted(links[sid])

            for code in linked_codes:
                try:
                    words, _m = propose_translations(
                        lemma=lemma, pos=pos, gloss=gloss,
                        language_name=langs[code].name)
                except Exception as exc:
                    single[(code, "errors")] += 1
                    print(f"  [single {code}] {sid}: {exc}", file=sys.stderr)
                    continue
                _score(single, code,
                       _resolve(db, code, langs[code].id, words),
                       links[sid][code])

            if not args.compare:
                continue

            ask = linked_codes + [
                c for c in pad_pool[:max(args.pad_to - len(linked_codes), 0)]]
            try:
                got, _m = propose_translations_batch(
                    lemma=lemma, pos=pos, gloss=gloss,
                    languages=[(c, langs[c].name) for c in ask])
            except Exception as exc:
                for code in linked_codes:
                    batched[(code, "errors")] += 1
                print(f"  [batch] {sid}: {exc}", file=sys.stderr)
                continue
            misses += len(ask)
            omissions += sum(1 for c in ask if c not in got)
            for code in linked_codes:
                _score(batched, code,
                       _resolve(db, code, langs[code].id, got.get(code, [])),
                       links[sid][code])

    s = _report("SINGLE (one call per language)", single, args.targets)
    if not args.compare:
        return
    b = _report(f"BATCHED (padded to {args.pad_to})", batched, args.targets)
    print(f"\nomission rate: {omissions}/{misses} "
          f"({omissions / max(misses, 1):.1%}) of asked codes absent "
          f"from the batched response")

    print("\n--- DELTA (batched - single), precision_any ---")
    worst = 0.0
    for code in args.targets + ["__pooled__"]:
        d = (b[code] - s[code]) * 100
        worst = min(worst, d)
        print(f"  {code:<11} {d:+.1f} pp")
    pooled = (b["__pooled__"] - s["__pooled__"]) * 100
    verdict = "PASS" if (pooled >= -5.0 and worst >= -10.0) else "FAIL"
    print(f"\nGATE: pooled {pooled:+.1f}pp (>= -5.0), "
          f"worst language {worst:+.1f}pp (>= -10.0)  ->  {verdict}")


if __name__ == "__main__":
    main()