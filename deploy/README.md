# Server and deploy tooling (E1)

Scripts that set up the Oracle server, build and ship images, deploy and
roll back, back up schema `live`, and check a deployed site. Written and
tested locally in E1 (Findings E-1.x in `notes/PUBLISHING_ROADMAP.md`).
**The user runs all of them, in E2–E4.** Claude Code never runs anything
here against a server (CLAUDE.md Safety rule 2).

- Every remote-facing script takes an **SSH alias** as a required argument.
  A plain alias is all it accepts: no hostname, IP or `user@`. Nothing in this
  repo names the server; the alias lives in `~/.ssh/config` on the Mac.
- Smoke and load scripts take a **base URL** as a required argument and run
  from the Mac.
- Every SSH call uses `BatchMode=yes`, so load the key first, once per
  session: `ssh-add ~/.ssh/nameleaf_prod_ed25519` (its passphrase is in the
  password manager, not the keychain; E-0.4).

## SSH aliases

Two aliases for the same server, in `~/.ssh/config` (placeholders here):

```
Host nameleaf-prod-admin
    HostName <server-ip>
    User ubuntu
    IdentityFile ~/.ssh/nameleaf_prod_ed25519
    IdentitiesOnly yes

Host nameleaf-prod
    HostName <server-ip>
    User nameleaf
    IdentityFile ~/.ssh/nameleaf_prod_ed25519
    IdentitiesOnly yes
```

| Alias | User | Used for |
|---|---|---|
| `nameleaf-prod-admin` | `ubuntu` (has sudo) | `server/setup.sh`, and anything else that needs sudo |
| `nameleaf-prod` | `nameleaf` (docker group, no sudo) | `images.sh`, `deploy.sh`, `smoke.py`, `load_probe.py`, `vector_parity.sh`, and the server-side scripts deploy puts in `/srv/nameleaf/repo` |

Order in E2:
1. Create `nameleaf-prod-admin` only.
2. Run `setup.sh` through it (below).
3. In a second terminal, log in as `nameleaf`:
   `ssh -o User=nameleaf nameleaf-prod-admin`.
4. Only then add `nameleaf-prod` (User `nameleaf`).

## Scripts

| Script | Runs on | Purpose | Arguments |
|---|---|---|---|
| `server/setup.sh` | server, as root | Hardening and setup (6c): preflight against E-0.6, data volume, firewall, Docker, user, SSH, dirs, backup cron | `preflight` or `apply`; `[--data-device DEV] [--data-size-gb N]`; `apply --format-data-device DEV` to format the data volume |
| `images.sh` | Mac | Build `linux/arm64` images tagged with the git short SHA (12 chars), with the server's SITE_URL; ship over SSH (6a, 6d) | `build --site-url URL`; `ship --alias A [--tag T] [--replace-tag]` |
| `deploy.sh` | Mac | Deploy HEAD, or roll back (6e) | `bootstrap A`; `deploy A --base-url URL [--site-url URL] [--insecure]`; `rollback A --base-url URL`; `status A` |
| `smoke.py` | Mac | Smoke suite (7a); deploy.sh runs it after every deploy | `--base-url URL [--alias A] [--site-url URL] [--insecure]` |
| `load_probe.py` | Mac | 10 concurrent searches + rate-limit probe, sampling memory and swap (7b) | `--base-url URL --alias A [--concurrency N] [--skip-rate-probe]` |
| `vector_parity.sh` | Mac (+ server via alias) | Embed the B-1.7 texts inside an image (`--network none`) and compare; report only | `encode --image I --out F [--alias A]`; `compare --image I A.json B.json` |
| `server/release.sh` | server | deploy.sh's server half: preflight, bootstrap, activate, rollback, prune, status | called by deploy.sh |
| `server/compose.sh` | server | `nameleaf-compose`: the only way to run compose on the server | compose arguments |
| `server/backup_live.sh` | server, cron | Nightly `pg_dump -n live` + `rclone copy` (6f) | `--keep-local-days N --keep-remote-days N` |
| `server/restore_live_test.sh` | server | Restore a live dump into a scratch database (E3) | `--dump F --scratch-db restoretest_<x>` |
| `compose.server.yml` | server | Compose override that removes `build:` | used by `nameleaf-compose` |

