# shellcheck shell=bash
# Shared helpers for the publish-build scripts (PARTITION C4). Sourced, never
# run. See README.md for the full publish sequence.
#
# SAFETY MODEL
#   * Master (container name_generator_db, host port 5433, database
#     name_generator, volume name-generator_name_generator_pgdata) is only
#     ever READ: a pg_dump, and psql sessions with
#     default_transaction_read_only=on. Every write-side script refuses
#     master as a target in refuse_target(), a pure argument check that runs
#     before any docker/psql/pg_* binary is invoked, and again at run time
#     in assert_not_master_container() from `docker inspect`.
#   * Every psql / pg_dump / pg_restore / vacuumdb runs INSIDE a Postgres 17
#     container via `docker exec -e PGOPTIONS=...` (never the host's
#     Homebrew tools: those are 18.x, C-1.1). PGOPTIONS carries
#     lock_timeout=30s for every session; pg_dump also gets
#     --lock-wait-timeout=30s.

MASTER_CONTAINER="name_generator_db"
MASTER_PORT="5433"
MASTER_DB="name_generator"
MASTER_VOLUME="name-generator_name_generator_pgdata"

# The alembic revision every published database must be at. Equal to the
# repo's migration head; tests/test_publish_scripts.py keeps them in sync.
# shellcheck disable=SC2034  # read by build_reference.sh, which sources this file
REQUIRED_ALEMBIC="a7c3e91f0b52"

# Session options.
PGOPTS_WRITE="-c lock_timeout=30s"
PGOPTS_READONLY="-c lock_timeout=30s -c default_transaction_read_only=on"

die() {
    echo "ERROR: $*" >&2
    exit 2
}

note() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

# --- argument checks (no binaries) ------------------------------------------

require_arg() {  # name value
    [[ -n "${2:-}" ]] || die "missing required argument $1"
}

valid_identifier() {  # name value -- a plain lower-case Postgres identifier
    [[ "$2" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
        || die "$1 must match ^[a-z_][a-z0-9_]*\$ (got '$2')"
}

# refuse_target CONTAINER [DATABASE] [PORT]
# Pure argument check: refuses master's container, database name and port.
refuse_target() {
    local container="${1:-}" db="${2:-}" port="${3:-}"
    [[ -n "$container" ]] || die "a target container is required"
    if [[ "$container" == "$MASTER_CONTAINER" ]]; then
        die "refusing: '$container' is the master container"
    fi
    if [[ -n "$db" && "$db" == "$MASTER_DB" ]]; then
        die "refusing: '$db' is the master database name"
    fi
    if [[ -n "$port" && "$port" == "$MASTER_PORT" ]]; then
        die "refusing: port $port is master's port"
    fi
}

refuse_existing_path() {  # path
    [[ ! -e "$1" ]] || die "refusing to overwrite existing $1"
}

# --- run-time checks ---------------------------------------------------------

# Refuse a container that mounts master's volume or publishes master's port,
# whatever it is called.
assert_not_master_container() {  # container
    local mounts ports
    mounts=$(docker inspect -f '{{range .Mounts}}{{.Name}} {{end}}' "$1") \
        || die "container '$1' not found"
    ports=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$1")
    if [[ " $mounts " == *" $MASTER_VOLUME "* ]]; then
        die "refusing: container '$1' mounts master's volume $MASTER_VOLUME"
    fi
    if [[ "$ports" == *"\"HostPort\":\"$MASTER_PORT\""* ]]; then
        die "refusing: container '$1' publishes master's port $MASTER_PORT"
    fi
}

# A parallel HNSW build allocates dynamic shared memory sized by
# maintenance_work_mem; Docker's default /dev/shm is only 64 MB. Fail early
# rather than hours into an index build. (A serial build, workers=0, uses
# local memory and is exempt.)
mem_to_bytes() {  # 512MB / 2GB / 65536kB -> bytes
    local n="${1//[!0-9]/}" unit="${1//[0-9]/}"
    case "$unit" in
        kB) echo $((n * 1024)) ;;
        MB) echo $((n * 1024 * 1024)) ;;
        GB) echo $((n * 1024 * 1024 * 1024)) ;;
        *) die "bad memory size '$1' (use kB, MB or GB)" ;;
    esac
}

assert_shm_fits() {  # container maintenance_work_mem parallel_workers
    local shm need
    [[ "$3" == 0 ]] && return 0
    shm=$(docker inspect -f '{{.HostConfig.ShmSize}}' "$1")
    need=$(mem_to_bytes "$2")
    if (( shm < need )); then
        die "container $1 has /dev/shm $shm bytes < maintenance_work_mem $2 ($need bytes): parallel HNSW would fail. Restart it with --shm-size >= $2, lower --maintenance-work-mem, or use --parallel-workers 0"
    fi
}

# --- in-container clients (always with PGOPTIONS) ----------------------------

# psql_ro CONTAINER DB [psql args...]  -- read-only session (master, checks)
psql_ro() {
    local ctr="$1" db="$2"
    shift 2
    docker exec -i -e PGOPTIONS="$PGOPTS_READONLY" "$ctr" \
        psql -X -v ON_ERROR_STOP=1 -U postgres -d "$db" "$@"
}

# psql_rw CONTAINER DB [psql args...]  -- write session (never master)
psql_rw() {
    local ctr="$1" db="$2"
    shift 2
    [[ "$ctr" != "$MASTER_CONTAINER" ]] || die "refusing: write session on master"
    docker exec -i -e PGOPTIONS="$PGOPTS_WRITE" "$ctr" \
        psql -X -v ON_ERROR_STOP=1 -U postgres -d "$db" "$@"
}

# manifest_value BUILD_DIR KEY -> the value of KEY in BUILD_DIR/manifest.txt;
# dies (clearly) if the key is missing, rather than yielding an empty value.
manifest_value() {
    local v
    v=$(grep "^$2=" "$1/manifest.txt" | head -1 | cut -d= -f2-) || true
    [[ -n "$v" ]] || die "manifest $1/manifest.txt lacks '$2'"
    echo "$v"
}

sha256_of() {  # file -> hex digest
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# Strip the entries a restore into a NEW database must skip: that database
# already has schema `public` (and its comment), so recreating them fails.
filter_toc() {  # in-list out-list
    grep -v -E ' (SCHEMA - public|COMMENT - SCHEMA public) ' "$1" > "$2"
}
