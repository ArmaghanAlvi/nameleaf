#!/usr/bin/env bash
# setup.sh -- one-time hardening and setup of the Oracle A1 server
# (PARTITION E1; roadmap 6c; E-0.6 predictions, E-0.7 decisions 6g-6k).
# Run by the user on the server in E2, as root, from the admin alias:
#
#   scp deploy/server/setup.sh nameleaf-prod-admin:
#   ssh -t nameleaf-prod-admin sudo bash setup.sh preflight
#   ssh -t nameleaf-prod-admin sudo bash setup.sh apply --format-data-device /dev/oracleoci/oraclevdb
#
# (Copy, then run: piping it into `bash -s` would let apt-get read the rest
# of the script from stdin.) Self-contained: the repo isn't on the server yet.
#
#   preflight [--data-device DEV] [--data-size-gb N]
#       READ-ONLY. Compares the server with E-0.6's predictions and prints
#       each item as OK or MISMATCH (found vs expected); any MISMATCH stops
#       with exit 2 and nothing changed. Never guesses.
#         * aarch64; Ubuntu 24.04;
#         * the data volume at DEV (default /dev/oracleoci/oraclevdb): size
#           within 5% of N GB or GiB (default 120), attachment (iSCSI vs
#           paravirtualized), filesystem signatures (blkid -p, wipefs -n);
#           a signed disk passes only if fstab already mounts it at /mnt/data;
#         * no swap; ufw absent or inactive;
#         * iptables: INPUT accepts tcp/22 and ends in REJECT
#           icmp-host-prohibited; FORWARD has that REJECT; whether Docker's
#           chains exist (a re-run).
#   apply [--data-device DEV] [--data-size-gb N] [--format-data-device DEV]
#       Preflight, then (each step checks before acting, so re-runs are safe):
#        1. data volume: mkfs.ext4 ONLY when --format-data-device names the
#           preflight device AND blkid/wipefs show no signature; fstab by UUID
#           (nofail, plus _netdev if iSCSI); mount /mnt/data. Before Docker.
#        2. packages: fail2ban (sshd jail, verified), unattended-upgrades
#           (enabled, Automatic-Reboot "false"), rclone (installed only),
#           iptables-persistent/netfilter-persistent if missing (autosave
#           preseeded off).
#        3. firewall, BEFORE Docker: INPUT accepts tcp/80, tcp/443, udp/443,
#           inserted above Oracle's final REJECT, then `netfilter-persistent
#           save`. Never edits or saves the ruleset once Docker's chains exist:
#           a save would capture them (deploy/README.md, Findings E-1).
#        4. Docker Engine + compose plugin from Docker's apt repository.
#           daemon.json data-root /mnt/data/docker and containerd root
#           /mnt/data/containerd are written BEFORE the first start: on
#           Engine 29 fresh installs images live in containerd's store, which
#           data-root does not move (Findings E-1). Also before the first
#           start: systemd drop-ins with RequiresMountsFor=/mnt/data for
#           docker.service and containerd.service, so a boot where the
#           volume fails to mount (fstab has nofail) leaves Docker stopped
#           instead of starting it on the boot disk with an empty data root.
#           Prints where images and volumes actually live.
#        5. user nameleaf (docker group, no sudo) with ubuntu's
#           authorized_keys; sshd drop-in: key-only, no root login,
#           validated with `sshd -t` before reload.
#        6. /srv/nameleaf/{releases,publish,state,env (700)},
#           /mnt/data/backups/live, owned by nameleaf; the nameleaf-compose
#           link; the nightly backup cron entry.
#        7. prints the console steps and from-outside checks it can't do.
#       Writes no secrets and no env file. No swap is created.
#
# Exit codes: 0 ok; 2 usage, refusal, mismatch or failed verification; other
# non-zero: a failed command (set -e). Log: /var/log/nameleaf-setup.log.
#
# NAMELEAF_SETUP_ROOT prefixes every file path (tests only; unset on the
# server). Commands are found on PATH, so the tests can stub them.
set -Eeuo pipefail

