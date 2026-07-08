"""
main.py — FastAPI server for Voice Survey V1.

Endpoints:
  GET  /                → serves client/index.html
  POST /transcribe      → audio bytes → {text: str}
  POST /respond         → {transcript, history} → {response, next_question, done}
  POST /synthesise      → {text} → MP3 audio bytes
"""

import json
import logging
import os
import time
from pathlib import Path

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("survey")

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

load_dotenv()

# Local modules
from stt import transcribe
from llm import get_response
from tts import synthesise

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Voice Survey V1", version="1.0.0")

# ── Per-request total latency middleware ─────────────────────────────────────
@app.middleware("http")
async def log_total_latency(request: Request, call_next):
    if request.url.path in ("/transcribe", "/respond", "/synthesise"):
        t0 = time.perf_counter()
        response = await call_next(request)
        total_ms = (time.perf_counter() - t0) * 1000
        stage = request.url.path.lstrip("/").upper()
        log.info("%-12s TOTAL ⏱  %.0f ms  ──────────────────────", stage, total_ms)
        return response
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load survey questions once at startup
SURVEY_PATH = Path(__file__).parent / "survey.json"
with SURVEY_PATH.open() as f:
    SURVEY_DATA = json.load(f)
QUESTIONS: list[dict] = SURVEY_DATA["questions"]

# Path to the single-page UI
CLIENT_HTML = Path(__file__).parent.parent / "client" / "index.html"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    """Serve the single-page voice survey UI."""
    if not CLIENT_HTML.exists():
        raise HTTPException(status_code=404, detail="client/index.html not found.")
    return HTMLResponse(content=CLIENT_HTML.read_text(encoding="utf-8"))


@app.post("/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """
    Accept an audio file upload and return its transcript.

    Request : multipart/form-data with field `audio` (WAV / WebM / OGG …)
    Response: {"text": "<transcript string>"}
    """
    try:
        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file received.")

        # Guard against accidental micro-recordings (< 3 KB = less than ~0.1s of speech)
        MIN_AUDIO_BYTES = 3_000
        if len(audio_bytes) < MIN_AUDIO_BYTES:
            log.warning("STT  ⚠  audio too short (%.1f KB) — skipped", len(audio_bytes) / 1024)
            return {"text": ""}   # UI handles empty transcript gracefully

        mime = audio.content_type or "audio/wav"
        log.info("STT  ▶  received %.1f KB audio (%s)", len(audio_bytes) / 1024, mime)

        t0 = time.perf_counter()
        text = await transcribe(audio_bytes, mime_type=mime)
        stt_ms = (time.perf_counter() - t0) * 1000

        log.info('STT  ✔  transcript: "%s"', text[:80] + ("…" if len(text) > 80 else ""))
        log.info("STT  ⏱  %.0f ms", stt_ms)

        return {"text": text}

    except EnvironmentError as exc:
        log.error("STT  ✘  env error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except RuntimeError as exc:
        log.error("STT  ✘  runtime error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/respond")
async def respond(request: Request):
    """
    Accept the latest transcript + conversation history; return bot response.

    Request body (JSON):
        {
          "transcript": "<latest user utterance>",
          "history":    [{"role": "user"|"assistant", "content": "..."},  ...]
        }

    Response (JSON):
        {
          "response":      "<acknowledgement text>",
          "next_question": "<next question text, or empty string when done>",
          "done":          true | false
        }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    transcript: str = body.get("transcript", "").strip()
    history: list = body.get("history", [])

    if not transcript:
        raise HTTPException(status_code=400, detail="`transcript` field is required.")

    try:
        log.info('LLM  ▶  transcript: "%s"', transcript[:80] + ("…" if len(transcript) > 80 else ""))
        log.info("LLM  ▶  history length: %d turns", len(history))

        t0 = time.perf_counter()
        result = await get_response(transcript, history, QUESTIONS)
        llm_ms = (time.perf_counter() - t0) * 1000

        log.info('LLM  ✔  ack: "%s"', result["acknowledgement"][:60])
        log.info('LLM  ✔  next_q: "%s"', result["next_question"][:60])
        log.info("LLM  ✔  done: %s", result["done"])
        log.info("LLM  ⏱  %.0f ms", llm_ms)

        return {
            "response": result["acknowledgement"],
            "next_question": result["next_question"],
            "done": result["done"],
        }
    except EnvironmentError as exc:
        log.error("LLM  ✘  env error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except RuntimeError as exc:
        log.error("LLM  ✘  runtime error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/synthesise")
async def synthesise_speech(request: Request):
    """
    Accept a text string and return MP3 audio bytes.

    Request body (JSON): {"text": "<text to speak>"}
    Response: audio/mpeg binary stream
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    text: str = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="`text` field is required.")

    try:
        log.info('TTS  ▶  text (%d chars): "%s"', len(text), text[:60] + ("…" if len(text) > 60 else ""))

        t0 = time.perf_counter()
        mp3_bytes = await synthesise(text)
        tts_ms = (time.perf_counter() - t0) * 1000

        log.info("TTS  ✔  audio size: %.1f KB", len(mp3_bytes) / 1024)
        log.info("TTS  ⏱  %.0f ms", tts_ms)

        return Response(content=mp3_bytes, media_type="audio/mpeg")
    except EnvironmentError as exc:
        log.error("TTS  ✘  env error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except RuntimeError as exc:
        log.error("TTS  ✘  runtime error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Entry point (for direct `python main.py` usage during dev)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)