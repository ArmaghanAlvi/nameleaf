"""
The LLM origin pass -- Stage 20 pilot and Stage 22 full run, one script.

WHY ONE SCRIPT. The pilot's six gates are only meaningful if the pilot
batched, called, parsed and reconciled exactly the way the pass will. A
separate pilot script would be a thing to keep in sync rather than a thing
that is true -- the same argument that put input assembly in
app/services/name_origin.py.

MODES
  --mode pilot   stratified sample, one JSON artifact per batch-size arm,
                 WRITES NOTHING TO THE LEDGER. Four arms times three passes
                 is up to twelve verdicts per row against a ledger that
                 holds one row per name; the artifact keeps them apart.
  --mode commit  read one arm's artifact, write those verdicts to the
                 ledger. Stage 21 needs real rows to render against, and
                 these are already paid for.
  --mode full    Stage 22. Resumable, quota-guarded, ledger-writing.
  --mode census  read-only distribution report over the ledger (22e).

BATCHES ARE TYPE-HOMOGENEOUS. There are two prompts, so a batch is
all-given or all-surname. Patronymics ride with surnames (5 rows).

THREE PASSES, TWO PHASES. A and B run per batch with the batch re-ordered
between them. C runs after, over a queue of disagreements collected across
the whole chunk -- so a pass-C batch is composed entirely of hard cases.
That is a stronger perturbation than the shuffle, deliberately, and it is
measured in the pilot rather than assumed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import text                                    # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.db.session import SessionLocal                        # noqa: E402
from app.models.generated_name import Language                 # noqa: E402
from app.models.semantic import NameOriginAttempt              # noqa: E402
from app.services import name_origin as assembly               # noqa: E402
from app.services import name_origin_llm as nol                # noqa: E402

HOST = "en"
SEED = 20260831   # fixed so a re-run draws the same sample and shuffle


# gemini-3.5-flash-lite, paid tier, Standard, per the pricing page current
# as of this writing. Re-check https://ai.google.dev/gemini-api/docs/pricing
# before a long paid run -- these numbers move.
_PRICE_PER_TOKEN = {"input": 0.30 / 1_000_000, "output": 2.50 / 1_000_000}


def call_cost(usage: dict) -> float:
    """Real cost of one call, from the API's own token counts. Returns 0.0
    for a free-tier key (usageMetadata is present either way, but the
    caller only needs this when tracking a paid-tier ceiling)."""
    prompt_tokens = usage.get("promptTokenCount", 0)
    output_tokens = usage.get("candidatesTokenCount", 0)
    return (prompt_tokens * _PRICE_PER_TOKEN["input"] +
            output_tokens * _PRICE_PER_TOKEN["output"])


# --- batching --------------------------------------------------------------

def token_for(index: int) -> str:
    return f"i{index}"


def batches(items, size):
    """Type-homogeneous batches, in 19d order within each type.

    Partitioned in PYTHON rather than by a SQL name_type filter so the 19d
    meaning-first ordering is preserved inside each type without a second
    query -- and so a change to the ordering rule stays in one place.
    """
    by_type: dict[str, list] = {}
    for item in items:
        key = "given" if item.name_type == "given" else "surname"
        by_type.setdefault(key, []).append(item)
    for name_type, rows in sorted(by_type.items()):
        for start in range(0, len(rows), size):
            yield name_type, rows[start:start + size]


def run_batch(name_type, rows, vocabulary, host_name, *, shuffle_seed=None):
    order = list(range(len(rows)))
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(order)
    items = [(token_for(i), assembly.item_payload(rows[i])) for i in order]
    verdicts, served, usage = nol.propose_origins(
        name_type, vocabulary, items, host_language_name=host_name)
    return ({rows[i].established_name_id: verdicts[token_for(i)]
             for i in order}, served, usage)



def is_schema_ceiling(exc: Exception) -> bool:
    """A bare 400 from the API. §23.4 finding 1: the responseSchema size
    ceiling presents this way, with no field violation and no detail. We
    cannot distinguish it from any other malformed request, and we do not
    need to -- splitting is the right response to both."""
    return (isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code == 400)


def ab_pair(name_type, rows, vocabulary, host_name, *, splits, min_size=5):
    try:
        a, served, usage_a = run_batch(name_type, rows, vocabulary, host_name)
        b, _, usage_b = run_batch(name_type, rows, vocabulary, host_name,
                                  shuffle_seed=SEED + len(rows))
        cost = call_cost(usage_a) + call_cost(usage_b)
        return [(rows, a, b, served, cost)], 2
    except Exception as exc:                            # noqa: BLE001
        if not is_schema_ceiling(exc) or len(rows) <= min_size:
            raise
        mid = len(rows) // 2
        splits.append(len(rows))
        print(f"  schema ceiling at {len(rows)}; splitting "
              f"{mid}/{len(rows) - mid}")
        left, lc = ab_pair(name_type, rows[:mid], vocabulary, host_name,
                           splits=splits, min_size=min_size)
        right, rc = ab_pair(name_type, rows[mid:], vocabulary, host_name,
                            splits=splits, min_size=min_size)
        return left + right, 2 + lc + rc

# --- sampling (pilot) ------------------------------------------------------

CONTROLS = {
    "given": ["abigail", "john", "michael", "sarah", "hannah", "amal",
              "nadezhda", "akansha", "siobhan", "xiulan", "nadia",
              "nevaeh", "jaylen", "braxton"],
    "surname": ["smith", "baker", "ashley", "whitfield", "thatcher",
                "nakamura", "kowalski", "okonkwo", "petrov"],
}


def stratified_sample(items, per_stratum):
    """Four strata: {given, surname} x {twin-positive, twin-negative}.

    Twin presence is the split because it is the one piece of evidence the
    payload carries that the model cannot get from the name itself, and
    §22.9 measured it firing on only 7.8% of hosts -- so a flat sample
    would put almost no twin-positive rows in front of the gate.
    """
    strata: dict[tuple[str, bool], list] = {}
    for item in items:
        key = ("given" if item.name_type == "given" else "surname",
               bool(item.twin_languages))
        strata.setdefault(key, []).append(item)
    rng = random.Random(SEED)
    sample, report = [], {}
    for key in sorted(strata):
        pool = strata[key]
        take = min(per_stratum, len(pool))
        sample.extend(rng.sample(pool, take))
        report["/".join(str(k) for k in key)] = {
            "available": len(pool), "sampled": take}
    return sample, report


def add_controls(db, sample):
    """Controls are APPENDED to every arm so their verdicts are comparable
    across batch sizes. Duplicates against the random draw are dropped --
    a row asked about twice in one batch is a different question."""
    have = {i.established_name_id for i in sample}
    wanted = {(lemma, t) for t, lemmas in CONTROLS.items() for lemma in lemmas}
    extra = [i for i in assembly.select_pending(db)
             if (i.normalized_lemma,
                 "given" if i.name_type == "given" else "surname") in wanted
             and i.established_name_id not in have]
    return sample + extra, [i.lemma for i in extra]


# --- pilot -----------------------------------------------------------------

def run_pilot(db, args):
    vocabulary = assembly.origin_vocabulary(db)
    host_name = db.scalar(
        text("SELECT name FROM languages WHERE code = :c"), {"c": HOST})
    pending = assembly.select_pending(db, skip_ledger=True)
    sample, strata = stratified_sample(pending, args.per_stratum)
    sample, control_names = add_controls(db, sample)
    # Nothing here writes, so this just ends the transaction the reads
    # above opened -- the loop below is 20+ minutes of external HTTP work
    # with no further DB access until the artifact is written to disk.
    db.commit()

    plan = {"rows": len(sample), "strata": strata,
            "controls": control_names, "arms": args.arms,
            "calls_ab": sum(2 * math.ceil(len(sample) / a) for a in args.arms)}
    print(json.dumps(plan, indent=2))
    if args.dry_plan:
        return
    if not args.yes:
        raise SystemExit("refusing to spend quota without --yes")

    artifact = {"plan": plan, "arms": {}}
    for size in args.arms:
        arm: dict = {"batch_size": size, "verdicts": {}, "served": set(),
                     "batch_errors": []}
        disagreed = []
        for name_type, rows in batches(sample, size):
            try:
                a, served, _ = run_batch(name_type, rows, vocabulary, host_name)
                b, _, _ = run_batch(name_type, rows, vocabulary, host_name,
                                    shuffle_seed=SEED + size)
            except Exception as exc:                    # noqa: BLE001
                arm["batch_errors"].append(
                    {"type": name_type, "size": len(rows),
                     "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            arm["served"].add(served)
            for row in rows:
                nid = row.established_name_id
                rec = nol.reconcile_ab(a[nid], b[nid],
                                       host_language_name=host_name)
                arm["verdicts"][str(nid)] = {
                    "lemma": row.lemma, "name_type": row.name_type,
                    "twin": bool(row.twin_languages),
                    "a": asdict(a[nid]), "b": asdict(b[nid]),
                    "c": None, **asdict(rec)}
                if rec.status == "disagreed":
                    disagreed.append(row)

        # Pass C, over the whole arm's disagreements at once.
        for name_type, rows in batches(disagreed, size):
            try:
                c, _, _ = run_batch(name_type, rows, vocabulary, host_name,
                                    shuffle_seed=SEED + size + 1)
            except Exception as exc:                    # noqa: BLE001
                arm["batch_errors"].append(
                    {"phase": "c", "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            for row in rows:
                nid = str(row.established_name_id)
                entry = arm["verdicts"][nid]
                verdict = c[row.established_name_id]
                rec = nol.reconcile_c(
                    nol.Verdict(**entry["a"]), nol.Verdict(**entry["b"]),
                    verdict)
                entry["c"] = asdict(verdict)
                entry.update(asdict(rec))

        arm["served"] = sorted(arm["served"])
        artifact["arms"][str(size)] = arm
        print(f"arm {size}: {len(arm['verdicts'])} rows, "
              f"{len(arm['batch_errors'])} batch errors")

    Path(args.out).write_text(json.dumps(artifact, ensure_ascii=False,
                                         indent=1))
    print(f"wrote {args.out}")


def run_report(path: str) -> None:
    """Everything the six Stage-20 gates read, from one artifact."""
    art = json.loads(Path(path).read_text())
    controls = {n.lower() for names in CONTROLS.values() for n in names}

    for size, arm in sorted(art["arms"].items(), key=lambda kv: int(kv[0])):
        rows = list(arm["verdicts"].values())
        total = len(rows) or 1
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["agreement"]] = counts.get(r["agreement"], 0) + 1
        print(f"\n=== batch {size} ({len(rows)} rows, "
              f"served {arm['served']}) ===")
        for key in ("native", "agreed", "on_c", "declined", "no_majority",
                    "disagreed"):
            n = counts.get(key, 0)
            print(f"  {key:<12} {n:>5}  {100 * n / total:5.1f}%")

        # 20f: agreement per stratum.
        for name_type in ("given", "surname"):
            for twin in (True, False):
                sub = [r for r in rows if r["name_type"] == name_type
                       and r["twin"] == twin]
                if not sub:
                    continue
                agreed = sum(1 for r in sub
                             if r["agreement"] in ("native", "agreed"))
                print(f"  {name_type}/twin={twin!s:<5} "
                      f"{agreed}/{len(sub)} = {100*agreed/len(sub):5.1f}% AB")

        # 20i-2: the floor, read on RAW strings, not the fold.
        anc = [r for r in rows
               if (r["a"]["raw"] or "").lower() in nol.ANCESTRAL_ENGLISH]
        surnames = [r for r in rows if r["name_type"] == "surname"] or [None]
        print(f"  ancestral-English raw: {len(anc)} "
              f"({100*len(anc)/len(surnames):.1f}% of surnames)")

        # 20h: the out-of-vocabulary tail, by frequency.
        tail: dict[str, int] = {}
        for r in rows:
            for p in ("a", "b", "c"):
                v = r.get(p)
                if v and v["origin"] == "other" and v["display"]:
                    tail[v["display"]] = tail.get(v["display"], 0) + 1
        top = sorted(tail.items(), key=lambda kv: -kv[1])[:20]
        print(f"  out-of-vocabulary: {sum(tail.values())} verdicts, "
              f"{len(tail)} distinct")
        for name, n in top:
            print(f"    {n:>4}  {name}")

        # 20c: the seeded controls, one line each.
        print("  controls:")
        for r in sorted(rows, key=lambda r: r["lemma"].lower()):
            if r["lemma"].lower() in controls:
                print(f"    {r['lemma']:<12} {r['origin'] or r['status']:<12} "
                      f"a={r['a']['raw']!s:<14} b={r['b']['raw']!s:<14} "
                      f"c={(r['c'] or {}).get('raw')}")


def ledger_rows(db, entries, model):
    """UPSERT on the natural grain. Same on_conflict shape as
    root_llm._attempt_rows -- a re-run of the same arm must be idempotent,
    not a duplicate-key error."""
    lang_id = db.scalar(text("SELECT id FROM languages WHERE code = :c"),
                        {"c": HOST})
    payload = []
    for e in entries:
        payload.append({
            "language_id": lang_id,
            "normalized_lemma": e["normalized_lemma"],
            "name_type": e["name_type"],
            "status": e["status"],
            "pass_a_raw": (e["a"] or {}).get("raw"),
            "pass_b_raw": (e["b"] or {}).get("raw"),
            "pass_c_raw": (e["c"] or {}).get("raw"),
            "origin": e["origin"],
            "origin_raw": e["origin_raw"],
            "confidence": e["confidence"],
            "is_coined": e["is_coined"],
            "in_vocabulary": e["in_vocabulary"],
            # D-3: the CORROBORATING twin only. name_origin_twins still
            # holds every twin that was sent, so "what the model was shown"
            # is fully recoverable by join -- while `llm_twin` gets to mean
            # what it says.
            "twin_language": e.get("twin_language"),
            "source_sense_id": e.get("source_sense_id"),
            "model": model,
            "attempt_count": e.get("attempt_count", 1),
        })
    if not payload:
        return 0
    stmt = pg_insert(NameOriginAttempt).values(payload)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_name_origin_attempts_key",
        set_={c: stmt.excluded[c] for c in (
            "status", "pass_a_raw", "pass_b_raw", "pass_c_raw", "origin",
            "origin_raw", "confidence", "is_coined", "in_vocabulary",
            "twin_language", "source_sense_id", "model", "attempt_count")})
    db.execute(stmt)
    db.commit()
    return len(payload)


def error_rows(db, rows, detail: str, model: str) -> int:
    """Write a whole failed batch as retryable errors.

    PER-ROW rows, written immediately rather than left absent: without a
    ledger row the resume predicate cannot tell "never asked" from "asked
    and failed", so attempt_count would never climb and a permanently
    failing batch would be retried every session forever.

    attempt_count is incremented SQL-SIDE. Taking the excluded literal
    would reset it to 1 on every retry and the ceiling would never fire --
    which is the whole reason the column exists.
    """
    lang_id = db.scalar(text("SELECT id FROM languages WHERE code = :c"),
                        {"c": HOST})
    payload = [{
        "language_id": lang_id,
        "normalized_lemma": r.normalized_lemma,
        "name_type": r.name_type,
        "status": "error",
        "source_sense_id": r.source_sense_id,
        "model": model,
        "error_detail": detail[:1000],
        "attempt_count": 1,
    } for r in rows]
    if not payload:
        return 0
    stmt = pg_insert(NameOriginAttempt).values(payload)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_name_origin_attempts_key",
        set_={
            "status": stmt.excluded.status,
            "error_detail": stmt.excluded.error_detail,
            "model": stmt.excluded.model,
            "attempt_count": NameOriginAttempt.attempt_count + 1,
        })
    db.execute(stmt)
    db.commit()
    return len(payload)


def run_commit(db, args):
    art = json.loads(Path(args.out).read_text())
    arm = art["arms"][str(args.arm)]
    pending = {i.established_name_id: i
               for i in assembly.select_pending(db)}
    entries = []
    for nid, e in arm["verdicts"].items():
        item = pending.get(int(nid))
        if item is None:
            continue
        twin = None
        if e["origin"] and e["origin"] in (item.twin_languages or ()):
            twin = e["origin"]
        entries.append(e | {
            "normalized_lemma": item.normalized_lemma,
            "name_type": item.name_type,
            "source_sense_id": item.source_sense_id,
            "twin_language": twin,
        })
    model = (arm["served"] or ["unknown"])[0]
    print(f"committing {len(entries)} rows from arm {args.arm} "
          f"(model {model})")
    print(f"wrote {ledger_rows(db, entries, model)} ledger rows")


def load_stuck_disagreements(db, vocabulary, host_name):
    """Ledger rows already marked 'disagreed' from a prior interrupted
    session. Their A and B verdicts are real, paid-for evidence -- sending
    them back through phase one would re-ask both passes from scratch for
    no reason. Reconstructed as Verdict objects so reconcile_c can run
    directly, skipping straight to the one call actually needed.

    Confidence/is_coined are NOT reconstructable per-pass -- the ledger
    only stores raw strings, not per-pass confidence -- so these come back
    as None. reconcile_c's _weaker() already tolerates missing confidence
    by ignoring absent values, so this only slightly weakens the stored
    confidence on rows resolved this way, never breaks the reconciliation.
    """
    rows = db.execute(text("""
        SELECT n.normalized_lemma, n.name_type, n.pass_a_raw, n.pass_b_raw,
               n.source_sense_id, en.id AS established_name_id
        FROM name_origin_attempts n
        JOIN languages l ON l.id = n.language_id
        JOIN established_names en
          ON en.language_id = n.language_id
         AND en.normalized_lemma = n.normalized_lemma
         AND en.name_type = n.name_type
        WHERE l.code = :code AND n.status = 'disagreed'
    """), {"code": HOST}).all()

    twins = assembly._twin_index(db)
    out = []
    for r in rows:
        a_origin, a_disp = nol.normalize_origin(
            r.pass_a_raw, vocabulary, host_name)
        b_origin, b_disp = nol.normalize_origin(
            r.pass_b_raw, vocabulary, host_name)
        av = nol.Verdict(origin=a_origin, display=a_disp, raw=r.pass_a_raw,
                         confidence=None, is_coined=None)
        bv = nol.Verdict(origin=b_origin, display=b_disp, raw=r.pass_b_raw,
                         confidence=None, is_coined=None)
        item = assembly.OriginItem(
            established_name_id=r.established_name_id,
            lemma=r.normalized_lemma, normalized_lemma=r.normalized_lemma,
            name_type=r.name_type, gender="", meaning_text=None,
            meaning_channel=None, source_sense_id=r.source_sense_id,
            twin_languages=twins.get(
                (r.normalized_lemma, r.name_type), ()))
        out.append((item, av, bv))
    return out


def run_full(db, args):
    vocabulary = assembly.origin_vocabulary(db)
    host_name = db.scalar(
        text("SELECT name FROM languages WHERE code = :c"), {"c": HOST})

    # Drain any 'disagreed' rows a prior session left behind BEFORE
    # computing `pending`. Their A/B verdicts are already paid for.
    # select_pending has no way to know they're spoken for -- 'disagreed'
    # is deliberately absent from _SETTLED_STATUSES (Step 3) -- so without
    # this filter the SAME rows get pulled into `pending` for a redundant
    # A/B AND queued into `stuck` for pass C in the same run. That's what
    # just happened: some of the +800 error growth was two code paths
    # spending quota on the same ~970 rows.
    stuck = load_stuck_disagreements(db, vocabulary, host_name)
    stuck_ids = {item.established_name_id for item, _, _ in stuck}

    pending = [
        p for p in assembly.select_pending(
            db, skip_ledger=True, limit=args.limit_rows or None)
        if p.established_name_id not in stuck_ids
    ]

    # Error rows join PHASE ONE, not the pass-C drain: a failed batch wrote
    # a placeholder for every row in it, so there is no A/B verdict to
    # reconcile -- they owe a fresh ask. PREPENDED for the same priority
    # reasoning as Defect B's backlog drain: a --max-calls ceiling should
    # spend itself on the known, bounded backlog before opening new work.
    # Behind a flag so the default plan stays byte-identical to Stage 22's,
    # the same contract D-2 kept for skip_ledger.
    retry = assembly.select_errors(db) if args.retry_errors else []
    if retry:
        retry_ids = {r.established_name_id for r in retry}
        pending = retry + [p for p in pending
                           if p.established_name_id not in retry_ids]

    db.commit()   # see main(): do not hold a transaction through the loop

    size = args.batch_size
    planned = sum(1 for _ in batches(pending, size))
    plan = {"rows": len(pending), "batches": planned,
            "calls_ab": planned * 2, "batch_size": size,
            "max_calls": args.max_calls,
            "stuck_disagreements": len(stuck),
            "retry_errors": len(retry)}
    print(json.dumps(plan, indent=2))
    if args.dry_plan:
        return
    if not args.yes:
        raise SystemExit("refusing to spend quota without --yes")

    calls = 0
    spend = 0.0
    splits: list[int] = []
    consecutive_failures = 0

    if stuck:
        print(f"draining {len(stuck)} stuck disagreements straight to "
              f"pass C, before any new phase-one work")
        calls, spend = _drain_pass_c(
            db, list(stuck), vocabulary, host_name, size,
            calls=calls, spend=spend, args=args, phase_label="pass C (backlog)")

    def budget_left(n=1):
        return args.max_calls == 0 or calls + n <= args.max_calls

    def spend_left(estimated=0.0):
        return args.max_spend_usd <= 0 or spend + estimated <= args.max_spend_usd

    disagreed: list = []   # only freshly-disagreed rows from THIS run's phase one

    # --- phase one: A/B ----------------------------------------------------
    for name_type, rows in batches(pending, size):
        if not budget_left(2):
            print("quota guard reached; stopping cleanly")
            break
        if not spend_left():
            print(f"spend ceiling reached: ${spend:.2f} >= "
                  f"${args.max_spend_usd:.2f}; stopping cleanly")
            break
        if consecutive_failures >= 4:
            print(f"stopping: {consecutive_failures} consecutive batch "
                  f"failures, likely quota exhaustion or an outage")
            break
        try:
            chunks, spent = ab_pair(name_type, rows, vocabulary, host_name,
                                    splits=splits)
            calls += spent
            consecutive_failures = 0
        except Exception as exc:                        # noqa: BLE001
            calls += 2
            consecutive_failures += 1
            error_rows(db, rows, f"{type(exc).__name__}: {exc}",
                       "unknown")
            print(f"batch error ({name_type}, {len(rows)}): {exc}"[:300])
            continue

        for chunk_rows, a, b, served, cost in chunks:
            spend += cost
            entries = []
            for r in chunk_rows:
                nid = r.established_name_id
                rec = nol.reconcile_ab(a[nid], b[nid],
                                       host_language_name=host_name)
                twin = (rec.origin if rec.origin in (r.twin_languages or ())
                        else None)
                entries.append(asdict(rec) | {
                    "normalized_lemma": r.normalized_lemma,
                    "name_type": r.name_type,
                    "source_sense_id": r.source_sense_id,
                    "twin_language": twin, "attempt_count": 1,
                    "a": asdict(a[nid]), "b": asdict(b[nid]), "c": None})
                if rec.status == "disagreed":
                    disagreed.append((r, a[nid], b[nid]))
            ledger_rows(db, entries, served)
        print(f"{name_type} {len(rows)}: calls={calls} spend=${spend:.3f} "
              f"queued_for_c={len(disagreed)}")

    # --- phase two: freshly-disagreed rows from THIS run's phase one ------
    if disagreed:
        calls, spend = _drain_pass_c(
            db, disagreed, vocabulary, host_name, size,
            calls=calls, spend=spend, args=args, phase_label="pass C")

    print(f"done. calls={calls} spend=${spend:.3f} splits={splits or 'none'}")


def _drain_pass_c(db, disagreed, vocabulary, host_name, size, *,
                  calls, spend, args, phase_label):
    """Runs pass C over `disagreed`, writing results, returning updated
    (calls, spend). Shared by the pre-phase-one drain of the stuck backlog
    and the post-phase-one drain of freshly disagreed rows, so the
    batching/error/ledger logic exists in exactly one place rather than
    twice with a risk of drifting apart.

    Own circuit-breaker counter, local to this call -- a streak of pass-C
    failures says nothing about whether phase-one A/B is still healthy,
    and the two invocations of this function (backlog, then fresh) are
    independent runs of it.
    """
    by_id = {r.established_name_id: (r, av, bv) for r, av, bv in disagreed}
    consecutive_c_failures = 0
    for name_type, rows in batches([r for r, _, _ in disagreed], size):
        if args.max_calls and calls + 1 > args.max_calls:
            print(f"{phase_label}: quota guard reached; stopping cleanly")
            break
        if args.max_spend_usd > 0 and spend > args.max_spend_usd:
            print(f"{phase_label}: spend ceiling reached: ${spend:.2f} >= "
                  f"${args.max_spend_usd:.2f}; stopping cleanly")
            break
        if consecutive_c_failures >= 4:
            print(f"{phase_label}: stopping, {consecutive_c_failures} "
                  f"consecutive batch failures")
            break
        try:
            c, served, usage = run_batch(name_type, rows, vocabulary,
                                         host_name, shuffle_seed=SEED + 1)
            calls += 1
            consecutive_c_failures = 0
        except Exception as exc:                        # noqa: BLE001
            calls += 1
            consecutive_c_failures += 1
            # Left as `disagreed` rather than overwritten to `error`: the
            # row's A/B verdicts are real evidence and status='error' would
            # discard them. A failed pass-C attempt just means the row is
            # retried as a fresh A/B next run, not that the disagreement
            # itself was invalid.
            print(f"{phase_label} batch error ({name_type}, {len(rows)}): "
                  f"{exc}"[:300])
            continue
        entries = []
        spend += call_cost(usage)
        for r in rows:
            _, av, bv = by_id[r.established_name_id]
            cv = c[r.established_name_id]
            rec = nol.reconcile_c(av, bv, cv)
            twin = (rec.origin if rec.origin in (r.twin_languages or ())
                    else None)
            entries.append(asdict(rec) | {
                "normalized_lemma": r.normalized_lemma,
                "name_type": r.name_type,
                "source_sense_id": r.source_sense_id,
                "twin_language": twin, "attempt_count": 2,
                "a": asdict(av), "b": asdict(bv), "c": asdict(cv)})
        ledger_rows(db, entries, served)
        print(f"{phase_label} {len(rows)}: calls={calls} spend=${spend:.3f}")
    return calls, spend


def run_census(db) -> None:
    """22e. What the pass actually produced, read off the ledger and the
    applied rows. Co-located with the writer so the read shape and the
    write shape cannot drift."""
    def show(title, sql):
        print(f"\n=== {title} ===")
        for row in db.execute(text(sql)).mappings():
            print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))

    show("ledger status", """
        SELECT n.status, n.in_vocabulary, count(*) AS rows,
               count(*) FILTER (WHERE n.twin_language IS NOT NULL) AS twin,
               count(*) FILTER (WHERE n.confidence = 'low') AS low_conf,
               count(*) FILTER (WHERE n.pass_c_raw IS NOT NULL) AS via_c
        FROM name_origin_attempts n GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    show("applied origin_source", """
        SELECT en.origin_source, en.name_type, count(*) AS rows,
               count(en.display_origin_language) AS with_display
        FROM established_names en JOIN languages l ON l.id = en.language_id
        WHERE l.code = 'en'
        GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    show("top displayed origins", """
        SELECT en.display_origin_language AS lang, count(*) AS rows
        FROM established_names en JOIN languages l ON l.id = en.language_id
        WHERE l.code = 'en' AND en.display_origin_language IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 30
    """)
    show("out-of-vocabulary tail", """
        SELECT n.origin_raw, count(*) AS rows
        FROM name_origin_attempts n
        WHERE n.origin = 'other' AND n.origin_raw IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 40
    """)
    show("retry ceiling", """
        SELECT n.attempt_count, n.status, count(*) AS rows
        FROM name_origin_attempts n WHERE n.status = 'error'
        GROUP BY 1, 2 ORDER BY 1
    """)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", required=True,
                   choices=["pilot", "commit", "full", "census", "report"])
    # 100 and 50 both failed with a 400 INVALID_ARGUMENT -- an undocumented
    # Gemini responseSchema size ceiling, confirmed by bisection to sit
    # between 45 (pass) and 50 (fail) for this schema's per-item shape.
    # 40 is the largest arm kept, for margin: a production batch can carry
    # rows with longer meaning_text or larger twin-language lists than the
    # sample used to bisect, which pushes serialized size up per item.
    p.add_argument("--arms", type=int, nargs="+", default=[10, 25, 40])
    p.add_argument("--per-stratum", type=int, default=125)
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--max-calls", type=int, default=0,
                   help="hard quota guard; 0 = no ceiling")
    p.add_argument("--limit-rows", type=int, default=0)
    p.add_argument("--out", default="/tmp/origin_pilot.json")
    p.add_argument("--arm", type=int, help="which arm --mode commit writes")
    p.add_argument("--dry-plan", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--max-spend-usd", type=float, default=0.0,
                   help="hard spend ceiling in USD; 0 = no ceiling")
    p.add_argument("--retry-errors", action="store_true",
                   help="also re-offer ledger rows sitting at 'error' "
                        "(they are unreachable via select_pending once "
                        "apply_origin has written llm_error onto them)")
    args = p.parse_args()

    db = SessionLocal()
    # Same idiom as every other long-running script here (root_battery.py,
    # capture_parallel_reference.py, the probes) -- a session that opens a
    # transaction and then sits through minutes of external HTTP work gets
    # killed by Postgres otherwise. Those scripts budget 300-600s because
    # their workloads are bounded; this one throttles at ROOT_LLM_RPM
    # against a variable batch count and can run far longer, so disabled
    # is the right choice rather than picking another number to outgrow.
    db.execute(text("SET lock_timeout = '30s'"))
    db.execute(text("SET idle_in_transaction_session_timeout = '0'"))
    try:
        if args.mode == "pilot":
            run_pilot(db, args)
        elif args.mode == "commit":
            run_commit(db, args)
        elif args.mode == "full":
            run_full(db, args)
        elif args.mode == "report":
            run_report(args.out)
        else:
            run_census(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()