ROOT="${NAMELEAF_SETUP_ROOT:-}"
RETRY_SLEEP="${NAMELEAF_SETUP_RETRY_SLEEP:-2}"
DEV="/dev/oracleoci/oraclevdb"
SIZE_GB="120"
FORMAT_DEV=""
DATA_MNT="/mnt/data"
SRV="/srv/nameleaf"
USER_NAME="nameleaf"
DOCKER_ROOT="$DATA_MNT/docker"
CONTAINERD_ROOT="$DATA_MNT/containerd"
BACKUP_DIR="$DATA_MNT/backups"
REJECT_RULE="-j REJECT --reject-with icmp-host-prohibited"
BASE_PACKAGES="fail2ban unattended-upgrades rclone iptables-persistent netfilter-persistent"
DOCKER_PACKAGES="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

die() {
    echo "ERROR: $*" >&2
    exit 2
}

note() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

P() { echo "$ROOT$1"; }

usage() { die "usage: setup.sh preflight [--data-device DEV] [--data-size-gb N] | apply [--data-device DEV] [--data-size-gb N] [--format-data-device DEV]"; }

# write_file PATH MODE CONTENT -> writes only when different; sets CHANGED=1.
CHANGED=0
write_file() {
    local path="$1" mode="$2" content="$3"
    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
        note "unchanged: ${path#"$ROOT"}"
        CHANGED=0
        return 0
    fi
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path.nameleaf-tmp"
    chmod "$mode" "$path.nameleaf-tmp"
    mv -f "$path.nameleaf-tmp" "$path"
    note "written: ${path#"$ROOT"}"
    CHANGED=1
}

# --- preflight -------------------------------------------------------------------

MISMATCHES=0
ok() { echo "OK        $1: $2"; }
mismatch() {  # label found expected
    echo "MISMATCH  $1: found '$2', expected '$3'"
    MISMATCHES=$((MISMATCHES + 1))
}

os_field() {  # KEY -> value from os-release
    sed -n "s/^$1=//p" "$(P /etc/os-release)" | tr -d '"' | head -1
}

docker_chains_present() {
    { iptables -S 2>/dev/null; iptables -t nat -S 2>/dev/null; } | grep -qE '^-N DOCKER'
}

# Data-device facts, set by inspect_device.
DEV_SIZE="" DEV_TRAN="" DEV_SIG="" DEV_UUID="" DEV_STATE=""
inspect_device() {
    local line wipe
    line=$(lsblk -bdno SIZE,TRAN "$DEV" 2>/dev/null) || { DEV_STATE="absent"; return 0; }
    DEV_SIZE=$(echo "$line" | awk '{print $1}')
    [[ "$DEV_SIZE" =~ ^[0-9]+$ ]] || DEV_SIZE=0
    DEV_TRAN=$(echo "$line" | awk '{print $2}')
    DEV_SIG=$(blkid -p -o value -s TYPE "$DEV" 2>/dev/null || true)
    wipe=$(wipefs -n "$DEV" 2>/dev/null || true)
    if [[ -z "$DEV_SIG" && -n "$wipe" ]]; then
        DEV_SIG="(wipefs: $(echo "$wipe" | tail -n +2 | awk '{print $NF}' | tr '\n' ' '))"
    fi
    if [[ -z "$DEV_SIG" ]]; then
        DEV_STATE="unformatted"
        return 0
    fi
    DEV_UUID=$(blkid -s UUID -o value "$DEV" 2>/dev/null || true)
    if [[ -n "$DEV_UUID" ]] && grep -qE "^UUID=${DEV_UUID}[[:space:]]+${DATA_MNT}[[:space:]]" "$(P /etc/fstab)" 2>/dev/null; then
        DEV_STATE="ours"
    else
        DEV_STATE="foreign"
    fi
}

attachment() {
    if [[ "$DEV_TRAN" == "iscsi" ]]; then echo iscsi; else echo "paravirtualized"; fi
}

