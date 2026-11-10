"""
Fake binaries for tests/test_deploy_scripts.py (E1). Not a test module.

Every fake on the tests' PATH is a two-line shell launcher that runs this file
with the binary's name: `exec <python> deploy_fakes.py docker "$@"`. Each call
is appended to $FAKE_LOG as one line ("docker image inspect ..."), so a test
can assert what ran, and in what order.

Behaviour comes from $FAKE_CONFIG (JSON, written by the test) and $FAKE_STATE
(a directory the fakes keep state in, e.g. which containers exist):

  ssh     strips -o options, checks the alias, and runs the rest of the
          command LOCALLY with `bash -c` (stdin passed through). Nothing ever
          leaves the machine: that is the whole point of the fake.
  docker  a small model of the engine: images (config "images"), containers
          created by `compose up`, compose `ps`/`config --hash`, inspect,
          image ls/rm/tag, save/load, exec, run, info.
  git     answers from config "git"; `archive` streams a tar of the real
          repo's docker-compose.prod.yml and deploy/ (the server scripts).
  others  (gh, rclone, curl, flock, python3, and setup.sh's system tools)
          answer from config "rules": the first rule whose "cmd" equals the
          binary and whose regex "match" finds the joined arguments wins;
          {"stdout", "rc"}; default: empty stdout, rc 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _config() -> dict:
    return json.loads(Path(os.environ["FAKE_CONFIG"]).read_text())


def _log(name: str, args: list[str]) -> None:
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(" ".join([name, *args]) + "\n")


def _state() -> Path:
    p = Path(os.environ["FAKE_STATE"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load(name: str, default):
    f = _state() / f"{name}.json"
    return json.loads(f.read_text()) if f.exists() else default


def _save(name: str, value) -> None:
    (_state() / f"{name}.json").write_text(json.dumps(value, indent=1))


def _flag(name: str) -> Path:
    return _state() / f"flag-{name}"


def _rules(name: str, args: list[str]) -> int:
    """Rule keys: cmd, match (regex on the joined args), stdout, rc, and the
    side effects consume_stdin, touch (write the file named after this
    option), set_flag / if_flag / unless_flag (state flags for multi-step
    flows), write {path: text}, create_launcher (make another fake exist on
    PATH, e.g. docker after its apt install), require_files (rc 1 unless they
    all exist, to check an ordering)."""
    joined = " ".join(args)
    for rule in _config().get("rules", []):
        if rule["cmd"] != name or not re.search(rule.get("match", ""), joined):
            continue
        if rule.get("if_flag") and not _flag(rule["if_flag"]).exists():
            continue
        if rule.get("unless_flag") and _flag(rule["unless_flag"]).exists():
            continue
        if rule.get("consume_stdin"):
            sys.stdin.buffer.read()
        missing = [p for p in rule.get("require_files", []) if not Path(p).exists()]
        if missing:
            print(f"fake {name}: required files missing: {missing}", file=sys.stderr)
            return 1
        sys.stdout.write(rule.get("stdout", ""))
        if rule.get("touch"):  # e.g. curl -o FILE
            Path(args[args.index(rule["touch"]) + 1]).write_text("fake\n")
        if rule.get("set_flag"):
            _flag(rule["set_flag"]).write_text("1")
        for path, text in rule.get("write", {}).items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text)
        if rule.get("create_launcher"):
            make_launcher(Path(os.environ["FAKE_BIN"]), rule["create_launcher"])
        return int(rule.get("rc", 0))
    return 0


def make_launcher(bin_dir: Path, name: str) -> Path:
    path = bin_dir / name
    # -S -E: skip site and env setup; the fakes need only the stdlib.
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S -E "{Path(__file__).resolve()}" {name} "$@"\n')
    path.chmod(0o755)
    return path


# --- ssh ------------------------------------------------------------------------

def fake_ssh(args: list[str]) -> int:
    i = 0
    while i < len(args) and args[i] == "-o":
        i += 2
    alias, rest = args[i], args[i + 1:]
    if alias != _config().get("alias", "example-alias"):
        print(f"fake ssh: unknown alias {alias!r}", file=sys.stderr)
        return 255
    if _config().get("ssh_unreachable"):
        return 255
    import subprocess
    # The "server" keeps its own docker state, apart from the Mac's.
    env = dict(os.environ, FAKE_STATE=str(Path(os.environ["FAKE_STATE"]) / "remote"), FAKE_REMOTE="1")
    return subprocess.call(["bash", "-c", " ".join(rest)], env=env)


# --- git --------------------------------------------------------------------------

def _archive() -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(REPO / "docker-compose.prod.yml", arcname="docker-compose.prod.yml")
        for path in sorted((REPO / "deploy").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                tar.add(path, arcname=str(path.relative_to(REPO)))
        for rel in _config().get("git", {}).get("extra_files", {}):
            data = _config()["git"]["extra_files"][rel].encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fake_git(args: list[str]) -> int:
    g = _config().get("git", {})
    if args[:1] == ["-C"]:
        args = args[2:]
    sha = g.get("sha", "0123456789abcdef0123456789abcdef01234567")
    joined = " ".join(args)
    if joined == "rev-parse --abbrev-ref HEAD":
        print(g.get("branch", "main"))
    elif joined == "status --porcelain":
        sys.stdout.write(g.get("porcelain", ""))
    elif joined == "rev-parse HEAD":
        print(sha)
    elif joined == "rev-parse --short=12 HEAD":
        print(sha[:12])
    elif joined == "rev-parse --show-toplevel":
        print(REPO)
    elif args[:1] == ["ls-remote"]:
        print(f"{g.get('origin', sha)}\trefs/heads/main")
    elif args[:1] == ["archive"]:
        sys.stdout.buffer.write(_archive())
    else:
        print(f"fake git: unhandled {joined!r}", file=sys.stderr)
        return 1
    return 0


# --- docker -------------------------------------------------------------------------

def _images() -> dict:
    """Config images (a test may add more between runs) overlaid with what
    the fake engine changed: loads, tags, builds, minus removals."""
    key = "remote_images" if os.environ.get("FAKE_REMOTE") else "images"
    images = dict(_config().get(key, {}))
    images.update(_load("images", {}))
    for ref in _load("removed", []):
        images.pop(ref, None)
    return images


def _containers() -> dict:
    return _load("containers", _config().get("containers", {}))


def _fingerprint(image: dict) -> str:
    return image.get("fingerprint", f"[layers-of-{image['id']}] cfg")


def _render(fmt: str, ref: str, image: dict) -> str:
    if "RootFS.Layers" in fmt:
        return _fingerprint(image)
    if "org.nameleaf.site_url" in fmt:
        return image.get("site_url", "<no value>")
    return image["id"]


def _env_tag(args: list[str]) -> str:
    if os.environ.get("IMAGE_TAG"):
        return os.environ["IMAGE_TAG"]
    tag = "latest"
    for i, a in enumerate(args):
        if a == "--env-file":
            text = Path(args[i + 1]).read_text() if Path(args[i + 1]).exists() else ""
            for line in text.splitlines():
                if line.startswith("IMAGE_TAG="):
                    tag = line.split("=", 1)[1]
    return tag


def _hash(svc: str, tag: str) -> str:
    return f"h-{svc}-{tag}-{_config().get('config_version', 1)}"


def _compose(args: list[str]) -> int:
    i = 0
    while i < len(args) and args[i] in ("-f", "--env-file"):
        i += 2
    sub, rest = args[i], args[i + 1:]
    tag = _env_tag(args)
    containers = _containers()
    if sub == "ps":
        svc = [a for a in rest if not a.startswith("-")]
        if svc and svc[0] in containers:
            print(containers[svc[0]]["id"])
        return 0
    if sub == "config" and rest[:1] == ["--hash"]:
        print(f"{rest[1]} {_hash(rest[1], tag)}")
        return 0
    if sub == "up":
        svcs = [a for a in rest if not a.startswith("-") and a not in ("never",)]
        images = _images()
        counter = _load("counter", 0)
        for svc in svcs:
            counter += 1
            ref = {"db": "pgvector/pgvector:pg17", "caddy": "caddy:2.11.4"}.get(svc, f"nameleaf-{svc}:{tag}")
            image_id = images.get(ref, {}).get("id", f"sha256:{svc}-pinned")
            mounts = [f"{os.getcwd()}/deploy/Caddyfile"] if svc == "caddy" else []
            containers[svc] = {"id": f"cid-{svc}-{counter}", "ref": ref, "image_id": image_id,
                               "hash": _hash(svc, tag), "health": _config().get("health", {}).get(svc, "healthy"),
                               "has_health": svc in ("db", "backend"), "mounts": mounts}
        _save("counter", counter)
        _save("containers", containers)
        return 0
    print(f"fake docker compose: unhandled {sub!r}", file=sys.stderr)
    return 1


def _by_id(cid: str):
    for svc, c in _containers().items():
        if c["id"] == cid:
            return svc, c
    return None, None


def fake_docker(args: list[str]) -> int:
    if args[:1] == ["compose"]:
        return _compose(args[1:])
    images = _images()
    if args[:2] == ["image", "inspect"]:
        fmt = args[args.index("--format") + 1] if "--format" in args else "{{.Id}}"
        ref = args[-1]
        found = images.get(ref) or next((v for v in images.values() if v["id"] == ref), None)
        if not found:
            print(f"Error: No such image: {ref}", file=sys.stderr)
            return 1
        print(_render(fmt, ref, found))
        return 0
    if args[:2] == ["image", "ls"]:
        fmt, repo = args[args.index("--format") + 1], args[-1]
        if repo.startswith("--") or repo == fmt:
            return 0
        for ref in images:
            name, tag = ref.rsplit(":", 1)
            if name == repo:
                print(fmt.replace("{{.Repository}}", name).replace("{{.Tag}}", tag))
        return 0
    if args[:2] == ["image", "rm"]:
        ref = args[2]
        if ref not in images:
            return 1
        image_id = images[ref]["id"]
        others = [r for r, v in images.items() if v["id"] == image_id and r != ref]
        used = any(c["image_id"] == image_id for c in _containers().values())
        if used and not others:
            print(f"Error: image is being used by a container: {ref}", file=sys.stderr)
            return 1
        del images[ref]
        _save("images", images)
        _save("removed", _load("removed", []) + [ref])
        return 0
    if args[:1] == ["tag"]:
        images[args[2]] = dict(images[args[1]])
        _save("images", images)
        return 0
    if args[:1] == ["inspect"]:
        fmt, cid = args[args.index("--format") + 1], args[-1]
        _svc, c = _by_id(cid)
        if c is None:
            return 1
        if fmt == "{{.Image}}":
            print(c["image_id"])
        elif fmt == "{{.Config.Image}}":
            print(c["ref"])
        elif "config-hash" in fmt:
            print(c["hash"])
        elif fmt.startswith("{{if .State.Health}}h"):
            print("h" if c["has_health"] else "")
        elif "State" in fmt:
            print(c["health"] if c["has_health"] else "running")
        elif ".Mounts" in fmt:
            for m in c["mounts"]:
                print(m)
        else:
            return 1
        return 0
    if args[:1] == ["ps"]:
        for c in _containers().values():
            print(c["id"])
        return 0
    if args[:1] == ["load"]:
        sys.stdin.buffer.read()
        n = _load("loads", 0)
        adds = _config().get("load_adds", [])
        images.update(adds[n] if n < len(adds) else {})
        _save("loads", n + 1)
        _save("images", images)
        print("Loaded image")
        return 0
    if args[:1] == ["save"]:
        sys.stdout.buffer.write(b"fake-image-tarball")
        return 0
    if args[:1] == ["build"]:
        tag = args[args.index("-t") + 1]
        site = next((a.split("=", 1)[1] for a in args if a.startswith("org.nameleaf.site_url=")), None)
        images[tag] = {"id": f"sha256:built-{tag}"}
        if site:
            images[tag]["site_url"] = site
        _save("images", images)
        return 0
    return _rules("docker", args)


def main() -> int:
    name, args = sys.argv[1], sys.argv[2:]
    _log(name, args)
    if name == "ssh":
        return fake_ssh(args)
    if name == "git":
        return fake_git(args)
    if name == "docker":
        return fake_docker(args)
    return _rules(name, args)


if __name__ == "__main__":
    sys.exit(main())
