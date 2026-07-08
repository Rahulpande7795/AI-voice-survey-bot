"""
pipeline.py — V4 orchestration. Production-hardened.

Decision order per turn:
  1. Intent classifier (<1ms) → INTENT_HIT
  2. Semantic FAISS cache (~5ms) → CACHE_HIT
  3. Groq LLM (~300-800ms) → LLM_CALL with sentence-level TTS streaming

Sentence-level streaming (KEY FIX):
  TTS tasks for each sentence are STARTED immediately as sentences arrive from
  the LLM, not collected and awaited after the LLM finishes.
  This means first audio can arrive while the LLM is still generating.
  We track tasks in a list and await them only once before sending next_question.

Pre-emptive TTS:
  Background task synthesises most likely next question while user is still
  speaking, so it's ready to play instantly on cache/intent hits.
"""
import asyncio
import base64
import json
import logging
import time
from typing import Optional

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


async def _speak(ws, text: str, cached: Optional[bytes] = None) -> float:
    """Send TTS audio over WebSocket. Returns milliseconds taken."""
    if not text or not text.strip():
        return 0.0
    t0 = time.perf_counter()

    # Use pre-synthesised bytes if available, otherwise call edge-tts
    src = cached if cached else None
    if src:
        for i in range(0, len(src), tts_stream.CHUNK_SZ):
            await _send(ws, {"type": "audio_chunk",
                              "data": base64.b64encode(src[i:i + tts_stream.CHUNK_SZ]).decode()})
    else:
        async for chunk in tts_stream.synthesise_chunks(text):
            await _send(ws, {"type": "audio_chunk",
                              "data": base64.b64encode(chunk).decode()})

    ms = (time.perf_counter() - t0) * 1000
    await _send(ws, {"type": "audio_ready"})
    return ms


async def _speak_sentence(ws, text: str) -> None:
    """
    TTS a single sentence and stream it over WebSocket.
    Called as asyncio.create_task() during LLM streaming to overlap I/O.
    Each sentence gets its own audio_ready signal so the client can play
    sentence chunks progressively.
    """
    try:
        async for chunk in tts_stream.synthesise_chunks(text):
            await ws.send_text(json.dumps(
                {"type": "audio_chunk",
                 "data": base64.b64encode(chunk).decode()}
            ))
        await ws.send_text(json.dumps({"type": "audio_ready"}))
    except Exception as e:
        log.warning("TTS_SENTENCE ✘  %s", e)


async def preempt_next_question(session: context.Session, intent_result) -> None:
    """Synthesise next question in background while user is still speaking."""
    if intent_result is None:
        return
    nd = intent_result.next_node
    text = survey_engine.get_question(nd)
    if not text:
        return
    try:
        audio = await tts_stream.synthesise(text)
        session.preempt_audio = audio
        session.preempt_node  = nd
        log.info("PREEMPT ✔  node=%s  %dB queued", nd, len(audio))
    except Exception as e:
        log.warning("PREEMPT ⚠  %s", e)


async def run_session_start(ws, session: context.Session):
    session.current_node = survey_engine.START_NODE
    log.info("PIPE ▶  session start  node=%s", session.current_node)
    await _send(ws, {"type": "intro_text", "lines": survey_engine.INTRO_LINES})
    ms = await _speak(ws, survey_engine.INTRO_TEXT)
    log.info("TTS  ✔  intro  %.0f ms", ms)
    first_nd = survey_engine.next_node(session.current_node, "default")
    session.current_node = first_nd
    q_text = survey_engine.get_question(first_nd)
    ms = await _speak(ws, q_text)
    log.info("TTS  ✔  first Q  %.0f ms", ms)
    await _send(ws, {"type": "next_question", "text": q_text, "node": first_nd})


