"""
llm_stream.py — Groq Llama-3.1-8b-instant streaming acknowledgement.

The LLM produces ONLY a 1-2 sentence spoken acknowledgement.
Survey branching is done server-side by survey_engine.py — never by the LLM.
This keeps flow deterministic and latency low.
"""
import logging
import os
from typing import AsyncGenerator

from groq import AsyncGroq

log = logging.getLogger("survey")

SYSTEM_PROMPT = """\
You are a warm, empathetic voice survey bot responding on a phone call.
When the user answers a question, produce a brief spoken acknowledgement — 1 to 2 short sentences.
Rules:
- Sound natural and conversational, as if speaking aloud.
- Match the user's sentiment: sympathetic for complaints, enthusiastic for praise.
- Do NOT ask a follow-up question (the next question is appended separately).
- No markdown, bullets, numbers, or special characters.
- Maximum 25 words total.
Output ONLY the acknowledgement text. Nothing else."""


async def stream_ack(
    transcript: str,
    history: list[dict],
) -> AsyncGenerator[str, None]:
    """
    Yield acknowledgement tokens as they stream from Groq Llama.

    Args:
        transcript: User's latest spoken answer.
        history:    Full conversation history [{role, content}, ...].
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set in .env")

    client = AsyncGroq(api_key=api_key)

    # Last 4 turns for context — keeps prompt small and fast
    recent = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-4:]
        if m["role"] in ("user", "assistant")
    ]

    messages = [
        {"role": "system",  "content": SYSTEM_PROMPT},
        *recent,
        {"role": "user",    "content": transcript},
    ]

    try:
        stream = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            stream=True,
            max_tokens=60,       # short acks only
            temperature=0.6,
        )
        async for chunk in stream:
            token = chunk.choices[0].delta.content or ""
            if token:
                yield token

    except Exception as exc:
        log.error("LLM  ✘  %s", exc)
        yield "Thank you for sharing that."   # safe fallback — survey continues