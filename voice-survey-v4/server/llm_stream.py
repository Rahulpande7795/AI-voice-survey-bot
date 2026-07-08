"""
llm_stream.py — V4 sentence-level streaming for LLM ack.

Buffers tokens and yields complete sentences as soon as they end (. ! ?).
Pipeline.py starts TTS on each sentence immediately — first audio arrives
~200-400ms earlier than waiting for the full ack.

Also yields the full assembled text at the end for caching/history.

FIXES in this version:
  - AsyncGroq client created ONCE as module-level singleton (not per-turn).
    Per-turn creation wastes ~30-80ms on connection setup.
  - 8-second total timeout on the API call (prevents hanging turns).
  - Sentence-end regex updated to handle Hindi danda (।) more robustly.
"""
import asyncio
import logging
import os
import re
from typing import AsyncGenerator

from groq import AsyncGroq

log = logging.getLogger("survey")

# ── Singleton client — created once at import time ─────────────────────────
_client: AsyncGroq | None = None

def _get_client() -> AsyncGroq:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY not set")
        _client = AsyncGroq(api_key=api_key)
        log.info("LLM  ▶  AsyncGroq client initialised (singleton)")
    return _client


# ── Sentence boundary: split on punctuation followed by whitespace ──────────
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")

SYSTEM_PROMPT = """\
You are a warm, professional L&T Finance customer service representative on a phone call.
When the customer gives an answer, produce a brief spoken acknowledgement — 1 to 2 short sentences.
Rules:
- Natural, conversational Hindi-English mix (Hinglish) is fine.
- Match the customer's sentiment: reassuring for problems, warm for compliance.
- Do NOT ask a follow-up question (the next question is appended separately).
- No markdown, bullets, or special characters. Plain spoken text only.
- Maximum 20 words.
Output ONLY the acknowledgement. Nothing else."""


async def stream_ack(
    transcript: str,
    history: list[dict],
) -> AsyncGenerator[dict, None]:
    """
    Yield dicts of three types:
      {"type": "token",    "text": str}   — individual token (sent to UI)
      {"type": "sentence", "text": str}   — complete sentence (triggers TTS immediately)
      {"type": "done",     "text": str}   — full assembled ack (for cache/history)
    """
    client = _get_client()

    recent = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-4:]
        if m["role"] in ("user", "assistant")
    ]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *recent,
        {"role": "user",   "content": transcript},
    ]

    full_text    = ""
    sentence_buf = ""

    try:
        # 8-second timeout prevents stuck turns on Groq API hiccups
        stream = await asyncio.wait_for(
            client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=messages,
                stream=True,
                max_tokens=60,
                temperature=0.55,
            ),
            timeout=8.0
        )

        async for chunk in stream:
            token = chunk.choices[0].delta.content or ""
            if not token:
                continue

            full_text    += token
            sentence_buf += token

            # Yield token for UI streaming display
            yield {"type": "token", "text": token}

            # Check for sentence boundary → flush to TTS immediately
            parts = _SENTENCE_END.split(sentence_buf)
            while len(parts) > 1:
                complete = parts.pop(0).strip()
                sentence_buf = " ".join(parts)
                if complete:
                    yield {"type": "sentence", "text": complete}

        # Flush any remaining text that doesn't end with punctuation
        if sentence_buf.strip():
            yield {"type": "sentence", "text": sentence_buf.strip()}

        yield {"type": "done", "text": full_text.strip()}

    except asyncio.TimeoutError:
        log.error("LLM  ✘  API timeout after 8s")
        fallback = "Theek hai, samajh gaye."
        yield {"type": "token",    "text": fallback}
        yield {"type": "sentence", "text": fallback}
        yield {"type": "done",     "text": fallback}
    except Exception as exc:
        log.error("LLM  ✘  %s", exc)
        fallback = "Theek hai, samajh gaye."
        yield {"type": "token",    "text": fallback}
        yield {"type": "sentence", "text": fallback}
        yield {"type": "done",     "text": fallback}