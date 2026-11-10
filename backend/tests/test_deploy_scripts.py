"""
E1: the server and deploy scripts in deploy/ (images.sh, deploy.sh,
vector_parity.sh, server/{setup,release,compose,backup_live,restore_live_test}.sh).

Nothing here reaches a network, a server, Docker or a database. Every
external binary is a fake (tests/deploy_fakes.py) first on PATH; the fake
`ssh` runs the "remote" half of each command locally against a temp
/srv/nameleaf (NAMELEAF_SRV), with its own fake docker state. Aliases are
unresolvable placeholders (example-alias), and URLs use example.invalid.

Covered: every refusal (dirty tree, not main, HEAD != origin, CI pending,
failed or missing, missing or bad alias, local SITE_URL, a rehearsal or
local image shipped, a mismatched frontend, formatting a signed disk or the
wrong device, preflight mismatches), changed-service detection, first
deploy after bootstrap, rollback tag selection, retention (including a caddy
mounting an old release), the iptables save rule, backups (no remote, lock
held, the dump user), the compose wrapper's refusals, vector parity's
--network none, plus bash -n, shellcheck and an absolute-path lint.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_fakes  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
SERVER = DEPLOY / "server"
SHELL_SCRIPTS = sorted(DEPLOY.glob("*.sh")) + sorted(SERVER.glob("*.sh"))
ALIAS = "example-alias"
SITE = "https://example.invalid"
SHA_A = "aaaaaaaaaaaa" + "0" * 28
SHA_B = "bbbbbbbbbbbb" + "0" * 28
TAG_A, TAG_B = SHA_A[:12], SHA_B[:12]

# Fakes on PATH for every test. setup.sh's system tools come too; docker is
# left out of setup tests that start before Docker is installed.
FAKES = ["ssh", "docker", "git", "gh", "rclone", "curl", "flock", "python3",
         "uname", "lsblk", "blkid", "wipefs", "findmnt", "swapon", "iptables",
         "mkfs.ext4", "mount", "apt-get", "dpkg", "debconf-set-selections",
         "systemctl", "fail2ban-client", "containerd", "netfilter-persistent",
         "sshd", "useradd", "usermod", "id", "chown", "df"]
NEVER_REAL = ("ssh", "docker", "gh", "rclone", "git", "curl")


class Harness:
    def __init__(self, tmp: Path, fakes: list[str]):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name in fakes:
            deploy_fakes.make_launcher(self.bin, name)
        self.log = tmp / "calls.log"
        self.state = tmp / "state"
        self.srv = tmp / "srv"
        self.config: dict = {"alias": ALIAS, "rules": [], "images": {}, "remote_images": {}}
        self.config_path = tmp / "fake_config.json"
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(tmp),
            "FAKE_CONFIG": str(self.config_path),
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.state),
            "FAKE_BIN": str(self.bin),
            "NAMELEAF_SRV": str(self.srv),
            "NAMELEAF_POLL_SECONDS": "0",
            "NAMELEAF_HEALTH_TIMEOUT": "3",
            "LC_ALL": "C",
        }

    def rule(self, cmd: str, match: str = "", **kw) -> None:
        self.config["rules"].append({"cmd": cmd, "match": match, **kw})

    def run(self, script: Path, *args: str, extra_env: dict | None = None, stdin: str | None = None):
        self.config_path.write_text(json.dumps(self.config))
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run(["bash", str(script), *args], cwd=self.tmp, env=env,
                              input=stdin, capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def remote_state(self, name: str):
        f = self.state / "remote" / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path.resolve(), FAKES)
    # Every fake really is first on PATH (never the real binary).
    for name in NEVER_REAL:
        r = subprocess.run(["bash", "-c", f"command -v {name}"], env=harness.env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == str(harness.bin / name), (name, r.stdout)
    return harness


# --- static ------------------------------------------------------------------------

def test_expected_files_exist():
    for rel in ("lib.sh", "images.sh", "deploy.sh", "vector_parity.sh", "smoke.py",
                "load_probe.py", "compose.server.yml", "README.md",
                "server/lib.sh", "server/setup.sh", "server/release.sh", "server/compose.sh",
                "server/backup_live.sh", "server/restore_live_test.sh"):
        assert (DEPLOY / rel).is_file(), rel


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(DEPLOY)))
def test_bash_syntax(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None,
                    reason="shellcheck not installed (not installed by policy)")
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(DEPLOY)))
def test_shellcheck(script):
    r = subprocess.run(["shellcheck", "-x", script.name], cwd=script.parent,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("script", [p for p in SHELL_SCRIPTS if p.name != "lib.sh"],
                         ids=lambda p: str(p.relative_to(DEPLOY)))
def test_strict_mode_and_usage_header(script):
    text = script.read_text()
    assert "set -Eeuo pipefail" in text
    assert text.startswith("#!/usr/bin/env bash\n# ")


ABSOLUTE_CALL = re.compile(r"(?:^|[\s;|&(`\[\"'])/(?:[\w.-]+/)+(ssh|docker|gh|rclone|git|curl)(?=[\s\"',\]]|$)", re.M)


@pytest.mark.parametrize("path", SHELL_SCRIPTS + sorted(DEPLOY.glob("*.py")),
                         ids=lambda p: str(p.relative_to(DEPLOY)))
def test_no_absolute_paths_to_external_tools(path):
    """The fakes on PATH must always win: no /usr/bin/ssh and the like."""
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    assert not ABSOLUTE_CALL.findall(code)


def test_no_hostnames_or_ips_in_deploy_files():
    """Safety rule 2: only placeholders (example.invalid, <alias>, <server-ip>)."""
    allowed = {"0.0.0.0", "127.0.0.1"}
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        ips = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)) - allowed
        assert not ips, (path, ips)
        assert "nameleaf.app" not in text, path


# --- shared setup --------------------------------------------------------------------

ENV_FILE = """SITE_URL=https://example.invalid
POSTGRES_USER=pguser_secret_value
POSTGRES_PASSWORD=pw_secret_value
DATABASE_URL=postgresql+psycopg://nameleaf_app:app_secret_value@db:5432/name_generator_20261101
BACKUP_RCLONE_PATH=bucket-x/live
"""


def server_env(h: Harness, text: str = ENV_FILE) -> None:
    (h.srv / "env").mkdir(parents=True, exist_ok=True)
    (h.srv / "env" / ".env.production").write_text(text)


def server_images(h: Harness, tag: str, backend_id: str, frontend_id: str, site: str = SITE) -> None:
    h.config["remote_images"][f"nameleaf-backend:{tag}"] = {"id": backend_id}
    h.config["remote_images"][f"nameleaf-frontend:{tag}"] = {"id": frontend_id, "site_url": site}


def green_ci(h: Harness, sha: str) -> None:
    h.config["git"] = {"sha": sha, "branch": "main", "porcelain": "", "origin": sha}
    h.rule("gh", r"^run list --commit " + sha, stdout="completed success\ncompleted success\n")


def deploy(h: Harness, *args: str):
    return h.run(DEPLOY / "deploy.sh", *args)


def ssh_calls(h: Harness) -> list[str]:
    return [c for c in h.calls() if c.startswith("ssh ")]


def up_calls(h: Harness) -> list[str]:
    return [c for c in h.calls() if c.startswith("docker compose") and " up " in c]


# --- deploy.sh refusals ------------------------------------------------------------------

@pytest.mark.parametrize("git,why", [
    ({"branch": "feature"}, "not on main"),
    ({"porcelain": " M README.md\n"}, "dirty tree"),
    ({"origin": "f" * 40}, "HEAD is not origin's main"),
])
def test_deploy_refuses_git_state(h, git, why):
    green_ci(h, SHA_A)
    h.config["git"].update(git)
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 2, (why, r.stderr)
    assert ssh_calls(h) == [], why


@pytest.mark.parametrize("runs,why", [
    ("", "no CI run"),
    ("in_progress \n", "CI still running"),
    ("completed failure\n", "CI failed"),
    ("completed success\ncompleted cancelled\n", "one run not green"),
])
def test_deploy_refuses_unless_ci_green(h, runs, why):
    h.config["git"] = {"sha": SHA_A, "branch": "main", "porcelain": "", "origin": SHA_A}
    h.rule("gh", "^run list", stdout=runs)
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 2, (why, r.stdout, r.stderr)
    assert "CI" in r.stderr, why
    assert ssh_calls(h) == [], why


@pytest.mark.parametrize("args,why", [
    (["deploy"], "missing alias"),
    (["deploy", "--base-url", SITE], "flag where the alias goes"),
    (["deploy", "deploy@example.invalid", "--base-url", SITE], "user@host"),
    (["deploy", "host.example.invalid", "--base-url", SITE], "a hostname"),
    (["deploy", ALIAS], "missing --base-url"),
    (["deploy", ALIAS, "--base-url", "example.invalid"], "base url without scheme"),
    (["rollback", ALIAS], "rollback without --base-url"),
    (["status"], "status without alias"),
    (["nonsense", ALIAS], "unknown command"),
])
def test_deploy_usage_refusals_before_any_remote_call(h, args, why):
    green_ci(h, SHA_A)
    r = deploy(h, *args)
    assert r.returncode == 2, (why, r.stderr)
    assert ssh_calls(h) == [], why


def test_deploy_refuses_a_server_that_was_never_bootstrapped(h):
    green_ci(h, SHA_A)
    server_env(h)
    server_images(h, TAG_A, "sha256:b1", "sha256:f1")
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 2, r.stderr
    assert "bootstrap" in r.stderr
    assert up_calls(h) == []


def test_unreachable_alias_is_a_clear_refusal(h):
    green_ci(h, SHA_A)
    h.config["ssh_unreachable"] = True
    r = deploy(h, "bootstrap", ALIAS)
    assert r.returncode == 2
    assert "ssh-add" in r.stderr


# --- bootstrap, first deploy, changed-service detection ------------------------------------

def bootstrap(h: Harness, sha: str = SHA_A):
    green_ci(h, sha)
    server_env(h)
    return deploy(h, "bootstrap", ALIAS)


def test_bootstrap_starts_only_the_database(h):
    server_images(h, TAG_A, "sha256:b1", "sha256:f1")
    r = bootstrap(h)
    assert r.returncode == 0, r.stdout + r.stderr
    assert up_calls(h) == [next(c for c in h.calls() if " up " in c)]
    assert up_calls(h)[0].endswith("up -d --no-deps --no-build --pull never db")
    assert set(h.remote_state("containers")) == {"db"}
    assert (h.srv / "repo").resolve() == (h.srv / "releases" / TAG_A).resolve()
    assert (h.srv / "state" / "image.env").read_text() == f"IMAGE_TAG={TAG_A}\n"
    assert (h.srv / "state" / "deployed").read_text() == f"{TAG_A}\n"


def test_bootstrap_refuses_once_a_backend_exists(h):
    server_images(h, TAG_A, "sha256:b1", "sha256:f1")
    assert bootstrap(h).returncode == 0
    h.rule("python3", "smoke.py")
    assert deploy(h, "deploy", ALIAS, "--base-url", SITE).returncode == 0
    r = deploy(h, "bootstrap", ALIAS)
    assert r.returncode == 2
    assert "first publish only" in r.stderr


def test_first_deploy_after_bootstrap_creates_backend_frontend_and_caddy(h):
    server_images(h, TAG_A, "sha256:b1", "sha256:f1")
    assert bootstrap(h).returncode == 0
    db_id = h.remote_state("containers")["db"]["id"]
    h.rule("python3", "smoke.py")
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 0, r.stdout + r.stderr
    containers = h.remote_state("containers")
    assert set(containers) == {"db", "caddy", "backend", "frontend"}
    assert containers["db"]["id"] == db_id, "db must not be recreated"
    assert up_calls(h)[-1].endswith("up -d --no-deps --no-build --pull never caddy backend frontend")
    smoke = [c for c in h.calls() if c.startswith("python3")]
    assert smoke and f"--base-url {SITE} --alias {ALIAS}" in smoke[0]


def deploy_to(h: Harness, sha: str, backend_id: str, frontend_id: str):
    green_ci(h, sha)
    server_images(h, sha[:12], backend_id, frontend_id)
    h.rule("python3", "smoke.py")
    return deploy(h, "deploy", ALIAS, "--base-url", SITE)


def first_deploy(h: Harness):
    server_images(h, TAG_A, "sha256:b1", "sha256:f1")
    assert bootstrap(h).returncode == 0
    h.rule("python3", "smoke.py")
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 0, r.stdout + r.stderr


def test_only_the_service_whose_image_changed_is_recreated(h):
    first_deploy(h)
    before = h.remote_state("containers")
    r = deploy_to(h, SHA_B, "sha256:b2", "sha256:f1")   # frontend unchanged
    assert r.returncode == 0, r.stdout + r.stderr
    after = h.remote_state("containers")
    assert up_calls(h)[-1].endswith("--pull never backend")
    assert "backend image changed" in r.stdout
    for svc in ("db", "caddy", "frontend"):
        assert after[svc]["id"] == before[svc]["id"], svc
    assert after["backend"]["id"] != before["backend"]["id"]
    assert "330 s" in r.stdout


def test_compose_config_change_recreates_backend_and_frontend_not_db_or_caddy(h):
    first_deploy(h)
    before = h.remote_state("containers")
    h.config["config_version"] = 2
    r = deploy_to(h, SHA_B, "sha256:b1", "sha256:f1")   # same images
    assert r.returncode == 0, r.stdout + r.stderr
    assert "compose config changed" in r.stdout
    assert up_calls(h)[-1].endswith("--pull never backend frontend")
    after = h.remote_state("containers")
    assert after["db"]["id"] == before["db"]["id"]
    assert after["caddy"]["id"] == before["caddy"]["id"]


def test_identical_images_and_config_recreate_nothing(h):
    first_deploy(h)
    n_up = len(up_calls(h))
    r = deploy_to(h, SHA_B, "sha256:b1", "sha256:f1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(up_calls(h)) == n_up
    assert "nothing to recreate" in r.stdout


def test_changed_caddyfile_is_reported_not_applied(h):
    first_deploy(h)
    caddy_id = h.remote_state("containers")["caddy"]["id"]
    h.config["git"] = {}
    green_ci(h, SHA_B)
    server_images(h, TAG_B, "sha256:b1", "sha256:f1")
    h.rule("python3", "smoke.py")
    # A different Caddyfile in the new release.
    release_b = h.srv / "releases" / TAG_B
    shutil.copytree(h.srv / "releases" / TAG_A, release_b, symlinks=True)
    (release_b / "deploy" / "Caddyfile").write_text("# changed\n")
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTICE: changed since the previous release: deploy/Caddyfile" in r.stdout
    assert "nameleaf-compose up -d --no-deps --no-build --pull never caddy" in r.stdout
    assert h.remote_state("containers")["caddy"]["id"] == caddy_id


def test_preflight_refuses_missing_images_before_activating(h):
    first_deploy(h)
    green_ci(h, SHA_B)          # no images for TAG_B on the server
    n_up = len(up_calls(h))
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 2
    assert "images.sh ship" in r.stderr
    assert len(up_calls(h)) == n_up
    assert (h.srv / "state" / "image.env").read_text() == f"IMAGE_TAG={TAG_A}\n"


def test_preflight_refuses_a_frontend_built_for_another_site_url(h):
    first_deploy(h)
    green_ci(h, SHA_B)
    server_images(h, TAG_B, "sha256:b2", "sha256:f2", site="https://localhost")
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 2
    assert "D-3b" in r.stderr


def test_failed_health_prints_the_rollback_command(h):
    first_deploy(h)
    mark = len(h.calls())
    h.config["health"] = {"backend": "unhealthy"}
    r = deploy_to(h, SHA_B, "sha256:b2", "sha256:f1")
    assert r.returncode == 1
    assert f"deploy/deploy.sh rollback {ALIAS} --base-url {SITE}" in r.stderr
    assert not any("release.sh prune" in c for c in h.calls()[mark:])
    assert not any(c.startswith("python3") for c in h.calls()[mark:]), "no smoke after a failed activation"


def test_failed_smoke_prints_the_rollback_command_and_does_not_prune(h):
    first_deploy(h)
    mark = len(h.calls())
    green_ci(h, SHA_B)
    server_images(h, TAG_B, "sha256:b2", "sha256:f1")
    h.config["rules"] = [r for r in h.config["rules"] if r["cmd"] != "python3"]
    h.rule("python3", "smoke.py", rc=1)
    r = deploy(h, "deploy", ALIAS, "--base-url", SITE)
    assert r.returncode == 1
    assert "smoke suite" in r.stderr
    assert f"deploy/deploy.sh rollback {ALIAS}" in r.stderr
    assert not any("release.sh prune" in c for c in h.calls()[mark:])


# --- rollback and retention --------------------------------------------------------------

SHAS = [c * 12 + "0" * 28 for c in "abcde"]


def deploy_series(h: Harness, n: int, frontend_same: bool = True) -> list[str]:
    first_deploy(h)  # SHAS[0] == SHA_A
    for i, sha in enumerate(SHAS[1:n], start=2):
        r = deploy_to(h, sha, f"sha256:b{i}", "sha256:f1" if frontend_same else f"sha256:f{i}")
        assert r.returncode == 0, r.stdout + r.stderr
    return [s[:12] for s in SHAS[:n]]


def test_rollback_steps_back_one_deployed_tag_at_a_time(h):
    tags = deploy_series(h, 3)
    r = deploy(h, "rollback", ALIAS, "--base-url", SITE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (h.srv / "state" / "image.env").read_text() == f"IMAGE_TAG={tags[1]}\n"
    assert (h.srv / "repo").resolve().name == tags[1]
    assert h.remote_state("containers")["backend"]["ref"] == f"nameleaf-backend:{tags[1]}"
    assert (h.srv / "state" / "deployed").read_text().split() == tags[:2]
    r = deploy(h, "rollback", ALIAS, "--base-url", SITE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (h.srv / "state" / "image.env").read_text() == f"IMAGE_TAG={tags[0]}\n"
    r = deploy(h, "rollback", ALIAS, "--base-url", SITE)
    assert r.returncode != 0
    assert "no previous tag" in r.stderr


def test_rollback_recreates_only_what_differs(h):
    deploy_series(h, 2)              # frontend image identical across tags
    before = h.remote_state("containers")
    r = deploy(h, "rollback", ALIAS, "--base-url", SITE)
    assert r.returncode == 0, r.stdout + r.stderr
    after = h.remote_state("containers")
    assert after["backend"]["id"] != before["backend"]["id"]
    for svc in ("db", "caddy", "frontend"):
        assert after[svc]["id"] == before[svc]["id"], svc


def test_prune_keeps_three_tags_plus_anything_in_use(h):
    tags = deploy_series(h, 5)
    images = h.remote_state("images")
    kept_backend = sorted(r.split(":")[1] for r in images if r.startswith("nameleaf-backend:"))
    assert kept_backend == sorted(tags[2:]), "backend: the current and previous two tags"
    # The frontend container was never recreated (same image), so it still runs
    # from tag A's reference: that tag stays.
    kept_frontend = sorted(r.split(":")[1] for r in images if r.startswith("nameleaf-frontend:"))
    assert kept_frontend == sorted([tags[0]] + tags[2:])
    # caddy was created in release A and mounts its Caddyfile: release A stays;
    # release B (no longer kept, mounted by nothing) is removed.
    releases = sorted(p.name for p in (h.srv / "releases").iterdir())
    assert releases == sorted([tags[0]] + tags[2:])
    assert h.remote_state("containers")["caddy"]["mounts"][0].startswith(str(h.srv / "releases" / tags[0]))
    rm_calls = [c for c in h.calls() if c.startswith("docker image rm")]
    assert rm_calls and not any(" -f" in c or "--force" in c for c in rm_calls)


def test_prune_never_forces_an_image_a_container_needs(h):
    deploy_series(h, 4)
    containers = h.remote_state("containers")
    images = h.remote_state("images") or h.config["remote_images"]
    for c in containers.values():
        if c["ref"].startswith("nameleaf-"):
            assert any(v["id"] == c["image_id"] for v in images.values()), c


# --- compose wrapper ----------------------------------------------------------------------

def with_release(h: Harness, tag: str = TAG_A) -> None:
    rel = h.srv / "releases" / tag
    shutil.copytree(DEPLOY, rel / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "docker-compose.prod.yml", rel)
    (h.srv / "repo").symlink_to(f"releases/{tag}")
    (h.srv / "state").mkdir(parents=True, exist_ok=True)
    (h.srv / "state" / "image.env").write_text(f"IMAGE_TAG={tag}\n")
    server_env(h)


@pytest.mark.parametrize("args", [["down", "-v"], ["down", "--volumes"], ["down", "-tv"],
                                  ["--ansi", "never", "down", "--volumes=true"]])
def test_compose_wrapper_refuses_down_with_volumes(h, args):
    with_release(h)
    r = h.run(SERVER / "compose.sh", *args)
    assert r.returncode == 2
    assert not any(c.startswith("docker") for c in h.calls())


def test_compose_wrapper_refuses_build(h):
    with_release(h)
    r = h.run(SERVER / "compose.sh", "up", "-d", "--build")
    assert r.returncode == 2
    assert not any(c.startswith("docker") for c in h.calls())


def test_compose_wrapper_passes_both_files_and_env_files_from_the_release(h):
    with_release(h)
    h.config["containers"] = {}
    r = h.run(SERVER / "compose.sh", "ps", "-q", "db")
    assert r.returncode == 0, r.stderr
    call = h.calls()[-1]
    assert call == ("docker compose -f docker-compose.prod.yml -f deploy/compose.server.yml "
                    f"--env-file {h.srv}/env/.env.production --env-file {h.srv}/state/image.env ps -q db")


def test_compose_wrapper_refuses_before_any_tag_exists(h):
    with_release(h)
    (h.srv / "state" / "image.env").unlink()
    r = h.run(SERVER / "compose.sh", "ps")
    assert r.returncode == 2
    assert "deploy.sh" in r.stderr


# --- images.sh -------------------------------------------------------------------------------

@pytest.mark.parametrize("site", ["", "https://localhost", "https://LOCALHOST/", "https://app.localhost",
                                  "https://127.0.0.1", "https://127.1.2.3:8443", "https://0.0.0.0",
                                  "https://[::1]", "http://example.invalid", "https://example.invalid/path"])
def test_build_refuses_bad_site_urls(h, site):
    h.config["git"] = {"sha": SHA_A}
    r = h.run(DEPLOY / "images.sh", "build", "--site-url", site)
    assert r.returncode == 2, (site, r.stderr)
    assert not any(c.startswith("docker build") for c in h.calls())


def test_build_refuses_a_dirty_tree(h):
    h.config["git"] = {"sha": SHA_A, "porcelain": "?? x\n"}
    r = h.run(DEPLOY / "images.sh", "build", "--site-url", SITE)
    assert r.returncode == 2
    assert not any(c.startswith("docker build") for c in h.calls())


def test_build_tags_the_short_sha_for_arm64_without_attestations(h):
    h.config["git"] = {"sha": SHA_A}
    r = h.run(DEPLOY / "images.sh", "build", "--site-url", SITE)
    assert r.returncode == 0, r.stderr
    builds = [c for c in h.calls() if c.startswith("docker build")]
    assert len(builds) == 2
    for b in builds:
        assert "--platform linux/arm64 --provenance=false --sbom=false" in b
    assert f"-t nameleaf-backend:{TAG_A}" in builds[0]
    assert f"--build-arg SITE_URL={SITE} --label org.nameleaf.site_url={SITE}" in builds[1]
    assert f"-t nameleaf-frontend:{TAG_A}" in builds[1]


def test_rehearsal_build_allows_localhost_and_tags_rehearsal(h):
    h.config["git"] = {"sha": SHA_A, "porcelain": " M x\n"}
    r = h.run(DEPLOY / "images.sh", "build", "--site-url", "https://localhost", "--rehearsal")
    assert r.returncode == 0, r.stderr
    assert any("-t nameleaf-frontend:rehearsal" in c for c in h.calls())


def test_ship_refuses_rehearsal_tags(h):
    h.config["images"] = {"nameleaf-frontend:rehearsal": {"id": "x", "site_url": "https://localhost"}}
    r = h.run(DEPLOY / "images.sh", "ship", "--alias", ALIAS, "--tag", "rehearsal")
    assert r.returncode == 2
    assert ssh_calls(h) == []


def test_ship_refuses_a_frontend_built_for_localhost(h):
    h.config["git"] = {"sha": SHA_A}
    h.config["images"] = {f"nameleaf-backend:{TAG_A}": {"id": "b"},
                          f"nameleaf-frontend:{TAG_A}": {"id": "f", "site_url": "https://localhost"}}
    r = h.run(DEPLOY / "images.sh", "ship", "--alias", ALIAS)
    assert r.returncode == 2
    assert "D-3b" in r.stderr
    assert ssh_calls(h) == []


def test_ship_refuses_missing_alias(h):
    r = h.run(DEPLOY / "images.sh", "ship")
    assert r.returncode == 2
    assert ssh_calls(h) == []


def local_images(h: Harness) -> None:
    h.config["git"] = {"sha": SHA_A}
    h.config["images"] = {f"nameleaf-backend:{TAG_A}": {"id": "sha256:b1", "fingerprint": "FP-B1"},
                          f"nameleaf-frontend:{TAG_A}": {"id": "sha256:f1", "fingerprint": "FP-F1",
                                                         "site_url": SITE}}


def test_ship_sends_what_the_server_lacks_and_verifies_it(h):
    local_images(h)
    h.config["load_adds"] = [{f"nameleaf-backend:{TAG_A}": {"id": "sha256:srv-b1", "fingerprint": "FP-B1"}},
                             {f"nameleaf-frontend:{TAG_A}": {"id": "sha256:srv-f1", "fingerprint": "FP-F1"}}]
    r = h.run(DEPLOY / "images.sh", "ship", "--alias", ALIAS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [c for c in h.calls() if c.startswith("docker save")] == [
        f"docker save nameleaf-backend:{TAG_A}", f"docker save nameleaf-frontend:{TAG_A}"]
    assert sum(c == "docker load" for c in h.calls()) == 2


def test_ship_skips_images_already_on_the_server(h):
    local_images(h)
    h.config["remote_images"] = {f"nameleaf-backend:{TAG_A}": {"id": "sha256:srv-b1", "fingerprint": "FP-B1"},
                                 f"nameleaf-frontend:{TAG_A}": {"id": "sha256:srv-f1", "fingerprint": "FP-F1"}}
    r = h.run(DEPLOY / "images.sh", "ship", "--alias", ALIAS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already on the server, skipped" in r.stdout
    assert not any(c.startswith(("docker save", "docker load")) for c in h.calls())


def test_ship_retags_an_unchanged_image_instead_of_sending_it(h):
    local_images(h)
    h.config["remote_images"] = {"nameleaf-backend:0123456789ab": {"id": "sha256:old", "fingerprint": "FP-B1"},
                                 f"nameleaf-frontend:{TAG_A}": {"id": "sha256:srv-f1", "fingerprint": "FP-F1"}}
    r = h.run(DEPLOY / "images.sh", "ship", "--alias", ALIAS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"docker tag nameleaf-backend:0123456789ab nameleaf-backend:{TAG_A}" in h.calls()
    assert not any(c.startswith("docker save") for c in h.calls())


def test_ship_refuses_a_different_image_under_the_same_tag(h):
    local_images(h)
    h.config["remote_images"] = {f"nameleaf-backend:{TAG_A}": {"id": "sha256:x", "fingerprint": "OTHER"}}
    r = h.run(DEPLOY / "images.sh", "ship", "--alias", ALIAS)
    assert r.returncode == 2
    assert "--replace-tag" in r.stderr
    assert not any(c.startswith("docker save") for c in h.calls())


# --- vector_parity.sh --------------------------------------------------------------------------

def test_parity_encode_locally_runs_without_network(h):
    h.rule("docker", "^run ", stdout='{"meta": {}}\n')
    out = h.tmp / "a.json"
    r = h.run(DEPLOY / "vector_parity.sh", "encode", "--image", "nameleaf-backend:rehearsal", "--out", str(out))
    assert r.returncode == 0, r.stderr
    run = next(c for c in h.calls() if c.startswith("docker run"))
    assert run.startswith("docker run --rm --network none")
    assert f"-v {REPO}/backend/scripts/eval:/probe:ro" in run
    # The image holds only the pinned snapshot (no refs/main): pass it.
    pinned = re.search(r'^DEFAULT_EMBEDDING_MODEL_REVISION = "([0-9a-f]{40})"$',
                       (REPO / "backend/app/services/embedding_provider.py").read_text(), re.M).group(1)
    assert run.endswith(f"--threads 2 --revision {pinned}")
    assert out.read_text().startswith("{")


def test_parity_encode_on_the_server_runs_without_network(h):
    h.rule("docker", "^run ", stdout='{"meta": {}}\n')
    out = h.tmp / "s.json"
    r = h.run(DEPLOY / "vector_parity.sh", "encode", "--image", f"nameleaf-backend:{TAG_A}",
              "--alias", ALIAS, "--out", str(out))
    assert r.returncode == 0, r.stderr
    ssh = [c for c in ssh_calls(h) if "docker run" in c]
    assert ssh and "docker run --rm --network none" in ssh[0]
    assert f"{h.srv}/repo/backend/scripts/eval:/probe:ro" in ssh[0]


def test_parity_compare_runs_inside_the_image_without_network(h):
    a, b = h.tmp / "a.json", h.tmp / "b.json"
    a.write_text("{}"); b.write_text("{}")
    r = h.run(DEPLOY / "vector_parity.sh", "compare", "--image", "nameleaf-backend:rehearsal", str(a), str(b))
    assert r.returncode == 0, r.stderr
    assert "REPORT ONLY" in r.stdout
    run = next(c for c in h.calls() if c.startswith("docker run"))
    assert run.startswith("docker run --rm --network none") and "--compare /in/a.json /in/b.json" in run


@pytest.mark.parametrize("args", [["encode", "--out", "x.json"], ["encode", "--image", "img:tag"],
                                  ["encode", "--image", "img:tag", "--out", "x.json", "--alias", "a.b.c"],
                                  ["compare", "--image", "img:tag", "only-one.json"]])
def test_parity_usage_refusals(h, args):
    r = h.run(DEPLOY / "vector_parity.sh", *args)
    assert r.returncode == 2
    assert not any(c.startswith(("docker", "ssh")) for c in h.calls())


def test_parity_refuses_existing_output(h):
    (h.tmp / "x.json").write_text("{}")
    r = h.run(DEPLOY / "vector_parity.sh", "encode", "--image", "img:tag", "--out", str(h.tmp / "x.json"))
    assert r.returncode == 2


# --- backup_live.sh ------------------------------------------------------------------------------

def backup_setup(h: Harness, env_text: str = ENV_FILE) -> dict:
    with_release(h)
    server_env(h, env_text)
    h.config["containers"] = {"db": {"id": "cid-db-7", "ref": "pgvector/pgvector:pg17", "image_id": "x",
                                     "hash": "", "health": "healthy", "has_health": True, "mounts": []}}
    h.rule("rclone", "^listremotes", stdout="nameleaf-backup:\n")
    h.rule("docker", "^exec ", stdout="PGDMP-fake-dump")
    return {"NAMELEAF_BACKUP_ROOT": str(h.tmp / "backups"), "FAKE_REMOTE": "1"}


def run_backup(h: Harness, env: dict):
    return h.run(SERVER / "backup_live.sh", "--keep-local-days", "7", "--keep-remote-days", "30", extra_env=env)


def test_backup_dumps_live_as_postgres_user_from_the_env_file(h):
    env = backup_setup(h)
    r = run_backup(h, env)
    assert r.returncode == 0, r.stdout + r.stderr
    dump = next(c for c in h.calls() if c.startswith("docker exec"))
    assert dump == ("docker exec -e PGOPTIONS=-c lock_timeout=30s cid-db-7 pg_dump -U pguser_secret_value "
                    "-d name_generator_20261101 -n live -Fc --lock-wait-timeout=30s")
    files = list((h.tmp / "backups" / "live").glob("live_name_generator_20261101_*.dump"))
    assert len(files) == 1 and files[0].read_text() == "PGDMP-fake-dump"
    assert any(c.startswith(f"rclone copy {files[0]} nameleaf-backup:bucket-x/live") for c in h.calls())
    assert any(c.startswith("rclone delete --min-age 30d --include live_*.dump nameleaf-backup:bucket-x/live")
               for c in h.calls())
    for secret in ("pguser_secret_value", "pw_secret_value", "app_secret_value"):
        assert secret not in r.stdout + r.stderr


def test_backup_finds_the_db_container_through_nameleaf_compose(h):
    env = backup_setup(h)
    assert run_backup(h, env).returncode == 0
    assert any(c.endswith("ps -q db") and c.startswith("docker compose -f docker-compose.prod.yml")
               for c in h.calls())


def test_backup_skips_when_the_publish_lock_is_held(h):
    env = backup_setup(h)
    h.rule("flock", "", rc=1)
    r = run_backup(h, env)
    assert r.returncode == 5
    assert "skipped: publish lock held" in r.stdout
    assert not any(c.startswith(("docker exec", "rclone")) for c in h.calls())


def test_backup_exits_3_without_a_remote_path(h):
    env = backup_setup(h, ENV_FILE.replace("BACKUP_RCLONE_PATH=bucket-x/live", "BACKUP_RCLONE_PATH="))
    r = run_backup(h, env)
    assert r.returncode == 3
    assert "rclone remote not configured" in r.stdout
    assert not any(c.startswith("docker exec") for c in h.calls())


def test_backup_exits_3_without_the_rclone_remote(h):
    env = backup_setup(h)
    h.config["rules"] = [r for r in h.config["rules"] if r["cmd"] != "rclone"]
    h.rule("rclone", "^listremotes", stdout="something-else:\n")
    r = run_backup(h, env)
    assert r.returncode == 3
    assert not any(c.startswith("docker exec") for c in h.calls())


def test_backup_refuses_a_missing_postgres_user(h):
    env = backup_setup(h, ENV_FILE.replace("POSTGRES_USER=pguser_secret_value\n", ""))
    r = run_backup(h, env)
    assert r.returncode == 2
    assert "POSTGRES_USER" in r.stderr
    assert not any(c.startswith("docker exec") for c in h.calls())


@pytest.mark.parametrize("args", [[], ["--keep-local-days", "7"], ["--keep-local-days", "x", "--keep-remote-days", "3"]])
def test_backup_usage_errors(h, args):
    env = backup_setup(h)
    r = h.run(SERVER / "backup_live.sh", *args, extra_env=env)
    assert r.returncode == 2


# --- restore_live_test.sh ---------------------------------------------------------------------

@pytest.mark.parametrize("name,why", [
    ("name_generator_20261101", "the current database / master's name"),
    ("scratch", "not restoretest_"),
    ("restoretest_X", "upper case"),
])
def test_restore_test_refuses_names(h, name, why):
    env = backup_setup(h)
    dump = h.tmp / "d.dump"; dump.write_text("x")
    r = h.run(SERVER / "restore_live_test.sh", "--dump", str(dump), "--scratch-db", name, extra_env=env)
    assert r.returncode == 2, why
    assert not any(c.startswith("docker exec") for c in h.calls())


def test_restore_test_refuses_the_current_database(h):
    env = backup_setup(h, ENV_FILE.replace("name_generator_20261101", "restoretest_live"))
    dump = h.tmp / "d.dump"; dump.write_text("x")
    r = h.run(SERVER / "restore_live_test.sh", "--dump", str(dump), "--scratch-db", "restoretest_live", extra_env=env)
    assert r.returncode == 2
    assert "current database" in r.stderr


def test_restore_test_refuses_an_existing_database_and_never_drops(h):
    env = backup_setup(h)
    h.config["rules"].insert(0, {"cmd": "docker", "match": "pg_database", "stdout": "1\n"})
    dump = h.tmp / "d.dump"; dump.write_text("x")
    r = h.run(SERVER / "restore_live_test.sh", "--dump", str(dump), "--scratch-db", "restoretest_e3", extra_env=env)
    assert r.returncode == 2
    assert "already exists" in r.stderr
    assert not any("CREATE DATABASE" in c or "pg_restore" in c for c in h.calls())


def test_restore_test_restores_live_only_and_prints_the_drop_for_the_user(h):
    env = backup_setup(h)
    h.config["rules"].insert(0, {"cmd": "docker", "match": "pg_database", "stdout": ""})
    h.config["rules"].insert(0, {"cmd": "docker", "match": "pg_tables", "stdout": "sense_selection_stats\n"})
    h.config["rules"].insert(0, {"cmd": "docker", "match": "count", "stdout": "0\n"})
    dump = h.tmp / "d.dump"; dump.write_text("x")
    r = h.run(SERVER / "restore_live_test.sh", "--dump", str(dump), "--scratch-db", "restoretest_e3", extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert any("pg_restore -U pguser_secret_value -d restoretest_e3 -n live --exit-on-error" in c for c in h.calls())
    assert not any("DROP" in c for c in h.calls())
    assert "DROP DATABASE restoretest_e3" in r.stdout


# --- setup.sh -----------------------------------------------------------------------------------

OS_RELEASE = 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n'
INPUT_RULES = """-P INPUT ACCEPT
-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT -p icmp -j ACCEPT
-A INPUT -i lo -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
-A INPUT -j REJECT --reject-with icmp-host-prohibited
"""
FORWARD_RULES = "-P FORWARD ACCEPT\n-A FORWARD -j REJECT --reject-with icmp-host-prohibited\n"
INPUT_LIST = """Chain INPUT (policy ACCEPT)
num  target     prot opt source               destination
1    ACCEPT     all  --  0.0.0.0/0            0.0.0.0/0            state RELATED,ESTABLISHED
2    ACCEPT     icmp --  0.0.0.0/0            0.0.0.0/0
3    ACCEPT     all  --  0.0.0.0/0            0.0.0.0/0
4    ACCEPT     tcp  --  0.0.0.0/0            0.0.0.0/0            state NEW tcp dpt:22
5    REJECT     all  --  0.0.0.0/0            0.0.0.0/0            reject-with icmp-host-prohibited
"""
SIZE_120_GIB = str(120 * 1024 ** 3)


def setup_server(h: Harness, docker_installed: bool = False, disk: str = "unformatted") -> Path:
    """A fake fresh Oracle server under NAMELEAF_SETUP_ROOT."""
    root = h.tmp / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text(OS_RELEASE)
    (root / "etc" / "fstab").write_text("LABEL=cloudimg-rootfs / ext4 defaults 0 1\n")
    keys = root / "home" / "ubuntu" / ".ssh"
    keys.mkdir(parents=True)
    (keys / "authorized_keys").write_text("ssh-ed25519 AAAAfake nameleaf-prod\n")
    if not docker_installed:
        (h.bin / "docker").unlink()
    rules = h.config["rules"]
    add = lambda cmd, match="", **kw: rules.append({"cmd": cmd, "match": match, **kw})  # noqa: E731
    add("uname", "-m", stdout="aarch64\n")
    add("lsblk", "", stdout=f"{SIZE_120_GIB} \n")
    if disk == "unformatted":
        add("blkid", "-p", if_flag="formatted", stdout="ext4\n")
        add("blkid", "-p", rc=2)
        add("blkid", "-s UUID", stdout="1111-uuid\n")
    elif disk == "ours":
        (root / "etc" / "fstab").write_text("UUID=1111-uuid /mnt/data ext4 defaults,nofail 0 2\n")
        add("blkid", "-p", stdout="ext4\n")
        add("blkid", "-s UUID", stdout="1111-uuid\n")
        add("findmnt", "", stdout="/dev/sdb\n")
    elif disk == "foreign":
        add("blkid", "-p", stdout="xfs\n")
        add("blkid", "-s UUID", stdout="2222-uuid\n")
    add("wipefs", "", stdout="")
    add("findmnt", "", if_flag="mounted", stdout="/dev/sdb ext4 rw\n")
    add("findmnt", "", stdout="")
    add("mount", "/mnt/data", set_flag="mounted")
    add("mkfs.ext4", "", set_flag="formatted")
    add("swapon", "", stdout="")
    add("iptables", "^-S INPUT$", stdout=INPUT_RULES)
    add("iptables", "^-S FORWARD$", stdout=FORWARD_RULES)
    if docker_installed:
        add("iptables", "^-S$", stdout="-N DOCKER\n-N DOCKER-USER\n")
    add("iptables", "^-S$", stdout="")
    add("iptables", "^-t nat -S$", stdout="")
    add("iptables", "^-C INPUT", rc=1)
    add("iptables", "^-L INPUT --line-numbers -n$", stdout=INPUT_LIST)
    add("netfilter-persistent", "^save$", write={str(root / "etc/iptables/rules.v4"): (
        "-A INPUT -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT\n"
        "-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT\n"
        "-A INPUT -p udp -m state --state NEW -m udp --dport 443 -j ACCEPT\n")})
    add("dpkg", "--print-architecture", stdout="arm64\n")
    add("dpkg", "^-s ", rc=1)
    add("apt-get", "install .*docker-ce", create_launcher="docker",
        require_files=[str(root / "etc/docker/daemon.json"), str(root / "etc/containerd/config.toml"),
                       str(root / "etc/iptables/rules.v4")])
    add("curl", "", touch="-o")
    add("docker", r"^info --format \{\{\.DockerRootDir\}\}", stdout="/mnt/data/docker\n")
    add("docker", r"^info --format \{\{\.Driver\}\}", stdout='overlayfs [["driver-type","io.containerd.snapshotter.v1"]]\n')
    add("containerd", "config dump", stdout='version = 3\nroot = "/mnt/data/containerd"\n')
    add("id", "^nameleaf$", if_flag="user")
    add("id", "^nameleaf$", rc=1)
    add("id", "^-nG nameleaf$", stdout="nameleaf\n")
    add("useradd", "", set_flag="user")
    add("sshd", "^-T$", stdout="passwordauthentication no\nkbdinteractiveauthentication no\n"
                               "permitrootlogin no\npubkeyauthentication yes\nport 22\n")
    return root


def run_setup(h: Harness, root: Path, *args: str):
    return h.run(SERVER / "setup.sh", *args,
                 extra_env={"NAMELEAF_SETUP_ROOT": str(root), "NAMELEAF_SETUP_RETRY_SLEEP": "0"})


MUTATING = ("mkfs", "apt-get", "iptables -I", "netfilter-persistent", "useradd", "usermod",
            "systemctl", "mount", "chown", "debconf")


def test_setup_preflight_passes_on_the_predicted_server_and_changes_nothing(h):
    root = setup_server(h)
    r = run_setup(h, root, "preflight")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MISMATCH" not in r.stdout
    assert "paravirtualized" in r.stdout
    assert not [c for c in h.calls() if c.startswith(MUTATING)]


@pytest.mark.parametrize("change,label", [
    (lambda h, root: h.config["rules"].insert(0, {"cmd": "uname", "match": "-m", "stdout": "x86_64\n"}), "architecture"),
    (lambda h, root: (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="22.04"\n'), "OS"),
    (lambda h, root: h.config["rules"].insert(0, {"cmd": "lsblk", "rc": 32}), "data device"),
    (lambda h, root: h.config["rules"].insert(0, {"cmd": "lsblk", "stdout": "53687091200 \n"}), "data device size"),
    (lambda h, root: h.config["rules"].insert(0, {"cmd": "swapon", "stdout": "/swapfile file 2G 0B -2\n"}), "swap"),
    (lambda h, root: h.config["rules"].insert(0, {"cmd": "iptables", "match": "^-S FORWARD$",
                                                  "stdout": "-P FORWARD ACCEPT\n"}), "iptables FORWARD"),
])
def test_setup_preflight_stops_on_each_mismatch(h, change, label):
    root = setup_server(h)
    change(h, root)
    r = run_setup(h, root, "preflight")
    assert r.returncode == 2
    assert f"MISMATCH  {label}" in r.stdout
    assert not [c for c in h.calls() if c.startswith(MUTATING)]


def test_setup_refuses_a_disk_with_a_foreign_signature(h):
    root = setup_server(h, disk="foreign")
    r = run_setup(h, root, "apply", "--format-data-device", "/dev/oracleoci/oraclevdb")
    assert r.returncode == 2
    assert "MISMATCH  data device signatures" in r.stdout
    assert not [c for c in h.calls() if c.startswith(MUTATING)]


def test_setup_refuses_a_signature_found_by_wipefs(h):
    root = setup_server(h)
    h.config["rules"].insert(0, {"cmd": "wipefs", "stdout": "DEVICE OFFSET TYPE UUID LABEL\nsdb 0x0 gpt\n"})
    r = run_setup(h, root, "apply", "--format-data-device", "/dev/oracleoci/oraclevdb")
    assert r.returncode == 2
    assert not any(c.startswith("mkfs") for c in h.calls())


def test_setup_will_not_format_without_the_explicit_flag(h):
    root = setup_server(h)
    r = run_setup(h, root, "apply")
    assert r.returncode == 2
    assert "--format-data-device /dev/oracleoci/oraclevdb" in r.stdout  # apply tees stderr into its log
    assert not any(c.startswith("mkfs") for c in h.calls())


def test_setup_will_not_format_a_different_device(h):
    root = setup_server(h)
    r = run_setup(h, root, "apply", "--format-data-device", "/dev/sdc")
    assert r.returncode == 2
    assert not any(c.startswith("mkfs") for c in h.calls())


def test_setup_apply_end_to_end_in_order(h):
    root = setup_server(h)
    r = run_setup(h, root, "apply", "--format-data-device", "/dev/oracleoci/oraclevdb")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = h.calls()

    def first(prefix: str) -> int:
        return next(i for i, c in enumerate(calls) if c.startswith(prefix))

    assert [c for c in calls if c.startswith("mkfs")] == ["mkfs.ext4 -L nameleaf-data /dev/oracleoci/oraclevdb"]
    assert "UUID=1111-uuid /mnt/data ext4 defaults,nofail 0 2" in (root / "etc/fstab").read_text()
    inserts = [c for c in calls if c.startswith("iptables -I INPUT 5")]
    assert inserts == ["iptables -I INPUT 5 -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT",
                       "iptables -I INPUT 5 -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT",
                       "iptables -I INPUT 5 -p udp -m state --state NEW -m udp --dport 443 -j ACCEPT"]
    # data volume -> firewall saved -> Docker installed (with its config already written).
    assert first("mount /mnt/data") < first("netfilter-persistent save") < first("apt-get install -y -o")
    assert json.loads((root / "etc/docker/daemon.json").read_text()) == {"data-root": "/mnt/data/docker"}
    assert 'root = "/mnt/data/containerd"' in (root / "etc/containerd/config.toml").read_text()
    assert "containerd image store under /mnt/data/containerd" in r.stdout
    assert first("sshd -t") < first("systemctl reload ssh")
    conf = (root / "etc/ssh/sshd_config.d/00-nameleaf.conf").read_text()
    assert "PasswordAuthentication no" in conf and "PermitRootLogin no" in conf
    assert (root / "home/nameleaf/.ssh/authorized_keys").read_text() == "ssh-ed25519 AAAAfake nameleaf-prod\n"
    assert not any(c.startswith("usermod") and "sudo" in c for c in calls)
    assert "usermod -aG docker nameleaf" in calls
    for d in ("srv/nameleaf/releases", "srv/nameleaf/publish", "srv/nameleaf/state", "srv/nameleaf/env",
              "mnt/data/backups/live"):
        assert (root / d).is_dir(), d
    assert oct((root / "srv/nameleaf/env").stat().st_mode & 0o777) == "0o700"
    cron = (root / "etc/cron.d/nameleaf-backup").read_text()
    assert "nameleaf /srv/nameleaf/repo/deploy/server/backup_live.sh --keep-local-days 7 --keep-remote-days 30" in cron
    assert (root / "usr/local/bin/nameleaf-compose").is_symlink()
    assert not any("swap" in c and not c.startswith("swapon --show") for c in calls)
    assert "second terminal" in r.stdout.lower() or "SECOND terminal" in r.stdout
    assert "Security list" in r.stdout or "security list" in r.stdout


def test_setup_never_saves_iptables_once_docker_chains_exist(h):
    root = setup_server(h, docker_installed=True, disk="ours")
    h.config["rules"].insert(0, {"cmd": "iptables", "match": "^-C INPUT", "rc": 0})
    h.config["rules"].insert(0, {"cmd": "id", "match": "^nameleaf$"})
    h.config["rules"].insert(0, {"cmd": "id", "match": "^-nG nameleaf$", "stdout": "nameleaf docker\n"})
    r = run_setup(h, root, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(c.startswith("netfilter-persistent") for c in h.calls())
    assert not any(c.startswith("iptables -I") for c in h.calls())
    assert not any(c.startswith("mkfs") for c in h.calls())
    assert not any("docker-ce" in c for c in h.calls())


def test_setup_with_docker_chains_and_missing_input_rules_stops_without_saving(h):
    root = setup_server(h, docker_installed=True, disk="ours")
    r = run_setup(h, root, "apply")
    assert r.returncode == 2
    assert not any(c.startswith(("netfilter-persistent", "iptables -I")) for c in h.calls())


def test_setup_refuses_when_sshd_rejects_the_config(h):
    root = setup_server(h)
    h.config["rules"].insert(0, {"cmd": "sshd", "match": "^-t$", "rc": 255})
    r = run_setup(h, root, "apply", "--format-data-device", "/dev/oracleoci/oraclevdb")
    assert r.returncode == 2
    assert not (root / "etc/ssh/sshd_config.d/00-nameleaf.conf").exists()
    assert "systemctl reload ssh" not in h.calls()


def test_setup_stops_if_images_would_land_on_the_boot_volume(h):
    root = setup_server(h)
    h.config["rules"].insert(0, {"cmd": "containerd", "match": "config dump",
                                 "stdout": 'root = "/var/lib/containerd"\n'})
    r = run_setup(h, root, "apply", "--format-data-device", "/dev/oracleoci/oraclevdb")
    assert r.returncode == 2
    assert "whose root is '/var/lib/containerd'" in r.stdout  # apply tees stderr into its log
