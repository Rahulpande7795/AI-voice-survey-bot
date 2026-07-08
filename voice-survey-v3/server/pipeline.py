"""
pipeline.py — V3 orchestration: intent → cache → LLM, sentence-level TTS.

Decision order per turn:
  1. Intent classifier  (<1ms, local)       → INTENT_HIT
  2. Semantic cache     (~5ms, local FAISS)  → CACHE_HIT
  3. Groq LLM           (~1300ms, network)   → LLM_CALL

Tasks covered: 1 (cache), 2 (intent), 6 (sentence TTS).
"""
import asyncio
import base64
import json
import logging
import time

import context
import extractor
import intent as intent_mod
import llm_stream
import survey_engine
import tts_stream
from cache import semantic_cache

log = logging.getLogger("survey")


async def _send(ws, payload: dict):
    await ws.send_text(json.dumps(payload))


async def speak(ws, text: str) -> float:
    """Synthesise text, stream chunks, fire audio_ready. Returns ms."""
    if not text or not text.strip():
        return 0.0
    t0 = time.perf_counter()
    async for chunk in tts_stream.synthesise_chunks(text):
        await _send(ws, {
            "type": "audio_chunk",
            "data": base64.b64encode(chunk).decode(),
        })
    ms = (time.perf_counter() - t0) * 1000
    await _send(ws, {"type": "audio_ready"})
    return ms


async def run_session_start(ws, session: context.Session):
    """Speak intro on connect / restart."""
    session.current_node = survey_engine.START_NODE
    log.info("PIPE ▶  session start  node=%s", session.current_node)

    await _send(ws, {
        "type":  "intro_text",
        "lines": survey_engine.INTRO_LINES,
    })

    intro_ms = await speak(ws, survey_engine.INTRO_TEXT)
    log.info("TTS  ✔  intro %.0f ms", intro_ms)

    # Advance to first real question node (AVAILABILITY)
    first_q_node = survey_engine.next_node(session.current_node, "default")
    session.current_node = first_q_node
    q_text = survey_engine.get_question(first_q_node)
    q_ms   = await speak(ws, q_text)
    log.info("TTS  ✔  first Q %.0f ms", q_ms)

    await _send(ws, {
        "type": "next_question",
        "text": q_text,
        "node": first_q_node,
    })