preflight() {
    local arch os ver lo hi in fwd last
    note "preflight (read-only) -- expected values are E-0.6's predictions"

    arch=$(uname -m)
    if [[ "$arch" == "aarch64" ]]; then ok "architecture" "$arch"; else mismatch "architecture" "$arch" "aarch64"; fi
    os=$(os_field ID); ver=$(os_field VERSION_ID)
    if [[ "$os $ver" == "ubuntu 24.04" ]]; then ok "OS" "$os $ver"; else mismatch "OS" "$os $ver" "ubuntu 24.04"; fi

    inspect_device
    if [[ "$DEV_STATE" == "absent" ]]; then
        mismatch "data device" "nothing at $DEV" "a ${SIZE_GB} GB block volume (if it is attached by iSCSI, run the console's iSCSI attach commands first, or pass --data-device)"
    else
        # Accept the size as GB or GiB, within 5%.
        lo=$((SIZE_GB * 1000000000 * 95 / 100)); hi=$((SIZE_GB * 1073741824 * 105 / 100))
        if (( DEV_SIZE >= lo && DEV_SIZE <= hi )); then
            ok "data device size" "$DEV_SIZE bytes at $DEV"
        else
            mismatch "data device size" "$DEV_SIZE bytes at $DEV" "${SIZE_GB} GB (+/-5%)"
        fi
        ok "data device attachment" "$(attachment) (lsblk TRAN='${DEV_TRAN}')"
        case "$DEV_STATE" in
            unformatted) ok "data device signatures" "none (format needs --format-data-device $DEV)" ;;
            ours) ok "data device signatures" "$DEV_SIG, already in fstab at $DATA_MNT" ;;
            foreign) mismatch "data device signatures" "$DEV_SIG, not set up at $DATA_MNT" "none, or our own ext4 at $DATA_MNT (never touched otherwise)" ;;
        esac
    fi

    if [[ -z "$(swapon --show --noheadings 2>/dev/null)" ]]; then
        ok "swap" "none"
    else
        mismatch "swap" "$(swapon --show --noheadings | tr '\n' ' ')" "none"
    fi
    if command -v ufw > /dev/null 2>&1 && ! ufw status 2>/dev/null | grep -q inactive; then
        mismatch "ufw" "$(ufw status 2>/dev/null | head -1)" "absent or inactive (E-0.7 6g: no ufw)"
    else
        ok "ufw" "absent or inactive"
    fi

    in=$(iptables -S INPUT 2>/dev/null) || die "iptables -S failed (run as root)"
    fwd=$(iptables -S FORWARD 2>/dev/null)
    if echo "$in" | grep -qE -- '--dport 22( |$).*-j ACCEPT'; then ok "iptables INPUT tcp/22" "ACCEPT"; else mismatch "iptables INPUT tcp/22" "no ACCEPT" "ACCEPT"; fi
    last=$(echo "$in" | grep '^-A INPUT' | tail -1)
    if [[ "$last" == *"$REJECT_RULE"* ]]; then ok "iptables INPUT final rule" "$last"; else mismatch "iptables INPUT final rule" "$last" "-A INPUT $REJECT_RULE"; fi
    if echo "$fwd" | grep -q -- "^-A FORWARD $REJECT_RULE"; then ok "iptables FORWARD" "REJECT icmp-host-prohibited"; else mismatch "iptables FORWARD" "$(echo "$fwd" | grep '^-A' | tr '\n' ';')" "-A FORWARD $REJECT_RULE"; fi
    if docker_chains_present; then ok "Docker iptables chains" "present (Docker installed: a re-run)"; else ok "Docker iptables chains" "none yet"; fi
    if command -v docker > /dev/null 2>&1; then
        ok "Docker" "installed"
        if [[ -z "$(findmnt -n -o SOURCE "$DATA_MNT" 2>/dev/null)" ]]; then
            mismatch "$DATA_MNT" "not mounted while Docker is installed" "mounted before Docker's first start"
        fi
    else
        ok "Docker" "not installed yet"
    fi

    if (( MISMATCHES > 0 )); then
        echo "PREFLIGHT FAILED: $MISMATCHES mismatch(es). Nothing was changed. Reconcile E-0.6 before re-running."
        exit 2
    fi
    note "preflight passed"
}

# --- apply steps -------------------------------------------------------------------

