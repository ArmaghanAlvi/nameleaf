#!/usr/bin/env bash
# recall_check.sh -- filtered vector-search recall, ANN vs exact (PARTITION C6).
# READ-ONLY. Generalises C5's one-off c5_build/logs/fann.sh into a repeatable
# publish gate (C-5.10): a restored copy has the SAME data as master (counts
# and digests prove it) but a REBUILT HNSW graph, so filtered ANN results
# shift a little. This measures how well each index finds the exact
# neighbours, so a publish can be gated on "recall did not drop".
#
#   measure --container C --db D --probe FILE --out DIR
#       For each probe case (seed sense x target language) runs
#       recall_case.sql twice in read-only sessions: once with EXPLAIN (the
#       plan type per list), once for the results. Lists:
#         ann150  relaxed_order, LIMIT 150: the app's traversal fetch limit
#                 for a width-3 node (its operating point)
#         ann15   relaxed_order, LIMIT 15: continuity with C-5.10
#         ann1    strict_order, LIMIT 1: the root fallback rung
#                 (cross-language cases only; skipped, with a note, if
#                 sense_embeddings holds more than one embedding model)
#         exact   index scans off, ordered by (distance, sense_id)
#       A HIT is an ANN row whose distance <= the k-th exact distance
#       (exact float equality, no epsilon: both sides compute the same
#       `<=>` on the same two stored vectors), so a tie swapped at the
#       boundary is not a miss. Writes preflight.txt, cases.tsv, ids.tsv,
#       and summary.txt LAST (the completion marker).
#
#   compare --baseline DIR --candidate DIR --max-drop X
#       Pure file comparison (no docker). Per case and in total; plan-type
#       differences listed and recall shown with and without those cases;
#       exact top-150 DISTANCE lists compared (ids may differ only inside a
#       tie) -- a difference there is a DATA signal, not an index signal.
#       --max-drop is required: the threshold is decided in C7.
#
# Exit codes (both modes):
#   0  ok (compare: no list's overall recall dropped by more than X)
#   1  compare: overall recall dropped by more than X on ann150, ann15 or ann1
#   2  usage or precondition error (bad/missing argument, existing --out,
#      missing seed or language, not Postgres 17, exact plan used the index)
#   3  compare: not comparable -- a directory lacks summary.txt, the probe
#      cases differ, or an exact distance list differs (the data differs)
#   4  internal error (any unexpected failure; see the message)
#
# Safety: every psql runs inside the Postgres container via docker exec with
# PGOPTIONS lock_timeout=30s and default_transaction_read_only=on (lib.sh
# psql_ro); recall_case.sql also wraps each case in BEGIN ... ROLLBACK.
# Targets are explicit (no defaults); --out must not exist.
#
# Usage (from backend/scripts/publish/):
#   ./recall_check.sh measure --container name_generator_db --db name_generator \
#       --probe recall_probe.tsv --out c6_recall/master
#   ./recall_check.sh compare --baseline c6_recall/master \
#       --candidate c6_recall/restored --max-drop 0.03
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# Any unexpected failure is exit 4, so 1 always means "recall dropped".
# errtrace (-E) makes the trap fire inside functions and command
# substitutions too; die() exits 2 directly and never reaches it.
trap 'echo "INTERNAL ERROR (exit 4) at line $LINENO: $BASH_COMMAND" >&2; exit 4' ERR

TAB=$'\t'
LISTS="ann150 ann15 ann1"

# block FILE LIST -> the lines between "@@begin LIST" and "@@end".
block() {
    awk -v want="$2" '$0 == "@@begin " want {f = 1; next} $0 == "@@end" {f = 0} f' "$1"
}

usage() { die "usage: recall_check.sh measure --container C --db D --probe FILE --out DIR | compare --baseline DIR --candidate DIR --max-drop X"; }

# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------
measure() {
    local ctr="" db="" probe="" out=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --container) ctr="${2:-}"; shift 2 ;;
            --db) db="${2:-}"; shift 2 ;;
            --probe) probe="${2:-}"; shift 2 ;;
            --out) out="${2:-}"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_arg --container "$ctr"
    require_arg --db "$db"
    require_arg --probe "$probe"
    require_arg --out "$out"
    [[ "$ctr" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "bad --container"
    valid_identifier --db "$db"
    [[ -f "$probe" ]] || die "probe file not found: $probe"
    refuse_existing_path "$out"

    mkdir "$out"
    local start=$SECONDS
    note "recall_check measure: container=$ctr db=$db probe=$probe out=$out"

    # --- probe cases --------------------------------------------------------
    local cases="$out/.cases"
    awk -F "$TAB" 'NR > 1 && $1 != "" {print $1 "\t" $2}' "$probe" > "$cases"
    local ncases seeds langs
    ncases=$(wc -l < "$cases" | tr -d ' ')
    (( ncases > 0 )) || die "probe file has no cases"
    seeds=$(cut -f1 "$cases" | sort -un | paste -sd, -)
    langs=$(cut -f2 "$cases" | sort -u | sed "s/.*/'&'/" | paste -sd, -)
    [[ "$seeds" =~ ^[0-9,]+$ ]] || die "probe sense ids must be integers"
    [[ "$langs" =~ ^[a-z,\']+$ ]] || die "probe language codes must be lower-case letters"

    # --- preflight (read-only) ----------------------------------------------
    local version pgvector hnsw nmodels missing_seeds missing_langs
    version=$(psql_ro "$ctr" "$db" -At -c "SHOW server_version_num")
    [[ "$version" == 17* ]] || die "not Postgres 17 ($version)"
    pgvector=$(psql_ro "$ctr" "$db" -At -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    hnsw=$(psql_ro "$ctr" "$db" -At -c \
        "SELECT string_agg(c.relname, ',' ORDER BY c.relname)
           FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
           JOIN pg_am a ON a.oid = c.relam
          WHERE i.indrelid = 'public.sense_embeddings'::regclass AND a.amname = 'hnsw'")
    [[ -n "$hnsw" ]] || die "no HNSW index on sense_embeddings"
    [[ "$hnsw" != *,* ]] || die "more than one HNSW index on sense_embeddings ($hnsw)"
    nmodels=$(psql_ro "$ctr" "$db" -At -c "SELECT count(DISTINCT embedding_model) FROM sense_embeddings")
    missing_seeds=$(psql_ro "$ctr" "$db" -At -c \
        "SELECT string_agg(i::text, ',') FROM unnest(ARRAY[$seeds]) AS i
          WHERE NOT EXISTS (SELECT 1 FROM sense_embeddings se WHERE se.sense_id = i)")
    [[ -z "$missing_seeds" ]] || die "probe seed(s) missing from $db (no embedding): $missing_seeds"
    missing_langs=$(psql_ro "$ctr" "$db" -At -c \
        "SELECT string_agg(c, ',') FROM unnest(ARRAY[$langs]) AS c
          WHERE NOT EXISTS (SELECT 1 FROM languages g WHERE g.code = c)")
    [[ -z "$missing_langs" ]] || die "probe language(s) missing from $db: $missing_langs"
    local strict_note="ann1 measured on cross-language cases"
    if [[ "$nmodels" != 1 ]]; then
        strict_note="ann1 SKIPPED: sense_embeddings holds $nmodels embedding models, so exact[0] is not the fallback rung's exact top-1"
    fi
    {
        echo "container=$ctr"
        echo "db=$db"
        echo "server_version_num=$version"
        echo "pgvector=$pgvector"
        echo "hnsw_indexes=$hnsw"
        echo "embedding_models=$nmodels"
        echo "probe=$probe"
        echo "probe_sha256=$(sha256_of "$probe")"
        echo "cases=$ncases"
        echo "strict=$strict_note"
        echo "measured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$out/preflight.txt"
    cat "$out/preflight.txt"

    # --- cases ----------------------------------------------------------------
    printf 'sid\tlang\tlist\tk\texact_n\tann_n\thits\tplan\tms\texact_ms\n' > "$out/cases.tsv"
    printf 'sid\tlang\tlist\trank\tsense_id\tdistance\n' > "$out/ids.tsv"
    local plans="$out/.plans" rows="$out/.rows" n=0 sid lang strict list k
    while IFS="$TAB" read -r sid lang; do
        n=$((n + 1))
        strict=false
        if [[ "$lang" != en && "$nmodels" == 1 ]]; then strict=true; fi
        psql_ro "$ctr" "$db" -At -F "$TAB" -v sid="$sid" -v lang="$lang" \
            -v do_strict="$strict" -v "explain=EXPLAIN (COSTS OFF)" \
            < "$SCRIPT_DIR/recall_case.sql" > "$plans"
        psql_ro "$ctr" "$db" -At -F "$TAB" -v sid="$sid" -v lang="$lang" \
            -v do_strict="$strict" -v explain= \
            < "$SCRIPT_DIR/recall_case.sql" > "$rows"

        # Plans are captured as text first, never piped into grep -q (C-5.3).
        local plan_text
        plan_text=$(block "$plans" exact)
        if [[ "$plan_text" == *"$hnsw"* ]]; then
            die "case $sid/$lang: the exact query's plan used the HNSW index"
        fi
        block "$rows" exact > "$out/.exact"
        local exact_ms
        exact_ms=$(awk '$1 == "@@ms" && $2 == "exact" {print $3}' "$rows")
        awk -F "$TAB" -v s="$sid" -v l="$lang" \
            '{printf "%s\t%s\texact\t%d\t%s\t%s\n", s, l, NR, $1, $2}' \
            "$out/.exact" >> "$out/ids.tsv"

        for list in $LISTS; do
            case "$list" in
                ann150) k=150 ;;
                ann15) k=15 ;;
                ann1) k=1; [[ "$strict" == true ]] || continue ;;
            esac
            local plan ms
            plan_text=$(block "$plans" "$list")
            if [[ "$plan_text" == *"$hnsw"* ]]; then plan=index; else plan=noindex; fi
            ms=$(awk -v want="$list" '$1 == "@@ms" && $2 == want {print $3}' "$rows")
            block "$rows" "$list" > "$out/.ann"
            awk -F "$TAB" -v s="$sid" -v l="$lang" -v li="$list" \
                '{printf "%s\t%s\t%s\t%d\t%s\t%s\n", s, l, li, NR, $1, $2}' \
                "$out/.ann" >> "$out/ids.tsv"
            # hits: ANN rows at a distance <= the k-th exact distance.
            # (FILENAME, not FNR == NR, so an empty exact list stays empty.)
            awk -F "$TAB" -v k="$k" -v s="$sid" -v l="$lang" -v li="$list" \
                -v plan="$plan" -v ms="$ms" -v exms="$exact_ms" '
                FILENAME == ARGV[1] { ex[++nex] = $2 + 0; next }
                { ann[++nann] = $2 + 0 }
                END {
                    kn = (nex < k) ? nex : k
                    kth = (kn > 0) ? ex[kn] : -1
                    hits = 0
                    for (i = 1; i <= nann; i++) if (kn > 0 && ann[i] <= kth) hits++
                    printf "%s\t%s\t%s\t%d\t%d\t%d\t%d\t%s\t%s\t%s\n", s, l, li, k, kn, nann + 0, hits, plan, ms, exms
                }' "$out/.exact" "$out/.ann" >> "$out/cases.tsv"
        done
        if (( n % 10 == 0 || n == ncases )); then note "case $n/$ncases done"; fi
    done < "$cases"
    rm -f "$cases" "$plans" "$rows" "$out/.exact" "$out/.ann"

    # --- summary (written last: the completion marker) --------------------------
    local wall=$((SECONDS - start))
    {
        echo "# recall_check summary ($db on $ctr), wall ${wall}s"
        cat "$out/preflight.txt"
        echo
        echo "## overall (hits / exact, recall), per list"
        awk -F "$TAB" 'NR > 1 {h[$3] += $7; e[$3] += $5; c[$3]++}
            END {for (l in h) printf "%s\t%d\t%d\t%.4f\tcases=%d\n", l, h[l], e[l], (e[l] ? h[l] / e[l] : 0), c[l]}' \
            "$out/cases.tsv" | sort
        echo
        echo "## per language (list lang hits exact recall noindex_cases)"
        awk -F "$TAB" 'NR > 1 {k = $3 "\t" $2; h[k] += $7; e[k] += $5; if ($8 == "noindex") ni[k]++}
            END {for (k in h) printf "%s\t%d\t%d\t%.4f\t%d\n", k, h[k], e[k], (e[k] ? h[k] / e[k] : 0), ni[k] + 0}' \
            "$out/cases.tsv" | sort
        echo
        echo "## non-index plan cases per language (all lists)"
        awk -F "$TAB" 'NR > 1 && $8 == "noindex" {ni[$2]++} END {for (l in ni) printf "%s\t%d\n", l, ni[l]}' \
            "$out/cases.tsv" | sort
        echo
        echo "wall_seconds=$wall"
        echo "COMPLETE"
    } > "$out/summary.tmp"
    mv "$out/summary.tmp" "$out/summary.txt"
    cat "$out/summary.txt"
}

# ---------------------------------------------------------------------------
# compare (files only)
# ---------------------------------------------------------------------------
compare() {
    local base="" cand="" maxdrop=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --baseline) base="${2:-}"; shift 2 ;;
            --candidate) cand="${2:-}"; shift 2 ;;
            --max-drop) maxdrop="${2:-}"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    require_arg --baseline "$base"
    require_arg --candidate "$cand"
    require_arg --max-drop "$maxdrop"
    [[ "$maxdrop" =~ ^[0-9]*\.?[0-9]+$ ]] || die "--max-drop must be a non-negative number"
    local d
    for d in "$base" "$cand"; do
        [[ -d "$d" ]] || die "not a directory: $d"
        if [[ ! -f "$d/summary.txt" ]]; then
            echo "NOT COMPARABLE: $d has no summary.txt (measure did not complete)"
            exit 3
        fi
    done

    local notcomparable=0
    # Probe cases must match.
    if ! diff <(awk -F "$TAB" 'NR > 1 {print $1 "\t" $2 "\t" $3}' "$base/cases.tsv" | sort) \
              <(awk -F "$TAB" 'NR > 1 {print $1 "\t" $2 "\t" $3}' "$cand/cases.tsv" | sort) \
              > /dev/null; then
        echo "NOT COMPARABLE: the probe cases differ between the two directories"
        exit 3
    fi

    echo "# recall_check compare: baseline=$base candidate=$cand max_drop=$maxdrop"
    echo
    echo "## exact top-150 distance lists (a difference = the DATA differs)"
    local exactdiff
    exactdiff=$(awk -F "$TAB" '
        FNR == 1 {f++; next}
        $3 == "exact" { key = $1 "\t" $2; d[f, key] = d[f, key] "," $6; ids[f, key] = ids[f, key] "," $5; keys[key] = 1 }
        END {
            nd = 0; ni = 0
            for (k in keys) {
                if (d[1, k] != d[2, k]) { printf "DIFFERS\t%s\n", k; nd++ }
                else if (ids[1, k] != ids[2, k]) { printf "ids differ inside ties only\t%s\n", k; ni++ }
            }
            printf "exact_distance_lists_differing=%d\nexact_id_lists_differing_only_in_ties=%d\n", nd, ni
        }' "$base/ids.tsv" "$cand/ids.tsv")
    echo "$exactdiff" | sort
    if [[ "$exactdiff" != *"exact_distance_lists_differing=0"* ]]; then
        notcomparable=1
    fi

    echo
    echo "## plan type differences (index vs noindex)"
    awk -F "$TAB" '
        FNR == 1 {f++; next}
        { k = $1 "\t" $2 "\t" $3; p[f, k] = $8; keys[k] = 1 }
        END { n = 0; for (k in keys) if (p[1, k] != p[2, k]) { printf "%s\t%s -> %s\n", k, p[1, k], p[2, k]; n++ }
              printf "plan_type_differences=%d\n", n }' "$base/cases.tsv" "$cand/cases.tsv" | sort

    echo
    echo "## overall recall per list: baseline candidate drop (all cases | excluding plan-type differences)"
    local overall
    overall=$(awk -F "$TAB" -v X="$maxdrop" '
        FNR == 1 {f++; next}
        { k = $1 "\t" $2 "\t" $3; h[f, k] = $7; e[f, k] = $5; p[f, k] = $8; li[k] = $3; keys[k] = 1 }
        END {
            for (k in keys) {
                l = li[k]; lists[l] = 1
                bh[l] += h[1, k]; be[l] += e[1, k]; ch[l] += h[2, k]; ce[l] += e[2, k]
                if (p[1, k] == p[2, k]) { bhs[l] += h[1, k]; bes[l] += e[1, k]; chs[l] += h[2, k]; ces[l] += e[2, k] }
            }
            fail = 0
            for (l in lists) {
                b = be[l] ? bh[l] / be[l] : 0; c = ce[l] ? ch[l] / ce[l] : 0
                bs = bes[l] ? bhs[l] / bes[l] : 0; cs = ces[l] ? chs[l] / ces[l] : 0
                drop = b - c
                verdict = (drop > X + 1e-12) ? "DROP>X" : "ok"
                if (verdict != "ok") fail = 1
                printf "%s\t%.4f\t%.4f\t%+.4f\t%s\t|\t%.4f\t%.4f\t%+.4f\n", l, b, c, drop, verdict, bs, cs, bs - cs
            }
            printf "gate_fail=%d\n", fail
        }' "$base/cases.tsv" "$cand/cases.tsv")
    echo "$overall" | awk '!/^gate_fail=/' | sort
    echo
    echo "## per language, per list: baseline_hits candidate_hits exact delta"
    awk -F "$TAB" '
        FNR == 1 {f++; next}
        { k = $3 "\t" $2; h[f, k] += $7; e[f, k] += $5; keys[k] = 1 }
        END { for (k in keys) if (h[1, k] != h[2, k]) printf "%s\t%d\t%d\t%d\t%+d\n", k, h[1, k], h[2, k], e[1, k], h[2, k] - h[1, k] }' \
        "$base/cases.tsv" "$cand/cases.tsv" | sort
    echo
    echo "## per case (only cases whose hits changed): sid lang list baseline candidate"
    awk -F "$TAB" '
        FNR == 1 {f++; next}
        { k = $1 "\t" $2 "\t" $3; h[f, k] = $7; keys[k] = 1 }
        END { n = 0; for (k in keys) if (h[1, k] != h[2, k]) { printf "%s\t%d\t%d\n", k, h[1, k], h[2, k]; n++ }
              printf "cases_changed=%d\n", n }' "$base/cases.tsv" "$cand/cases.tsv" | sort

    echo
    if (( notcomparable )); then
        echo "RESULT: NOT COMPARABLE (exit 3): exact distance lists differ, so the data differs"
        exit 3
    fi
    if [[ "$overall" == *"gate_fail=1"* ]]; then
        echo "RESULT: FAIL (exit 1): overall recall dropped by more than $maxdrop on at least one list"
        exit 1
    fi
    echo "RESULT: PASS (exit 0): no list's overall recall dropped by more than $maxdrop"
}

case "${1:-}" in
    measure) shift; measure "$@" ;;
    compare) shift; compare "$@" ;;
    *) usage ;;
esac