Each script's header has the details, refusals and exit codes.

## Server layout

| Path | What |
|---|---|
| `/mnt/data` | 120 GB data volume (ext4, fstab by UUID, `nofail`) |
| `/mnt/data/docker` | Docker `data-root`: volumes (every published database), container state |
| `/mnt/data/containerd` | containerd's root: images and container snapshots (Engine 29's default image store; Findings E-1) |
| `/mnt/data/backups/live/` | nightly `live` dumps; `backup.log` beside it |
| `/srv/nameleaf/releases/<sha12>` | one `git archive` per deployed commit |
| `/srv/nameleaf/repo` | symlink to the current release (backend/scripts for the publish gate, C-6.7) |
| `/srv/nameleaf/env/.env.production` | secrets, written by hand from `.env.production.example` (dir mode 700) |
| `/srv/nameleaf/state/` | `image.env` (IMAGE_TAG), `deployed` (tag stack), `history.log`, `deploy.lock` |
| `/srv/nameleaf/publish/<stamp>` | publish build files (runbook) |
| `/srv/nameleaf/publish.lock` | flock shared by the backup and publish steps |
| `/usr/local/bin/nameleaf-compose` | link to `/srv/nameleaf/repo/deploy/server/compose.sh` |

**Compose on the server is always `nameleaf-compose …`.** It adds
`-f deploy/compose.server.yml`, the env file, and `state/image.env` (which
wins over any IMAGE_TAG in the env file). It refuses `--build` and `down -v`.
IMAGE_TAG is written only by `release.sh`; never edit it by hand.

**Publish steps take the publish lock**, so the nightly backup skips instead
of dumping mid-restore: e.g.
`flock /srv/nameleaf/publish.lock ./restore_reference.sh …` (runbook step 5).

## Order of use

**E2: server setup and first publish (bare IP)**
1. Create the instance (E-0.5). Then E0 Step 5-S4's readout, and reconcile E-0.6.
2. Run setup:
   ```bash
   scp deploy/server/setup.sh nameleaf-prod-admin:
   ssh -t nameleaf-prod-admin sudo bash setup.sh preflight
   ssh -t nameleaf-prod-admin sudo bash setup.sh apply --format-data-device /dev/oracleoci/oraclevdb
   ```
   Then work through the printed steps: the Oracle security list (TCP 80,
   TCP 443, UDP 443), confirming flow logs are off, the second-terminal login,
   and the `nameleaf-prod` alias. Record the printed FORWARD chain and storage
   locations. Keep the from-outside `curl`/`nmap` checks for after step 6.
3. Write `/srv/nameleaf/env/.env.production` from `.env.production.example`,
   with the server's SITE_URL (no Gemini key).
4. On the Mac:
   ```bash
   deploy/images.sh build --site-url <server SITE_URL>
   deploy/images.sh ship --alias nameleaf-prod
   ```
5. `deploy/deploy.sh bootstrap nameleaf-prod`: repo + tag, and the db
   container only.
6. Transfer, `roles.sql`, restore and the gate (runbook steps 4–6, from
   `/srv/nameleaf/repo/backend/scripts/publish`, under the publish lock).
7. `deploy/deploy.sh deploy nameleaf-prod --base-url https://<server-ip> --insecure`.
   This creates backend, frontend and caddy, waits for health, runs the
   smoke suite, and prunes.
8. Vector parity:
   ```bash
   deploy/vector_parity.sh encode --image nameleaf-backend:<sha12> --alias nameleaf-prod --out parity_server.json
   deploy/vector_parity.sh compare --image nameleaf-backend:<sha12> backend/scripts/eval/publishB_baseline/parity/vectors_mac_cpu.json parity_server.json
   ```
   The compare runs inside the local copy of that image (step 4 built it).