step_data() {
    note "== 1. data volume"
    if [[ -n "$FORMAT_DEV" && "$FORMAT_DEV" != "$DEV" ]]; then
        die "--format-data-device '$FORMAT_DEV' is not the data device '$DEV' (pass --data-device too if that is intended)"
    fi
    case "$DEV_STATE" in
        ours)
            [[ -z "$FORMAT_DEV" ]] || note "$DEV is already our ext4 at $DATA_MNT: --format-data-device ignored (never reformats)" ;;
        unformatted)
            [[ "$FORMAT_DEV" == "$DEV" ]] \
                || die "$DEV has no filesystem. Formatting erases it: re-run with --format-data-device $DEV"
            # Re-check right before formatting.
            if [[ -n "$(blkid -p -o value -s TYPE "$DEV" 2>/dev/null || true)" || -n "$(wipefs -n "$DEV" 2>/dev/null || true)" ]]; then
                die "refusing to format $DEV: a signature appeared since preflight"
            fi
            note "formatting $DEV as ext4 (label nameleaf-data)"
            mkfs.ext4 -L nameleaf-data "$DEV"
            DEV_UUID=$(blkid -s UUID -o value "$DEV")
            [[ -n "$DEV_UUID" ]] || die "no UUID on $DEV after mkfs"
            local opts="defaults,nofail"
            [[ "$(attachment)" != "iscsi" ]] || opts="$opts,_netdev"
            printf 'UUID=%s %s ext4 %s 0 2\n' "$DEV_UUID" "$DATA_MNT" "$opts" >> "$(P /etc/fstab)"
            note "fstab: UUID=$DEV_UUID $DATA_MNT ext4 $opts" ;;
        *) die "data device state '$DEV_STATE': not touching it" ;;
    esac
    mkdir -p "$(P "$DATA_MNT")"
    if [[ -z "$(findmnt -n -o SOURCE "$DATA_MNT" 2>/dev/null)" ]]; then
        mount "$DATA_MNT"
    fi
    [[ -n "$(findmnt -n -o SOURCE "$DATA_MNT" 2>/dev/null)" ]] || die "$DATA_MNT is not mounted"
    note "$DATA_MNT mounted: $(findmnt -n -o SOURCE,FSTYPE,OPTIONS "$DATA_MNT")"
}

step_packages() {
    note "== 2. packages"
    local missing="" p tries
    for p in $BASE_PACKAGES; do
        dpkg -s "$p" > /dev/null 2>&1 || missing="$missing $p"
    done
    if [[ -n "$missing" ]]; then
        case "$missing" in
            *iptables-persistent*)
                printf '%s\n' "iptables-persistent iptables-persistent/autosave_v4 boolean false" \
                    "iptables-persistent iptables-persistent/autosave_v6 boolean false" | debconf-set-selections ;;
        esac
        note "installing:$missing"
        apt-get update < /dev/null
        # shellcheck disable=SC2086  # a list of package names
        DEBIAN_FRONTEND=noninteractive apt-get install -y $missing < /dev/null
    else
        note "already installed: $BASE_PACKAGES"
    fi

    write_file "$(P /etc/fail2ban/jail.d/nameleaf.local)" 644 "# Written by deploy/server/setup.sh (E1).
[sshd]
enabled = true"
    systemctl enable --now fail2ban
    (( ! CHANGED )) || systemctl restart fail2ban
    tries=0
    until fail2ban-client status sshd > /dev/null 2>&1; do
        tries=$((tries + 1))
        (( tries < 10 )) || die "fail2ban's sshd jail is not running: check 'journalctl -u fail2ban' (Ubuntu 24.04 may need 'backend = systemd' in the jail; E2-verify)"
        sleep "$RETRY_SLEEP"
    done
    note "fail2ban sshd jail: running"

    local auto
    auto="$(P /etc/apt/apt.conf.d/20auto-upgrades)"
    if ! grep -qs 'APT::Periodic::Unattended-Upgrade "1";' "$auto"; then
        write_file "$auto" 644 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";'
    else
        note "unattended-upgrades already enabled ($auto)"
    fi
    write_file "$(P /etc/apt/apt.conf.d/52nameleaf-unattended-upgrades)" 644 '// Written by deploy/server/setup.sh (E1): security updates, never an automatic reboot.
Unattended-Upgrade::Automatic-Reboot "false";'
    systemctl enable --now unattended-upgrades
}

input_rule() {  # proto port -> the rule spec, Oracle's style
    echo "-p $1 -m state --state NEW -m $1 --dport $2 -j ACCEPT"
}

