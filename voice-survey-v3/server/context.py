"""
context.py — Per-session state (extended for V3 data_capture).
"""
import time
from dataclasses import dataclass, field

SESSION_TTL = 30 * 60

@dataclass
class Session:
    session_id: str
    current_node: str = "INTRO"
    history: list = field(default_factory=list)
    sentiment_log: list = field(default_factory=list)
    data_capture: dict = field(default_factory=dict)   # ← V3: structured extraction
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()

    def add_turn(self, role: str, content: str, sentiment: str = "neutral"):
        self.history.append({
            "role": role, "content": content,
            "timestamp": time.time(), "sentiment": sentiment,
        })
        if sentiment != "neutral":
            self.sentiment_log.append(sentiment)
        self.touch()

    def merge_capture(self, extracted: dict):
        """Merge extracted structured data into data_capture."""
        self.data_capture.update(extracted)

    @property
    def chat_history(self) -> list[dict]:
        return [{"role": m["role"], "content": m["content"]} for m in self.history]


_store: dict[str, Session] = {}

def get_or_create(session_id: str) -> Session:
    _purge_expired()
    if session_id not in _store:
        _store[session_id] = Session(session_id=session_id)
    s = _store[session_id]; s.touch(); return s

def reset(session_id: str) -> Session:
    _store[session_id] = Session(session_id=session_id)
    return _store[session_id]

def _purge_expired():
    now = time.time()
    for k in [k for k,v in _store.items() if now - v.last_active > SESSION_TTL]:
        del _store[k]