9. The from-outside checks setup printed (`curl`, `nmap`).

**E3: DNS, TLS, backups**
- SITE_URL changes to the real domain: rebuild the frontend
  (`images.sh build --site-url https://<domain>`), then
  `images.sh ship --alias nameleaf-prod --replace-tag` if the commit didn't
  change. Then `deploy.sh deploy`.
- Configure the rclone remote `nameleaf-backup` as `nameleaf`
  (`rclone config`). Set `BACKUP_RCLONE_PATH` in the env file, and the
  retention numbers in `/etc/cron.d/nameleaf-backup`.
- Test the backup:
  ```bash
  /srv/nameleaf/repo/deploy/server/backup_live.sh --keep-local-days 7 --keep-remote-days 30
  /srv/nameleaf/repo/deploy/server/restore_live_test.sh --dump <file> --scratch-db restoretest_e3
  ```
  Then drop the scratch database yourself (the script prints the command).

**E4: launch verification**
- Smoke suite:
  `python3 deploy/smoke.py --base-url https://<domain> --alias nameleaf-prod --site-url https://<domain>`.
- Load test:
  `python3 deploy/load_probe.py --base-url https://<domain> --alias nameleaf-prod`.
  It warns before rate-limiting your own IP.
- Rollback drill:
  `deploy/deploy.sh rollback nameleaf-prod --base-url https://<domain>`,
  then `deploy.sh deploy` again.

## Firewall and Docker

- Published ports (80, 443/tcp+udp, Caddy only) are DNAT'd in the nat table
  and then pass the filter **FORWARD** chain, not INPUT (Docker docs: packets
  are "diverted before it reaches the INPUT and OUTPUT chains").
- Docker adds jumps to `DOCKER-USER` / `DOCKER-FORWARD`, and "rules appended
  to FORWARD will be processed after Docker's rules". So Oracle's final
  `FORWARD REJECT` sits below them.
- The INPUT rules setup adds are belt and braces, and they cover any traffic
  that reaches the host itself.
- E2 confirms the order with `iptables -L FORWARD -n --line-numbers` plus the
  outside checks.
- **Never run `netfilter-persistent save` (or `reload`) while Docker is
  running.** A save captures Docker's chains into `rules.v4`. A reload
  flushes them, which cuts off the published ports until Docker is
  restarted. setup.sh saves only before Docker exists, and refuses to edit
  the ruleset afterwards.

## What remains unverified (E2 checks)

- Every E-0.6 prediction:
  - aarch64 and Ubuntu 24.04;
  - the data disk at `/dev/oracleoci/oraclevdb` and its attachment type;
  - Oracle's iptables ruleset, no swap, no ufw.
- Where Docker's FORWARD jumps land relative to Oracle's REJECT, and the
  ports reachable from outside.
- Whether containerd 2.x on the server accepts the `version = 2` config with
  `root` on `/mnt/data` (setup.sh stops if images would land elsewhere).
- `systemctl reload ssh` under Ubuntu 24.04's socket-activated sshd, and
  fail2ban's sshd jail without an explicit backend (setup.sh verifies the
  jail and stops if it isn't running).
- Image comparison across the Mac's and the server's image stores (layer
  diff IDs + runtime config), and that an unchanged service's rebuilt image
  keeps its ID while the Mac's build cache holds it.
- That compose honours `stop_grace_period: 330s` when recreating the backend
  (a stop takes up to 330 s only with a search in flight), and the backend's
  exit code after `init: true`. The Mac re-measurement comes first: exit 143
  expected.
- That Caddy keeps reaching `backend:8000` after the backend is recreated
  alone.
- That `docker inspect` records a bind mount's source as compose passes it.
  The wrapper's `cd -P` makes that the release path, which is what prune
  relies on.