step_firewall() {
    note "== 3. firewall (iptables INPUT; before Docker)"
    local spec port rule n changed=0 saved
    if docker_chains_present; then
        note "Docker's iptables chains exist: not editing or saving the ruleset (a save would capture them)"
        for spec in "tcp 80" "tcp 443" "udp 443"; do
            # shellcheck disable=SC2086  # spec and rule are plain words
            rule=$(input_rule $spec)
            # shellcheck disable=SC2086
            iptables -C INPUT $rule 2>/dev/null \
                || die "INPUT lacks '$rule' and Docker is installed: add it by hand above the REJECT, then stop Docker before any 'netfilter-persistent save'"
        done
        note "INPUT accepts tcp/80, tcp/443, udp/443"
        return 0
    fi
    for spec in "tcp 80" "tcp 443" "udp 443"; do
        # shellcheck disable=SC2086
        rule=$(input_rule $spec)
        # shellcheck disable=SC2086
        if iptables -C INPUT $rule 2>/dev/null; then
            note "present: $rule"
            continue
        fi
        n=$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" {print $1; exit}')
        [[ -n "$n" ]] || die "no REJECT rule in INPUT to insert above"
        # shellcheck disable=SC2086
        iptables -I INPUT "$n" $rule
        note "inserted at INPUT line $n: $rule"
        changed=1
    done
    saved="$(P /etc/iptables/rules.v4)"
    if (( changed )) || ! grep -qs -- '--dport 443' "$saved"; then
        ! docker_chains_present || die "Docker's chains appeared: not saving"
        netfilter-persistent save
        note "saved with netfilter-persistent"
    fi
    for port in "tcp --dport 80" "tcp --dport 443" "udp --dport 443"; do
        grep -qs -- "-m $port -j ACCEPT" "$saved" || die "$saved lacks '$port' after saving"
    done
    note "persisted: tcp/80, tcp/443, udp/443 in ${saved#"$ROOT"}"
}

# Docker and containerd must never start without the data volume: fstab
# mounts it with nofail (so a failed attach can't block boot and SSH), and
# without this a failed mount would give them an empty data root on the boot
# disk, where Postgres would initialise an empty cluster. RequiresMountsFor
# adds Requires= and After= on the mount unit (systemd.unit(5)). Written
# before Docker's first start, and on every run.
MOUNT_UNITS="docker.service containerd.service"
mount_dropins() {
    local unit changed=0
    for unit in $MOUNT_UNITS; do
        write_file "$(P "/etc/systemd/system/$unit.d/10-nameleaf-data.conf")" 644 "# Written by deploy/server/setup.sh (E2): start only with the data volume mounted.
[Unit]
RequiresMountsFor=$DATA_MNT"
        (( ! CHANGED )) || changed=1
    done
    (( ! changed )) || systemctl daemon-reload
}

verify_mount_dropins() {
    local unit got
    for unit in $MOUNT_UNITS; do
        got=$(systemctl show -p RequiresMountsFor --value "$unit" 2>/dev/null || true)
        [[ " $got " == *" $DATA_MNT "* ]] \
            || die "$unit does not require $DATA_MNT (RequiresMountsFor='$got'): check /etc/systemd/system/$unit.d/"
        echo "  $unit: RequiresMountsFor=$got"
    done
}

step_docker() {
    note "== 4. Docker Engine + compose plugin (Docker's apt repository)"
    mount_dropins
    if command -v docker > /dev/null 2>&1; then
        note "Docker is installed: not reinstalling"
    else
        [[ -n "$(findmnt -n -o SOURCE "$DATA_MNT" 2>/dev/null)" ]] || die "$DATA_MNT must be mounted before Docker's first start"
        local daemon ctd codename arch
        daemon="$(P /etc/docker/daemon.json)"
        ctd="$(P /etc/containerd/config.toml)"
        local want_daemon="{
  \"data-root\": \"$DOCKER_ROOT\"
}"
        if [[ -f "$daemon" && "$(cat "$daemon")" != "$want_daemon" ]]; then
            die "$daemon exists with other settings: merge data-root $DOCKER_ROOT by hand"
        fi
        write_file "$daemon" 644 "$want_daemon"
        # Docker docs (engine/daemon, "Configure the data directory location"):
        # with the containerd image store, data-root doesn't move image data;
        # containerd's own root does. cri stays disabled, as containerd.io ships it.
        local want_ctd="# Written by deploy/server/setup.sh (E1): containerd's storage on the data volume.
