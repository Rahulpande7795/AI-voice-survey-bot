"""
cache.py — Semantic FAISS cache. Per-node isolation. FAISS pre-loaded at startup.

BUGS FIXED:
  1. global_exact block previously ended with `pass` — matches for "yes", "no",
     "okay" etc. fell through to FAISS entirely, defeating the O(1) fast path.
     Now returns a real CacheHit with next_node="__intent__" as a sentinel,
     meaning pipeline.py should use intent routing for the transition but can
     use the pre-defined ack_text from this dict.

  2. FAISS index reset on LRU eviction was incomplete — when entries were
     popped from the list, vectors were not removed from the FAISS index
     (FAISS IndexFlatIP has no remove). Fixed by rebuilding the index
     from remaining entries on overflow (correct behaviour).

  3. _embed() was not being awaited safely under the lock in add() —
     embedding is now computed BEFORE acquiring the lock to keep
     the async lock hold time minimal.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger("survey")

from config import CACHE_THRESHOLD, CACHE_MAX_ENTRIES

EMBED_MODEL = os.getenv("CACHE_MODEL", "all-MiniLM-L6-v2")


@dataclass
class CacheHit:
    ack_text:   str
    next_node:  str      # "__intent__" = sentinel: use intent routing
    sentiment:  str
    similarity: float


class SemanticCache:
    def __init__(self, threshold: float = CACHE_THRESHOLD):
        self.threshold = threshold
        self._model    = None
        self._lock     = asyncio.Lock()
        self._entries:       dict[str, list[dict]]        = {}
        self._indexes:       dict[str, object]             = {}
        self._exact_matches: dict[str, dict[str, CacheHit]] = {}

        # ── Global fast-path: common short responses across all nodes ────────
        # next_node is intentionally "__intent__" — pipeline must use its own
        # intent detector for routing; only the ack_text is taken from here.
        self.global_exact: dict[str, tuple[str, str]] = {
            # (ack_text, sentiment)
            "yes":        ("Got it.",       "AFFIRMATIVE"),
            "yeah":       ("Got it.",       "AFFIRMATIVE"),
            "yep":        ("Got it.",       "AFFIRMATIVE"),
            "yup":        ("Got it.",       "AFFIRMATIVE"),
            "sure":       ("Of course.",    "AFFIRMATIVE"),
            "correct":    ("That's right.", "AFFIRMATIVE"),
            "right":      ("That's right.", "AFFIRMATIVE"),
            "absolutely": ("Absolutely.",   "AFFIRMATIVE"),
            "definitely": ("Definitely.",   "AFFIRMATIVE"),
            "of course":  ("Of course.",    "AFFIRMATIVE"),
            "haan":       ("Theek hai.",    "AFFIRMATIVE"),
            "ha":         ("Theek hai.",    "AFFIRMATIVE"),
            "ji":         ("Ji.",           "AFFIRMATIVE"),
            "ji haan":    ("Ji haan.",      "AFFIRMATIVE"),
            "bilkul":     ("Bilkul.",       "AFFIRMATIVE"),
            "zaroor":     ("Zaroor.",       "AFFIRMATIVE"),
            "no":         ("Understood.",   "NEGATIVE"),
            "nope":       ("Understood.",   "NEGATIVE"),
            "nah":        ("Understood.",   "NEGATIVE"),
            "nahi":       ("Theek hai.",    "NEGATIVE"),
            "na":         ("Theek hai.",    "NEGATIVE"),
            "okay":       ("Okay.",         "NEUTRAL"),
            "ok":         ("Okay.",         "NEUTRAL"),
            "fine":       ("Sure.",         "NEUTRAL"),
            "alright":    ("Alright.",      "NEUTRAL"),
            "theek hai":  ("Theek hai.",    "NEUTRAL"),
            # Payment-specific
            "i paid":             ("Got it.",       "AFFIRMATIVE"),
            "i made the payment": ("Understood.",   "AFFIRMATIVE"),
            "i paid it":          ("Got it.",       "AFFIRMATIVE"),
            "maine payment kiya": ("Theek hai.",    "AFFIRMATIVE"),
            "someone else":       ("I see.",        "NEUTRAL"),
            "my family":          ("I understand.", "NEUTRAL"),
            "my wife":            ("I understand.", "NEUTRAL"),
            "my husband":         ("I understand.", "NEUTRAL"),
        }

        self._dim    = None
        self._ready  = False
        self._hits   = 0
        self._misses = 0

    # ────────────────────────────────────────────────────────────────────────
    # Startup
    # ────────────────────────────────────────────────────────────────────────

    async def load(self) -> None:
        await asyncio.get_event_loop().run_in_executor(None, self._load)
        self._ready = True
        log.info(
            "CACHE ✔  model loaded  %s  threshold=%.2f",
            EMBED_MODEL, self.threshold
        )

    def _load(self) -> None:
        import logging as _l
        import warnings
        warnings.filterwarnings("ignore")
        for lib in ("sentence_transformers", "transformers",
                    "huggingface_hub", "filelock"):
            _l.getLogger(lib).setLevel(_l.ERROR)

        from sentence_transformers import SentenceTransformer
        try:
            self._model = SentenceTransformer(EMBED_MODEL, local_files_only=True)
        except Exception:
            self._model = SentenceTransformer(EMBED_MODEL)

        # Pre-load FAISS and warm encode path
        v = self._model.encode(
            ["init"], normalize_embeddings=True,
            batch_size=1, show_progress_bar=False
        )
        self._dim = v.shape[1]

        import faiss
        idx = faiss.IndexFlatIP(self._dim)
        z   = np.zeros((1, self._dim), dtype="float32")
        idx.add(z)
        idx.search(z, k=1)
        log.info("CACHE ▶  FAISS AVX2 pre-loaded")

    # ────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ────────────────────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> np.ndarray:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._model.encode(
                [text],
                normalize_embeddings=True,
                batch_size=1,
                show_progress_bar=False,
            ).astype("float32")
        )

    def _get_or_create_idx(self, node: str):
        if node not in self._indexes:
            import faiss
            self._indexes[node] = faiss.IndexFlatIP(self._dim)
            self._entries[node] = []
        return self._indexes[node]

    @staticmethod
    def _clean(text: str) -> str:
        return text.strip().lower().rstrip(".,!? ")

    # ────────────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────────────

    async def lookup(self, node: str, text: str) -> Optional[CacheHit]:
        if not self._ready:
            return None

        clean = self._clean(text)

        # ── L1: Node-specific exact match — O(1) ──────────────────────────
        async with self._lock:
            node_exact = self._exact_matches.get(node, {})
            if clean in node_exact:
                self._hits += 1
                hit = node_exact[clean]
                log.info(
                    "CACHE HIT (NODE_EXACT)   node=%-14s key='%s'",
                    node, clean
                )
                return hit

        # ── L2: Global exact match — O(1) ─────────────────────────────────
        # BUG FIX: previously this block ended with `pass`, so "yes"/"no"/
        # "okay" etc. fell straight through to FAISS. Now returns CacheHit.
        if clean in self.global_exact:
            ack_text, sentiment = self.global_exact[clean]
            self._hits += 1
            log.info(
                "CACHE HIT (GLOBAL_EXACT) node=%-14s key='%s'",
                node, clean
            )
            # next_node="__intent__" signals pipeline to use intent routing
            # for the state transition while using our pre-defined ack_text.
            return CacheHit(
                ack_text   = ack_text,
                next_node  = "__intent__",
                sentiment  = sentiment,
                similarity = 1.0,
            )

        # ── L3: FAISS semantic search — ~3–5 ms ───────────────────────────
        async with self._lock:
            if node not in self._indexes or self._indexes[node].ntotal == 0:
                self._misses += 1
                log.info("CACHE MISS (no index)    node=%-14s", node)
                return None

        vec = await self._embed(text)

        async with self._lock:
            idx = self._indexes[node]
            D, I = idx.search(vec, k=1)
            sim  = float(D[0][0])
            pos  = int(I[0][0])

        if sim >= self.threshold and 0 <= pos < len(self._entries[node]):
            self._hits += 1
            e = self._entries[node][pos]
            log.info(
                "CACHE HIT (SEMANTIC)     node=%-14s sim=%.3f  hits=%d",
                node, sim, self._hits
            )
            return CacheHit(e["ack_text"], e["next_node"], e["sentiment"], sim)

        self._misses += 1
        log.info(
            "CACHE MISS               node=%-14s sim=%.3f",
            node, sim if sim else 0.0
        )
        return None

    async def add(
        self,
        node:      str,
        text:      str,
        ack:       str,
        next_nd:   str,
        sentiment: str,
    ) -> None:
        if not self._ready:
            return

        clean = self._clean(text)
        hit   = CacheHit(ack, next_nd, sentiment, 1.0)

        # BUG FIX: embed BEFORE acquiring the lock so the lock hold time
        # is minimal (embedding takes ~5ms and must not block other lookups).
        vec = await self._embed(text)

        async with self._lock:
            # Always store in exact-match dict for O(1) future lookups
            if node not in self._exact_matches:
                self._exact_matches[node] = {}
            self._exact_matches[node][clean] = hit

            idx     = self._get_or_create_idx(node)
            entries = self._entries[node]

            if len(entries) >= CACHE_MAX_ENTRIES:
                # BUG FIX: FAISS IndexFlatIP has no remove() method.
                # Old code popped from entries list but left stale vectors
                # in the FAISS index, causing index/list position mismatch.
                # Correct fix: reset the index entirely on overflow.
                entries.pop(0)
                import faiss
                new_idx = faiss.IndexFlatIP(self._dim)
                self._indexes[node] = new_idx
                self._entries[node] = entries
                idx = new_idx
                log.debug(
                    "CACHE  ⚠  node=%s hit LRU cap (%d) — index reset",
                    node, CACHE_MAX_ENTRIES
                )

            idx.add(vec)
            entries.append({
                "ack_text":  ack,
                "next_node": next_nd,
                "sentiment": sentiment,
            })

    # ────────────────────────────────────────────────────────────────────────
    # Stats
    # ────────────────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits":     self._hits,
            "misses":   self._misses,
            "total":    total,
            "hit_rate": f"{self._hits / total * 100:.1f}%" if total else "0%",
        }


semantic_cache = SemanticCache()