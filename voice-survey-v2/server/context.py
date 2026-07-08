"""
context.py — Per-session state manager (in-memory, auto-expiring, 30-min TTL).
"""
import time
from dataclasses import dataclass, field

SESSION_TTL = 30 * 60

@dataclass
class Session:
    session_id: str
    current_node: str = "Q1"
    history: list = field(default_factory=list)
    sentiment_log: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()

    def add_turn(self, role: str, content: str, sentiment: str = "neutral"):
        self.history.append({
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "sentiment": sentiment,
        })
        if sentiment != "neutral":
            self.sentiment_log.append(sentiment)
        self.touch()

    @property
    def chat_history(self) -> list[dict]:
        return [{"role": m["role"], "content": m["content"]} for m in self.history]


_store: dict[str, Session] = {}


def get_or_create(session_id: str) -> Session:
    _purge_expired()
    if session_id not in _store:
        _store[session_id] = Session(session_id=session_id)
    s = _store[session_id]
    s.touch()
    return s


def reset(session_id: str) -> Session:
    _store[session_id] = Session(session_id=session_id)
    return _store[session_id]


def _purge_expired():
    now = time.time()
    expired = [k for k, v in _store.items() if now - v.last_active > SESSION_TTL]
    for k in expired:
        del _store[k]