version = 2
root = \"$CONTAINERD_ROOT\"
disabled_plugins = [\"cri\"]"
        if [[ -f "$ctd" && "$(cat "$ctd")" != "$want_ctd" ]]; then
            die "$ctd exists with other settings: set root = \"$CONTAINERD_ROOT\" by hand"
        fi
        write_file "$ctd" 644 "$want_ctd"

        codename=$(os_field VERSION_CODENAME)
        arch=$(dpkg --print-architecture)
        mkdir -p "$(P /etc/apt/keyrings)"
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$(P /etc/apt/keyrings/docker.asc)"
        chmod a+r "$(P /etc/apt/keyrings/docker.asc)"
        write_file "$(P /etc/apt/sources.list.d/docker.list)" 644 \
            "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable"
        apt-get update < /dev/null
        # confold keeps the containerd config written above.
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
            $DOCKER_PACKAGES < /dev/null
    fi

    local root status croot
    root=$(docker info --format '{{.DockerRootDir}}')
    [[ "$root" == "$DOCKER_ROOT" ]] || die "Docker's data root is '$root', expected $DOCKER_ROOT"
    status=$(docker info --format '{{.Driver}} {{json .DriverStatus}}')
    echo "Storage:"
    echo "  Docker data root (volumes, container state): $root"
    if [[ "$status" == *containerd.snapshotter* ]]; then
        croot=$(containerd config dump 2>/dev/null | sed -n 's/^root = "\(.*\)"$/\1/p' | head -1)
        [[ "$croot" == "$CONTAINERD_ROOT" ]] \
            || die "images use the containerd store, whose root is '$croot', not $CONTAINERD_ROOT"
        echo "  Images and container snapshots: containerd image store under $croot"
    else
        echo "  Images and container snapshots: $status under $root"
    fi
    df -h / "$DATA_MNT" | sed 's/^/  /'
    echo "Start only with $DATA_MNT mounted:"
    verify_mount_dropins
    echo "iptables FORWARD after Docker (its jumps must sit above Oracle's REJECT; E2 records this):"
    iptables -L FORWARD -n --line-numbers -v | sed 's/^/  /'
}

step_users_ssh() {
    note "== 5. user $USER_NAME and SSH"
    if ! id "$USER_NAME" > /dev/null 2>&1; then
        useradd --create-home --shell /bin/bash "$USER_NAME"
        note "created user $USER_NAME"
    fi
    local groups src dst
    groups=" $(id -nG "$USER_NAME") "
    case "$groups" in *" sudo "*|*" admin "*) die "$USER_NAME is in a sudo group: remove it by hand (it must not have sudo)" ;; esac
    case "$groups" in
        *" docker "*) note "$USER_NAME is in the docker group" ;;
        *) usermod -aG docker "$USER_NAME"; note "added $USER_NAME to the docker group" ;;
    esac

    src="$(P /home/ubuntu/.ssh/authorized_keys)"
    dst="$(P /home/$USER_NAME/.ssh/authorized_keys)"
    [[ -s "$src" ]] || die "no keys in /home/ubuntu/.ssh/authorized_keys to copy"
    if [[ ! -s "$dst" ]]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
        chmod 700 "$(dirname "$dst")"
        chmod 600 "$dst"
        chown -R "$USER_NAME:$USER_NAME" "$(dirname "$dst")"
        note "copied ubuntu's authorized_keys to $USER_NAME"
    elif ! cmp -s "$src" "$dst"; then
        note "WARNING: $USER_NAME's authorized_keys differ from ubuntu's; left as is"
    fi

    local conf backup=""
    conf="$(P /etc/ssh/sshd_config.d/00-nameleaf.conf)"
    [[ ! -f "$conf" ]] || backup=$(cat "$conf")
    # 00- sorts first: sshd keeps the first value it reads, and Ubuntu's
    # sshd_config includes sshd_config.d/*.conf before its own settings.
    write_file "$conf" 644 "# Written by deploy/server/setup.sh (E1): key-only SSH, no root login.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes"
    if (( CHANGED )); then
        if ! sshd -t; then
            if [[ -n "$backup" ]]; then printf '%s\n' "$backup" > "$conf"; else rm -f "$conf"; fi
            die "sshd -t rejected the configuration; restored the previous state, nothing reloaded"
        fi
        systemctl reload ssh
        note "sshd reloaded"
    fi
    local eff
    eff=$(sshd -T 2>/dev/null | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) ' | sort | tr '\n' ';')
    [[ "$eff" == "kbdinteractiveauthentication no;passwordauthentication no;permitrootlogin no;pubkeyauthentication yes;" ]] \
        || die "effective sshd settings are '$eff': another file overrides 00-nameleaf.conf"
    note "effective sshd: $eff"
}

