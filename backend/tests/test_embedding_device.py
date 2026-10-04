"""Stage 1a: EMBEDDING_DEVICE / TORCH_NUM_THREADS resolution in get_model().

The model is never loaded: SentenceTransformer is replaced by a fake, so CI
never downloads it. get_model is lru_cached, so every test clears the cache
before and after -- a fake left in the cache would leak into later tests.
"""
from __future__ import annotations

import pytest
import torch
from pydantic import ValidationError

import app.config as config
import app.services.embedding_provider as ep
from app.config import Settings


class _FakeModel:
    def __init__(self, name: str, device: str) -> None:
        self.name = name
        self.device = torch.device(device)


@pytest.fixture
def calls(monkeypatch):
    log: list[tuple] = []

    def fake_st(name, device, revision=None):
        assert revision == ep.DEFAULT_EMBEDDING_MODEL_REVISION
        log.append(("load", device))
        return _FakeModel(name, device)

    monkeypatch.setattr(ep, "SentenceTransformer", fake_st)
    monkeypatch.setattr(torch, "set_num_threads",
                        lambda n: log.append(("threads", n)))
    ep.get_model.cache_clear()
    yield log
    ep.get_model.cache_clear()


def _configure(monkeypatch, *, device=None, threads=None, mps=False, cuda=False):
    cfg = Settings(_env_file=None,  # type: ignore[call-arg]
                   database_url="postgresql+psycopg://test@localhost/test",
                   embedding_device=device, torch_num_threads=threads)
    monkeypatch.setattr(config, "settings", cfg)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)


@pytest.mark.parametrize("mps,cuda,expected", [
    (True, True, "mps"),
    (False, True, "cuda"),
    (False, False, "cpu"),
])
def test_unset_keeps_auto_detection_order(monkeypatch, calls, mps, cuda, expected):
    _configure(monkeypatch, mps=mps, cuda=cuda)
    assert ep.get_model().device.type == expected
    assert calls == [("load", expected)]


def test_unset_threads_never_touches_torch(monkeypatch, calls):
    _configure(monkeypatch, mps=True)
    ep.get_model()
    assert not [c for c in calls if c[0] == "threads"]


def test_explicit_cpu_wins_over_available_mps(monkeypatch, calls):
    _configure(monkeypatch, device="cpu", mps=True, cuda=True)
    assert ep.get_model().device.type == "cpu"


@pytest.mark.parametrize("device", ["mps", "cuda"])
def test_unavailable_explicit_device_fails_loudly(monkeypatch, calls, device):
    _configure(monkeypatch, device=device, mps=False, cuda=False)
    with pytest.raises(RuntimeError, match="EMBEDDING_DEVICE"):
        ep.get_model()
    assert calls == []


def test_available_explicit_device_is_used(monkeypatch, calls):
    _configure(monkeypatch, device="mps", mps=True)
    assert ep.get_model().device.type == "mps"


def test_threads_are_set_before_the_model_loads(monkeypatch, calls):
    _configure(monkeypatch, device="cpu", threads=2)
    ep.get_model()
    assert calls == [("threads", 2), ("load", "cpu")]


def test_model_loads_once(monkeypatch, calls):
    _configure(monkeypatch, device="cpu", threads=2)
    assert ep.get_model() is ep.get_model()
    assert calls == [("threads", 2), ("load", "cpu")]


@pytest.mark.parametrize("bad", [{"torch_num_threads": 0},
                                 {"embedding_device": "tpu"}])
def test_invalid_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None,  # type: ignore[call-arg]
                 database_url="postgresql+psycopg://test@localhost/test", **bad)


def test_parity_probe_encodes_texts_verbatim():
    from scripts.eval.vector_parity_probe import encode_inputs
    texts = ["query: verbatim check", "query: second"]
    assert encode_inputs(texts) == texts


def test_dockerfile_pins_the_same_model_revision():
    """B-2.4: get_model() and the image's build-time download must pin one
    snapshot. The builder stage has no app/, so the Dockerfile carries its
    own ARG; this keeps the two from drifting."""
    import re
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile.prod"
    m = re.search(r"^ARG EMBEDDING_MODEL_REVISION=(\S+)$",
                  dockerfile.read_text(), re.M)
    assert m, "Dockerfile.prod must declare ARG EMBEDDING_MODEL_REVISION"
    assert m.group(1) == ep.DEFAULT_EMBEDDING_MODEL_REVISION
    assert ep.DEFAULT_EMBEDDING_MODEL_REVISION == (
        "d128750597153bb5987e10b1c3493a34e5a4502a")
