"""
cache.py — Semantic FAISS cache for LLM ack responses.

FIXES IN THIS REVISION:
  - local_files_only=True: skips ALL HuggingFace HTTP checks after first download.
    Startup drops from ~12s to ~2s.
  - Per-node entry lists: FAISS pos maps correctly (no cross-node alignment bug).
  - TQDM / progress bars suppressed via env vars (set in main.py before imports).
  - Hit/miss counters + /stats endpoint.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger("survey")

CACHE_THRESHOLD = float(os.getenv("CACHE_THRESHOLD", "0.85"))
EMBED_MODEL     = os.getenv("CACHE_MODEL", "all-MiniLM-L6-v2")


@dataclass
class CacheHit:
    ack_text:   str
    next_node:  str
    sentiment:  str
    similarity: float


class SemanticCache:
    """
    Semantic cache backed by FAISS IndexFlatIP (cosine similarity).
    Each survey node has its own independent FAISS index and entry list.
    """

    def __init__(self, threshold: float = CACHE_THRESHOLD) -> None:
        self.threshold     = threshold
        self._model        = None
        self._lock         = asyncio.Lock()
        self._node_entries: dict[str, list[dict]] = {}
        self._node_indexes: dict[str, object]     = {}
        self._dim: Optional[int] = None
        self._ready  = False
        self._hits   = 0
        self._misses = 0

    async def load(self) -> None:
        """Load embedding model. Uses local cache if available (no network)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_blocking)
        self._ready = True
        log.info("CACHE ✔  model loaded (%s)  threshold=%.2f", EMBED_MODEL, self.threshold)

    def _load_blocking(self) -> None:
        # Silence all third-party loggers inside thread
        import logging as _log
        for lib in ("sentence_transformers", "transformers", "huggingface_hub",
                    "filelock", "urllib3"):
            _log.getLogger(lib).setLevel(_log.ERROR)

        import warnings
        warnings.filterwarnings("ignore")

        from sentence_transformers import SentenceTransformer

        # local_files_only=True: NEVER make network requests.
        # Uses the HuggingFace local cache (~/.cache/huggingface/).
        # First run (no cache) will fail here — remove the flag if running fresh.
        try:
            self._model = SentenceTransformer(EMBED_MODEL, local_files_only=True)
        except Exception:
            # First run: model not cached yet — download it (one time only)
            log.info("CACHE ▶  downloading model (first run only)…")
            self._model = SentenceTransformer(EMBED_MODEL)

        vec = self._model.encode(["init"], normalize_embeddings=True,
                                 show_progress_bar=False)
        self._dim = vec.shape[1]

    async def _embed(self, text: str) -> np.ndarray:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._model.encode(
                [text], normalize_embeddings=True, show_progress_bar=False
            ).astype("float32"),
        )

    def _get_index(self, node: str):
        if node not in self._node_indexes:
            import faiss
            self._node_indexes[node] = faiss.IndexFlatIP(self._dim)
            self._node_entries[node] = []
        return self._node_indexes[node]

    async def lookup(self, current_node: str, answer_text: str) -> Optional[CacheHit]:
        if not self._ready:
            return None

        async with self._lock:
            if current_node not in self._node_indexes:
                self._misses += 1
                return None
            ntotal = self._node_indexes[current_node].ntotal
        if ntotal == 0:
            self._misses += 1
            return None

        vec = await self._embed(answer_text)

        async with self._lock:
            idx     = self._node_indexes[current_node]
            entries = self._node_entries[current_node]
            distances, positions = idx.search(vec, k=1)
            sim = float(distances[0][0])
            pos = int(positions[0][0])

        if sim >= self.threshold and 0 <= pos < len(entries):
            self._hits += 1
            entry = entries[pos]
            log.info("CACHE HIT   node=%-14s  sim=%.3f  hits=%d  → %r",
                     current_node, sim, self._hits, entry["ack_text"][:40])
            return CacheHit(entry["ack_text"], entry["next_node"],
                            entry["sentiment"], sim)

        self._misses += 1
        log.info("CACHE MISS  node=%-14s  sim=%.3f", current_node, sim)
        return None

    async def add(self, current_node: str, answer_text: str,
                  ack_text: str, next_node: str, sentiment: str) -> None:
        if not self._ready:
            return
        vec = await self._embed(answer_text)
        async with self._lock:
            idx = self._get_index(current_node)
            idx.add(vec)
            self._node_entries[current_node].append(
                {"ack_text": ack_text, "next_node": next_node, "sentiment": sentiment}
            )
        log.debug("CACHE ADD   node=%-14s  answer=%r", current_node, answer_text[:40])

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits":    self._hits,
            "misses":  self._misses,
            "total":   total,
            "hit_rate": f"{self._hits/total*100:.1f}%" if total else "0%",
        }


semantic_cache = SemanticCache()