async def run_turn(ws, session: context.Session, transcript: str):
    """
    V3 pipeline per user turn.

    INTENT_HIT  → skip cache + LLM  → ~5ms  ack decision
    CACHE_HIT   → skip LLM          → ~15ms ack decision
    LLM_CALL    → full Groq round-trip → ~1300ms
    """
    t_turn = time.perf_counter()
    node   = session.current_node
    log.info("PIPE ▶  node=%-14s  %r", node, transcript[:70])

    # ── 1. Echo transcript to UI ──────────────────────────────────────────
    await _send(ws, {"type": "transcript_final", "text": transcript})
    session.add_turn("user", transcript)

    # ── 2. Extract structured data (always, zero-cost) ────────────────────
    extracted = extractor.extract(node, transcript)
    if extracted:
        session.merge_capture(extracted)
        log.info("EXTRACT     %s", extracted)

    # ── 3. Intent classifier ──────────────────────────────────────────────
    intent_result = intent_mod.classify(transcript, node)

    if intent_result:
        log.info(
            "INTENT_HIT  node=%-14s  type=%-8s  conf=%.2f  → %s",
            node, intent_result.intent_type, intent_result.confidence, intent_result.next_node,
        )
        ack_text  = intent_result.ack_text
        next_nd   = intent_result.next_node
        sentiment = intent_result.sentiment
        source    = "intent"

    else:
        # ── 4. Semantic cache ─────────────────────────────────────────────
        cache_hit = await semantic_cache.lookup(node, transcript)

        if cache_hit:
            ack_text  = cache_hit.ack_text
            next_nd   = cache_hit.next_node
            sentiment = cache_hit.sentiment
            source    = "cache"

        else:
            # ── 5. LLM (fallback) ─────────────────────────────────────────
            t_llm    = time.perf_counter()
            ack_text = ""
            source   = "llm"

            # TASK 6: sentence-level TTS streaming
            # Each complete sentence fires TTS immediately — don't wait for full ack
            tts_tasks = []

            async for event in llm_stream.stream_ack(transcript, session.chat_history):
                if event["type"] == "token":
                    await _send(ws, {"type": "llm_token", "text": event["text"]})

                elif event["type"] == "sentence":
                    # Fire TTS for this sentence immediately (don't await — run concurrently)
                    sentence = event["text"]
                    task = asyncio.create_task(_speak_and_send(ws, sentence))
                    tts_tasks.append(task)

                elif event["type"] == "done":
                    ack_text = event["text"]

            llm_ms = (time.perf_counter() - t_llm) * 1000
            log.info("LLM_CALL    node=%-14s  %.0f ms  %r", node, llm_ms, ack_text[:60])

            # Wait for all sentence TTS tasks to complete before advancing
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            # Determine next node from survey engine (LLM path uses sentiment/transcript)
            sentiment = survey_engine.detect_sentiment(transcript)
            # For yes/no nodes, re-check intent with lower confidence
            _fallback = _infer_next_from_text(node, transcript)
            next_nd   = _fallback or survey_engine.next_node(node, "default")

            # Add to cache so future similar answers hit
            await semantic_cache.add(
                current_node=node,
                answer_text=transcript,
                ack_text=ack_text,
                next_node=next_nd,
                sentiment=sentiment,
            )

    # ── 6. Advance session state ──────────────────────────────────────────
    session.current_node = next_nd
    session.add_turn("assistant", ack_text, sentiment=sentiment)
    is_done = survey_engine.is_terminal(next_nd)

    # ── 7. TTS for intent/cache hits (LLM already streamed sentence-by-sentence) ──
    t_tts = time.perf_counter()
    if source != "llm":
        ack_ms = await speak(ws, ack_text)
        log.info("TTS  ✔  ack %.0f ms (source=%s)", ack_ms, source)

    next_text = survey_engine.get_question(next_nd)
    q_ms  = await speak(ws, next_text)
    tts_ms = (time.perf_counter() - t_tts) * 1000
    log.info("TTS  ✔  question %.0f ms (cache %s)", q_ms, "HIT" if q_ms < 15 else "MISS")

    # ── 8. Log data_capture state ─────────────────────────────────────────
    if session.data_capture:
        log.info("DATA        %s", session.data_capture)

    # ── 9. Signal client ──────────────────────────────────────────────────
    total_ms = (time.perf_counter() - t_turn) * 1000

    if is_done:
        # Log final captured data
        log.info("CAPTURE_FINAL  %s", session.data_capture)
        await _send(ws, {
            "type":    "survey_done",
            "capture": session.data_capture,
        })
        log.info("PIPE ✔  call complete  %.0f ms total", total_ms)
    else:
        await _send(ws, {
            "type":    "next_question",
            "text":    next_text,
            "node":    next_nd,
            "source":  source,
            "latency": {
                "tts":   round(tts_ms),
                "total": round(total_ms),
            },
        })
        log.info(
            "PIPE ✔  %s → %s  via=%-6s  %.0f ms",
            node, next_nd, source, total_ms,
        )


# ── Helpers ───────────────────────────────────────────────────────────────

async def _speak_and_send(ws, text: str) -> None:
    """Used by sentence-level TTS to run concurrently with LLM token streaming."""
    async for chunk in tts_stream.synthesise_chunks(text):
        await ws.send_text(json.dumps({
            "type": "audio_chunk",
            "data": base64.b64encode(chunk).decode(),
        }))
    await ws.send_text(json.dumps({"type": "audio_ready"}))


def _infer_next_from_text(node: str, text: str) -> str:
    """
    Lightweight yes/no fallback for when intent classifier returned None
    (e.g. unusual phrasing) but LLM handled the ack.
    """
    import re
    t = text.lower()
    has_yes = bool(re.search(r"\b(yes|haan|ji|paid|ho gaya|kar diya|bilkul)\b", t))
    has_no  = bool(re.search(r"\b(no|nahi|nahin|nahi kiya|abhi nahi)\b", t))
    if has_yes and not has_no:
        return survey_engine.next_node(node, "yes")
    if has_no and not has_yes:
        return survey_engine.next_node(node, "no")
    return survey_engine.next_node(node, "default")