async def run_turn(ws, session: context.Session,
                   transcript: str, was_partial: bool = False):
    t_turn = time.perf_counter()
    node   = session.current_node
    log.info("PIPE ▶  node=%-14s  partial=%s  %r", node, was_partial, transcript[:70])

    await _send(ws, {"type": "transcript_final", "text": transcript,
                      "partial": was_partial})
    session.add_turn("user", transcript)

    # Extract structured data (zero network cost, <1ms)
    extracted = extractor.extract(node, transcript)
    if extracted:
        session.merge_capture(extracted)
        log.info("EXTRACT     %s", extracted)

    # ── 1. Intent (regex, <1ms) ────────────────────────────────────────────
    result = intent_mod.classify(transcript, node)
    if result:
        log.info("INTENT_HIT  node=%-14s  type=%-8s  conf=%.2f  → %s",
                 node, result.intent_type, result.confidence, result.next_node)
        ack_text  = result.ack_text
        next_nd   = result.next_node
        sentiment = result.sentiment
        source    = "intent"

    else:
        # ── 2. Semantic cache (~5ms) ───────────────────────────────────────
        hit = await semantic_cache.lookup(node, transcript)
        if hit:
            ack_text  = hit.ack_text
            next_nd   = hit.next_node
            sentiment = hit.sentiment
            source    = "cache"

        else:
            # ── 3. Groq LLM with REAL sentence-level TTS streaming ─────────
            # KEY: each sentence task is STARTED immediately when a sentence
            # boundary arrives — we do NOT wait for the full LLM response.
            # tts_tasks collects the running coroutines; we await ALL of them
            # only after the LLM stream is complete, to ensure all audio is
            # flushed before sending next_question.
            t_llm     = time.perf_counter()
            ack_text  = ""
            source    = "llm"
            tts_tasks: list[asyncio.Task] = []

            async for event in llm_stream.stream_ack(transcript,
                                                      session.chat_history):
                if event["type"] == "token":
                    # Forward token to UI for streaming display
                    await _send(ws, {"type": "llm_token", "text": event["text"]})

                elif event["type"] == "sentence":
                    # IMMEDIATELY start TTS for this sentence — don't wait
                    task = asyncio.create_task(
                        _speak_sentence(ws, event["text"])
                    )
                    tts_tasks.append(task)

                elif event["type"] == "done":
                    ack_text = event["text"]

            llm_ms = (time.perf_counter() - t_llm) * 1000
            log.info("LLM_CALL    node=%-14s  %.0f ms  %r",
                     node, llm_ms, ack_text[:60])

            # Wait for all sentence TTS tasks to finish sending audio
            if tts_tasks:
                await asyncio.gather(*tts_tasks, return_exceptions=True)

            sentiment = survey_engine.detect_sentiment(transcript)
            next_nd   = _infer_next(node, transcript)

            # Add to cache for future lookups
            await semantic_cache.add(node, transcript, ack_text, next_nd, sentiment)

    # ── Advance session state ──────────────────────────────────────────────
    session.current_node = next_nd
    session.add_turn("assistant", ack_text, sentiment)
    is_done   = survey_engine.is_terminal(next_nd)
    next_text = survey_engine.get_question(next_nd)

    # ── TTS playback ───────────────────────────────────────────────────────
    t_tts = time.perf_counter()

    # For intent/cache hits: speak the ack text first
    if source != "llm":
        ack_ms = await _speak(ws, ack_text)
        log.info("TTS  ✔  ack  %.0f ms  source=%s", ack_ms, source)

    # Play next question — use pre-synthesised audio if available (preempt hit)
    preempt_hit = (session.preempt_node == next_nd
                   and session.preempt_audio is not None
                   and len(session.preempt_audio) > 0)
    q_ms = await _speak(ws, next_text,
                         cached=session.preempt_audio if preempt_hit else None)
    log.info("TTS  ✔  question  %.0f ms  %s",
             q_ms, "PREEMPT" if preempt_hit else ("HIT" if q_ms < 15 else "MISS"))

    session.preempt_audio = None
    session.preempt_node  = None
    tts_ms   = (time.perf_counter() - t_tts) * 1000
    total_ms = (time.perf_counter() - t_turn) * 1000

    if session.data_capture:
        log.info("DATA        %s", session.data_capture)

    if is_done:
        log.info("CAPTURE_FINAL  %s", session.data_capture)
        await _send(ws, {"type": "survey_done", "capture": session.data_capture})
        log.info("PIPE ✔  call complete  %.0f ms total", total_ms)
    else:
        await _send(ws, {
            "type":    "next_question",
            "text":    next_text,
            "node":    next_nd,
            "source":  source,
            "latency": {"tts": round(tts_ms), "total": round(total_ms)},
            "capture": session.data_capture,
        })
        log.info("PIPE ✔  %s → %s  via=%-6s  %.0f ms",
                 node, next_nd, source, total_ms)


def _infer_next(node: str, text: str) -> str:
    import re
    t   = text.lower()
    yes = bool(re.search(r"\b(yes|haan|ji|paid|ho\s*gaya|kar\s*diya|bilkul|okay|done)\b", t))
    no  = bool(re.search(r"\b(no|nahi|nahin|nahi\s*kiya|abhi\s*nahi)\b", t))
    if yes and not no:
        return survey_engine.next_node(node, "yes")
    if no and not yes:
        return survey_engine.next_node(node, "no")
    return survey_engine.next_node(node, "default")


# ─────────────────────────────────────────────────────────────────────────────
# Twilio phone call pipeline
# Mirrors run_session_start / run_turn but uses tts_sender(ws, sid, text)
# instead of _speak(ws, text).  All routing logic is identical.
# Browser-specific messages (stt_done, llm_token, transcript_final) omitted.
# ─────────────────────────────────────────────────────────────────────────────

