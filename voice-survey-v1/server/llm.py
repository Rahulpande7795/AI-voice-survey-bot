"""
llm.py — Groq (Llama 3.1 8B Instant) wrapper + survey conductor logic.

Groq is FREE (console.groq.com) and the fastest hosted LLM available —
~50 ms TTFT on llama-3.1-8b-instant, making it ideal for V1 and the
"fast path" in V3.

Each call receives the full conversation history and returns a structured
JSON response: {acknowledgement, next_question, done}.
"""

import json
import os
import re
from typing import Any

from groq import AsyncGroq

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a voice survey bot. You MUST respond with ONLY a raw JSON object — no prose, no markdown, no explanation before or after.

REQUIRED OUTPUT FORMAT (copy this structure exactly):
{"acknowledgement": "...", "next_question": "...", "done": false}

Field rules:
- "acknowledgement": 1-2 sentence empathetic response to the user's answer. Empty string "" for START_SURVEY.
- "next_question": the exact text of the next question to ask. Empty string "" when done=true.
- "done": boolean true only after ALL 5 questions have been answered.

CRITICAL: Your entire response must be ONE JSON object. Do NOT write any text outside the JSON braces.
If the input is START_SURVEY, return the first question in next_question with done=false and acknowledgement="".
Count answers carefully — mark done=true only after the 5th answer."""


def _extract_json(raw: str) -> dict:
    """
    Robustly extract a JSON object from the model reply.

    Strategy (tried in order):
      1. Strip markdown fences and parse the whole thing.
      2. Find the first {...} block and parse that.
      3. FALLBACK: model returned plain conversational text — treat the
         entire reply as the acknowledgement and leave next_question empty
         so the caller can fill it in from context.
    """
    # Strip markdown fences
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()

    # --- attempt 1: parse whole cleaned string ---
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # --- attempt 2: find first {...} block ---
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # --- attempt 3: model returned plain text — wrap it ---
    # This happens when Llama ignores the JSON instruction.
    # Treat the whole reply as the acknowledgement; caller handles next_q.
    plain = cleaned.strip().strip('"')
    if plain:
        return {
            "acknowledgement": plain,
            "next_question": "",   # caller will inject the correct next question
            "done": False,
            "_plain_text_fallback": True,
        }

    raise ValueError(f"Could not extract any usable content from LLM reply:\n{raw!r}")


# ── Main function ─────────────────────────────────────────────────────────────

async def get_response(
    transcript: str,
    history: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Send the transcript + conversation history to Groq and get the next survey step.

    Args:
        transcript : The user's latest spoken answer (already transcribed).
        history    : List of {role, content} dicts for the full conversation.
        questions  : The ordered list of survey question objects from survey.json.

    Returns:
        dict with keys: acknowledgement (str), next_question (str), done (bool)
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY is not set in the environment.\n"
            "  → Get your FREE key at https://console.groq.com\n"
            "  → Then add  GROQ_API_KEY=gsk_...  to your .env file"
        )

    client = AsyncGroq(api_key=api_key)

    # ── Work out which question comes next using server-side counter ──────────
    # Count user turns in history (excluding START_SURVEY) to know how many
    # questions have already been answered — this is the source of truth,
    # not the model, so question sequencing never drifts.
    answered = sum(
        1 for m in history
        if m.get("role") == "user" and m.get("content", "").strip() != "START_SURVEY"
    )
    if transcript.strip() != "START_SURVEY":
        answered += 1   # the current answer counts too

    next_q_index = answered          # 0-based index of next question to ask
    is_done      = next_q_index >= len(questions)

    # ── Build system prompt ───────────────────────────────────────────────────
    questions_text = "\n".join(
        f"Q{i+1}: {q['text']}" for i, q in enumerate(questions)
    )
    full_system = (
        SYSTEM_PROMPT
        + f"\n\nThe 5 survey questions to ask in this exact order:\n{questions_text}"
        + f"\n\nServer state: {answered} question(s) answered so far. "
        + ("ALL DONE — set done=true." if is_done else
           f"Next question to ask is Q{next_q_index + 1}.")
    )

    # ── Build message list ────────────────────────────────────────────────────
    messages: list[dict[str, str]] = [{"role": "system", "content": full_system}]
    for msg in history:
        role = msg.get("role", "user")
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": str(msg["content"])})
    messages.append({"role": "user", "content": transcript})

    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            temperature=0.3,        # lower = more reliable JSON formatting
            max_tokens=300,
            messages=messages,      # type: ignore[arg-type]
        )

        raw = response.choices[0].message.content or "{}"
        parsed = _extract_json(raw)

        # ── Always use server-side question index — never trust the model ──────
        # The model frequently skips or repeats questions. The server counts
        # answered turns itself, so the sequence is always correct.
        if is_done:
            next_question = ""
            done = True
        else:
            next_question = questions[next_q_index]["text"]
            done = False

        # Log if model returned a different question (useful for debugging)
        model_next_q = str(parsed.get("next_question", "")).strip()
        if model_next_q and model_next_q != next_question:
            import logging as _log
            _log.getLogger("survey").warning(
                "LLM  ⚠  model suggested Q: '%s...' — overridden with server Q%d",
                model_next_q[:40], next_q_index + 1
            )

        # Plain-text fallback: log a warning so we can see when it triggers
        if parsed.get("_plain_text_fallback"):
            import logging
            logging.getLogger("survey").warning(
                "LLM  ⚠  plain-text fallback triggered — model ignored JSON instruction"
            )

        return {
            "acknowledgement": str(parsed.get("acknowledgement", "")),
            "next_question":   next_question,
            "done":            done,
        }

    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"LLM returned invalid JSON: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Groq API call failed: {exc}") from exc