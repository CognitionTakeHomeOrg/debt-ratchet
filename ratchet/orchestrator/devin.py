"""Devin API v3 client.

Thin on purpose. The interesting decisions in this system are not in the HTTP
layer -- they are in what goes into the prompt, what counts as done, and who is
trusted to say so. Those live in prompt.py and verify.py respectively.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


def load_env(root: Path) -> dict:
    env = {}
    envfile = root / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **os.environ}


# The verdict Devin must return. Prose would have to be scraped and guessed at;
# a schema makes the session's own claim machine-readable -- which matters
# because the orchestrator then goes and checks that claim independently.
STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "gate_closed": {
            "type": "boolean",
            "description": "True only if the verification command printed 0 for this area.",
        },
        "findings_fixed": {"type": "integer"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "behavior_change": {
            "type": "boolean",
            "description": "True if the fix changes runtime behaviour in any way a user could observe.",
        },
        "suppressions_added": {
            "type": "boolean",
            "description": "True if any lint-disable, ts-ignore, or config exclusion was added.",
        },
        "rationale": {
            "type": "string",
            "description": "Why this fix is correct. If blocked, why it cannot be fixed correctly.",
        },
        "blocked_reason": {
            "type": ["string", "null"],
            "description": "Non-null only if the task could not be completed correctly.",
        },
    },
    "required": ["gate_closed", "files_changed", "behavior_change", "suppressions_added", "rationale"],
}


@dataclass
class Session:
    session_id: str
    url: str
    is_new: bool


class DevinClient:
    def __init__(self, api_key: str, org_id: str, base: str = "https://api.devin.ai/v3"):
        self.api_key = api_key
        self.org_id = org_id
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}/organizations/{self.org_id}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:600]}") from None

    def create_session(
        self, prompt: str, title: str, tags: list[str], max_acu: int, idem_key: str | None = None
    ) -> Session:
        body = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            # Native per-session budget cap. The single most likely way to burn
            # an ACU allocation by accident is an agent looping on a task it
            # cannot finish, and this is the only control that stops it from
            # inside the platform rather than from our polling loop.
            "max_acu_limit": max_acu,
            # GitHub retries webhook deliveries. Without this, one retry is one
            # duplicate session and one duplicate pull request.
            "idempotent": True,
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        }
        if idem_key:
            body["idempotency_key"] = idem_key
        r = self._req("POST", "/sessions", body)
        return Session(r["session_id"], r.get("url", ""), r.get("is_new_session", True))

    def get_session(self, session_id: str) -> dict:
        return self._req("GET", f"/sessions/{session_id}")

    def get_messages(self, session_id: str) -> list[dict]:
        """The session's live narration.

        Undocumented in the v3 overview but present: `/messages` returns the
        conversation as `{event_id, source, message, created_at}` items, where
        `source` is `user` (our prompt) or `devin` (what it is doing right now).

        This is the only progress signal finer than `status_detail`, which only
        ever says "working". Without it, an issue sits silent for ten to twenty
        minutes and a reviewer cannot tell a working session from a hung one.
        """
        out, cursor = [], None
        while True:
            path = f"/sessions/{session_id}/messages"
            if cursor:
                path += f"?cursor={cursor}"
            page = self._req("GET", path)
            out.extend(page.get("items") or [])
            if not page.get("has_next_page"):
                return out
            cursor = page.get("end_cursor")
            if not cursor:
                return out

    def list_sessions(self, limit: int = 20) -> dict:
        return self._req("GET", f"/sessions?limit={limit}")
