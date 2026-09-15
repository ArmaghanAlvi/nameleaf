"""Error reporting (publishing Stage 3g). A no-op unless SENTRY_DSN is set."""
from __future__ import annotations

import json
import re
from typing import Any

from app.config import Settings

# services/root_llm.py sends the Gemini key as a ?key= query parameter, and
# httpx error messages include the full URL. Any event or breadcrumb that
# captured that URL would ship the key to Sentry, so every key= value is
# scrubbed before anything leaves the process.
_KEY_PARAM = re.compile(r"(?i)((?:\?|&|%3F|%26)key=)[^&\s\"'#\\]+")


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(_KEY_PARAM.sub(r"\1[scrubbed]", json.dumps(event, default=str)))
    except Exception:
        return None  # drop the event rather than risk sending an unscrubbed one


def init_sentry(cfg: Settings) -> bool:
    if not cfg.sentry_dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=cfg.sentry_dsn,
        environment=cfg.app_env,
        send_default_pii=False,
        traces_sample_rate=0.0,
        before_send=scrub_event,
        before_breadcrumb=scrub_event,
    )
    return True
