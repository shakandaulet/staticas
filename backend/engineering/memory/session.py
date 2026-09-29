"""
Conversation state: what the assistant remembers between messages.

WHY THIS EXISTS AT ALL
----------------------
"Now make it 800 N instead" is the second thing every user says, and it
is meaningless without the first. A stateless assistant answers it by
rebuilding a plan from that sentence alone -- which produces a study
with a force, no restraint, no material and no mesh, because the user
did not repeat those.

So the current plan is kept and handed back to the planner with every
follow-up, and the planner is told to return the whole updated plan
rather than a delta. Reconstructing a plan from a series of deltas is
where an assistant's idea of the model and the user's quietly diverge.

STORED ON DISK, NOT IN A DICTIONARY
-----------------------------------
A restart during a lab session should not lose everyone's work, and a
session that can be read as a file is a session that can be looked at
when someone says "it did something strange".
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import config

MAX_HISTORY = 40


@dataclass
class Session:
    id: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)
    plan: dict | None = None            # the last Plan, as JSON
    resolved: list[dict] = field(default_factory=list)
    last_code: str = ""
    last_results: dict = field(default_factory=dict)
    domain: str = ""

    # ---- history ----
    def add(self, role: str, text: str, **extra) -> None:
        self.history.append({"role": role, "text": text, "at": time.time(),
                             **extra})
        # Trimmed rather than unbounded: the planner only ever reads the
        # last few turns, and an unbounded file is a slow read on every
        # message for content nobody looks at.
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self.updated = time.time()

    def recent(self, n: int = 6) -> list[dict]:
        return [{"role": h["role"], "text": h["text"]} for h in self.history[-n:]]

    # ---- persistence ----
    @property
    def path(self) -> Path:
        return config.SESSIONS_DIR / f"{self.id}.json"

    def save(self) -> None:
        self.path.write_text(
            json.dumps(asdict(self), indent=1, ensure_ascii=False),
            encoding="utf-8")


def _safe_id(session_id: str) -> str:
    """Session ids arrive from the browser, so they are not trusted as
    file names. Keep only what a uuid4 hex string can contain."""
    clean = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return clean[:64]


def load(session_id: str | None) -> Session:
    """Fetch a session, or start one. Never fails on a bad id."""
    sid = _safe_id(session_id or "")
    if sid:
        path = config.SESSIONS_DIR / f"{sid}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return Session(**data)
            except (json.JSONDecodeError, TypeError):
                # A corrupted session should cost the user their history,
                # not their next message.
                pass
    return Session(id=sid or uuid.uuid4().hex[:16])


def recent_sessions(limit: int = 20) -> list[dict]:
    out = []
    paths = sorted(config.SESSIONS_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        first = next((h["text"] for h in data.get("history", [])
                      if h.get("role") == "user"), "")
        out.append({"id": data.get("id"), "updated": data.get("updated"),
                    "domain": data.get("domain", ""), "opening": first[:90]})
    return out
