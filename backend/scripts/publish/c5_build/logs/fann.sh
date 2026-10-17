#!/usr/bin/env bash
set -uo pipefail
source ~/Personal-Projects/nameforge_c5/c5.env
for sid in 23466 7459 72902 57055; do
  for lang in ru el ga is; do
    Q="SET hnsw.iterative_scan = relaxed_order; SET hnsw.max_scan_tuples = 20000; SET hnsw.ef_search = 100;
       SELECT se.sense_id FROM sense_embeddings se JOIN senses s ON s.id = se.sense_id
         JOIN lexemes l ON l.id = s.lexeme_id JOIN languages g ON g.id = l.language_id
        WHERE g.code = '$lang'
        ORDER BY se.embedding <=> (SELECT embedding FROM sense_embeddings WHERE sense_id = $sid) LIMIT 15;"
    M="$C5/fann_master_${sid}_$lang.txt"; R="$C5/fann_restored_${sid}_$lang.txt"; X="$C5/fexact_${sid}_$lang.txt"
    docker exec name_generator_db psql -qAtX -U postgres -d name_generator -c "$Q" < /dev/null | sort -n > "$M"
    docker exec nameforge-db-1 psql -qAtX -U postgres -d "name_generator_$STAMP" -c "$Q" < /dev/null | sort -n > "$R"
    cmp -s "$M" "$R" && r=SAME || r=DIFF
    echo "$sid $lang $r rows=$(wc -l < "$M" | tr -d ' ') exact=$(wc -l < "$X" | tr -d ' ') master_hits=$(grep -cFxf "$X" "$M") restored_hits=$(grep -cFxf "$X" "$R")"
  done
done