async def run_session_start_twilio(ws, stream_sid: str,
                                    session: context.Session,
                                    tts_sender) -> None:
    """
    Play intro + first question over a Twilio phone call.

    tts_sender signature: async (ws, stream_sid, text) -> None
    Uses the shared edge-tts warm cache — same audio as browser path.
    """
    session.current_node = survey_engine.START_NODE
    log.info("TWILIO PIPE ▶  session start  node=%s", session.current_node)

    # Play intro
    await tts_sender(ws, stream_sid, survey_engine.INTRO_TEXT)

    # Advance to first question node
    first_nd = survey_engine.next_node(session.current_node, "default")
    session.current_node = first_nd
    q_text = survey_engine.get_question(first_nd)

    # Small gap between intro and first question (natural pause)
    await asyncio.sleep(0.4)
    await tts_sender(ws, stream_sid, q_text)
    log.info("TWILIO PIPE ✔  intro + first Q played  node=%s", first_nd)


async def run_turn_twilio(ws, stream_sid: str,
                           session: context.Session,
                           transcript: str,
                           tts_sender) -> None:
    """
    Run one survey turn for a Twilio phone call.

    Decision order (identical to browser run_turn):
      1. Intent classifier (<1ms)  → INTENT_HIT
      2. Semantic FAISS cache      → CACHE_HIT
      3. Groq LLM with streaming   → LLM_CALL

    Audio output via tts_sender(ws, stream_sid, text) instead of WebSocket
    binary frames.  No browser UI messages sent.
    """
    t_turn = time.perf_counter()
    node   = session.current_node
    log.info("TWILIO PIPE ▶  node=%-14s  %r", node, transcript[:70])

    session.add_turn("user", transcript)

    # Extract structured data (<1ms)
    import extractor
    extracted = extractor.extract(node, transcript)
    if extracted:
        session.merge_capture(extracted)
        log.info("EXTRACT     %s", extracted)

    # ── 1. Intent classifier ───────────────────────────────────────────────
    result = intent_mod.classify(transcript, node)
    if result:
        log.info("INTENT_HIT  node=%-14s  type=%-8s  conf=%.2f  → %s",
                 node, result.intent_type, result.confidence, result.next_node)
        ack_text  = result.ack_text
        next_nd   = result.next_node
        sentiment = result.sentiment
        source    = "intent"

    else:
        # ── 2. Semantic FAISS cache ────────────────────────────────────────
        hit = await semantic_cache.lookup(node, transcript)
        if hit:
            ack_text  = hit.ack_text
            next_nd   = hit.next_node
            sentiment = hit.sentiment
            source    = "cache"

        else:
            # ── 3. Groq LLM ───────────────────────────────────────────────
            t_llm    = time.perf_counter()
            ack_text = ""
            source   = "llm"
            # Collect full LLM response — phone TTS is one shot per sentence
            llm_sentences: list[str] = []

            async for event in llm_stream.stream_ack(transcript,
                                                      session.chat_history):
                if event["type"] == "sentence":
                    llm_sentences.append(event["text"])
                elif event["type"] == "done":
                    ack_text = event["text"]

            llm_ms = (time.perf_counter() - t_llm) * 1000
            log.info("LLM_CALL  node=%-14s  %.0fms  %r",
                     node, llm_ms, ack_text[:60])

            # Stream each LLM sentence to caller sequentially
            for sentence in llm_sentences:
                await tts_sender(ws, stream_sid, sentence)

            sentiment = survey_engine.detect_sentiment(transcript)
            next_nd   = _infer_next(node, transcript)
            await semantic_cache.add(node, transcript, ack_text, next_nd, sentiment)

    # ── Advance session state ──────────────────────────────────────────────
    session.current_node = next_nd
    session.add_turn("assistant", ack_text, sentiment)
    is_done   = survey_engine.is_terminal(next_nd)
    next_text = survey_engine.get_question(next_nd)

    # ── TTS playback ───────────────────────────────────────────────────────
    # For intent/cache hits: speak the ack first, then the next question
    if source != "llm":
        await tts_sender(ws, stream_sid, ack_text)

    if not is_done:
        await tts_sender(ws, stream_sid, next_text)

    total_ms = (time.perf_counter() - t_turn) * 1000

    if session.data_capture:
        log.info("DATA        %s", session.data_capture)

    if is_done:
        log.info("CAPTURE_FINAL  %s", session.data_capture)
        # Play closing message to caller
        await tts_sender(ws, stream_sid,
            "Thank you for your time. Your payment details have been noted. "
            "Have a good day. Goodbye.")
        log.info("TWILIO PIPE ✔  call complete  %.0f ms total", total_ms)
    else:
        log.info("TWILIO PIPE ✔  %s → %s  via=%-6s  %.0f ms",
                 node, next_nd, source, total_ms)