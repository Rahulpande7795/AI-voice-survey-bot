"""
pipeline.py — STT → LLM → TTS orchestration per WebSocket turn.

run_session_start now speaks:
  1. INTRO_TEXT  (context for the user — pre-warmed in cache)
  2. Q1          (pre-warmed in cache)

Then run_turn handles all subsequent turns with two separate TTS calls:
  - ack text    (unique LLM output → cache miss, ~700-900ms)
  - next Q      (fixed phrase → cache HIT, <10ms)
"""
import asyncio
import base64
import json
import logging
import time

import context
import llm_stream
import survey_engine
import tts_stream

log = logging.getLogger("survey")


async def _send(ws, payload: dict):
    await ws.send_text(json.dumps(payload))


async def speak(ws, text: str) -> float:
    """Synthesise text, stream MP3 chunks, fire audio_ready. Returns ms."""
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
    """
    Speak intro context + Q1 on connect / restart.
    Both phrases are pre-warmed → should hit cache in <15ms each.
    Sends intro_text event first so the UI can display the welcome copy.
    """
    log.info("PIPE ▶  session start  node=%s", session.current_node)

    # 1. Send intro text to UI (displayed as context card)
    await _send(ws, {
        "type":  "intro_text",
        "lines": survey_engine.INTRO_LINES,
    })

    # 2. Speak intro aloud
    ms = await speak(ws, survey_engine.INTRO_TEXT)
    log.info("TTS  ✔  intro %.0f ms", ms)

    # 3. Speak Q1
    question = survey_engine.get_question(session.current_node)
    ms = await speak(ws, question)
    log.info("TTS  ✔  Q1 %.0f ms", ms)

    await _send(ws, {
        "type": "next_question",
        "text": question,
        "node": session.current_node,
    })


async def run_turn(ws, session: context.Session, transcript: str):
    """
    Full pipeline for one user turn.
    Two separate TTS calls keeps cached question phrases hitting <10ms.
    """
    t_turn = time.perf_counter()
    log.info("PIPE ▶  node=%-8s  %r", session.current_node, transcript[:70])

    # 1. Echo transcript
    await _send(ws, {"type": "transcript_final", "text": transcript})
    session.add_turn("user", transcript)

    # 2. Stream LLM ack
    t_llm = time.perf_counter()
    ack_text = ""
    async for token in llm_stream.stream_ack(transcript, session.chat_history):
        await _send(ws, {"type": "llm_token", "text": token})
        ack_text += token
    llm_ms = (time.perf_counter() - t_llm) * 1000
    log.info("LLM  ✔  %r  (%.0f ms)", ack_text[:60], llm_ms)

    # 3. Branch
    sentiment = survey_engine.detect_sentiment(transcript)
    next_nd   = survey_engine.next_node(session.current_node, transcript, sentiment)
    prev_nd   = session.current_node
    session.current_node = next_nd
    session.add_turn("assistant", ack_text, sentiment=sentiment)
    log.info("PIPE ✔  %s → %s  (sentiment=%s)", prev_nd, next_nd, sentiment)

    # 4. TTS — two calls: ack (miss) then question (hit)
    next_text = survey_engine.get_question(next_nd)
    is_done   = survey_engine.is_terminal(next_nd)
    t_tts = time.perf_counter()

    ack_clean = ack_text.strip()
    if ack_clean:
        ack_ms = await speak(ws, ack_clean)
        log.info("TTS  ✔  ack %.0f ms (cache miss expected)", ack_ms)

    q_ms = await speak(ws, next_text)
    log.info("TTS  ✔  question %.0f ms (cache %s)", q_ms, "HIT" if q_ms < 15 else "MISS")

    tts_ms    = (time.perf_counter() - t_tts) * 1000
    total_ms  = (time.perf_counter() - t_turn) * 1000

    # 5. Signal
    if is_done:
        await _send(ws, {"type": "survey_done"})
        log.info("PIPE ✔  survey complete  %.0f ms total", total_ms)
    else:
        await _send(ws, {
            "type": "next_question",
            "text": next_text,
            "node": next_nd,
            "latency": {
                "llm":   round(llm_ms),
                "tts":   round(tts_ms),
                "total": round(total_ms),
            },
        })
        log.info("PIPE ✔  turn done  %.0f ms  [LLM:%.0f TTS:%.0f]",
                 total_ms, llm_ms, tts_ms)