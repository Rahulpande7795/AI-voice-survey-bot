"""context.py — Per-session state with pre-emptive TTS fields."""
import time
from dataclasses import dataclass, field
from typing import Optional

SESSION_TTL = 30 * 60

@dataclass
class Session:
    session_id:    str
    current_node:  str   = "INTRO"
    history:       list  = field(default_factory=list)
    sentiment_log: list  = field(default_factory=list)
    data_capture:  dict  = field(default_factory=dict)
    preempt_audio: Optional[bytes] = None
    preempt_node:  Optional[str]   = None
    created_at:    float = field(default_factory=time.time)
    last_active:   float = field(default_factory=time.time)

    def touch(self): self.last_active = time.time()

    def add_turn(self, role: str, content: str, sentiment: str = "neutral"):
        self.history.append({"role": role, "content": content,
                             "timestamp": time.time(), "sentiment": sentiment})
        if sentiment != "neutral": self.sentiment_log.append(sentiment)
        self.touch()

    def merge_capture(self, d: dict): self.data_capture.update(d)

    @property
    def chat_history(self) -> list[dict]:
        return [{"role": m["role"], "content": m["content"]} for m in self.history]


_store: dict[str, Session] = {}

def get_or_create(sid: str) -> Session:
    _purge()
    if sid not in _store: _store[sid] = Session(session_id=sid)
    s = _store[sid]; s.touch(); return s

def reset(sid: str) -> Session:
    _store[sid] = Session(session_id=sid); return _store[sid]

def _purge():
    now = time.time()
    for k in [k for k,v in _store.items() if now - v.last_active > SESSION_TTL]:
        del _store[k]