mkd() {  # path mode
    mkdir -p "$(P "$1")"
    chmod "$2" "$(P "$1")"
    chown "$USER_NAME:$USER_NAME" "$(P "$1")"
}

step_dirs() {
    note "== 6. directories, nameleaf-compose, backup cron"
    mkd "$SRV" 755
    mkd "$SRV/releases" 755
    mkd "$SRV/publish" 755
    mkd "$SRV/state" 755
    mkd "$SRV/env" 700
    mkd "$BACKUP_DIR" 755
    mkd "$BACKUP_DIR/live" 750
    mkdir -p "$(P /usr/local/bin)"
    ln -sfn "$SRV/repo/deploy/server/compose.sh" "$(P /usr/local/bin/nameleaf-compose)"
    note "nameleaf-compose -> $SRV/repo/deploy/server/compose.sh"
    local cron
    cron="$(P /etc/cron.d/nameleaf-backup)"
    if [[ -f "$cron" ]]; then
        note "kept existing ${cron#"$ROOT"} (E3 may have edited the retention)"
    else
        write_file "$cron" 644 "# Nightly backup of schema live (deploy/server/backup_live.sh; written by setup.sh, E1).
# Exits 3 until the rclone remote exists (E3). Retention: change the two numbers in E3.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3 * * * $USER_NAME $SRV/repo/deploy/server/backup_live.sh --keep-local-days 7 --keep-remote-days 30 >> $BACKUP_DIR/backup.log 2>&1"
    fi
}

step_print() {
    cat <<EOF

== 7. Steps this script cannot do ==
Oracle console:
  1. nameleaf-vcn > public subnet > security list: ingress from 0.0.0.0/0 for
     TCP 80, TCP 443 and UDP 443 (TCP 22 is already there). Nothing else.
  2. nameleaf-vcn: confirm no flow logs are enabled (they would record visitor
     IPs; D-4.7). If any are, the privacy page needs updating first.

SSH (now, before closing this session):
  In a SECOND terminal on the Mac: ssh -o User=$USER_NAME nameleaf-prod-admin
  then: id   (expect groups $USER_NAME docker, no sudo)  and  docker ps
  Close this session only after that login works. Then add the
  nameleaf-prod alias (User $USER_NAME) to ~/.ssh/config (deploy/README.md).

From the Mac (record the output in E2):
  curl -sv -o /dev/null http://<server-ip>/
  nmap -Pn -p 22,80,443,3000,5432,8000 <server-ip>
      22 open; 80/443 open once the stack runs (closed or filtered before);
      3000, 5432, 8000 never open.
  sudo nmap -sU -p 443 <server-ip>      (open|filtered is normal for UDP)
EOF
}

main() {
    local mode="${1:-}"
    [[ -n "$mode" ]] || usage
    shift
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --data-device) DEV="${2:-}"; shift 2 ;;
            --data-size-gb) SIZE_GB="${2:-}"; shift 2 ;;
            --format-data-device) FORMAT_DEV="${2:-}"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    [[ -n "$DEV" ]] || die "--data-device needs a value"
    [[ "$SIZE_GB" =~ ^[1-9][0-9]*$ ]] || die "--data-size-gb must be a positive integer"
    [[ -n "$ROOT" || "$(id -u)" == 0 ]] || die "run as root: sudo bash setup.sh $mode"
    case "$mode" in
        preflight)
            [[ -z "$FORMAT_DEV" ]] || die "--format-data-device belongs to apply"
            preflight ;;
        apply)
            mkdir -p "$(P /var/log)"
            exec > >(tee -a "$(P /var/log/nameleaf-setup.log)") 2>&1
            preflight
            step_data
            step_packages
            step_firewall
            step_docker
            step_users_ssh
            step_dirs
            step_print
            note "apply finished" ;;
        *) usage ;;
    esac
}

main "$@"
