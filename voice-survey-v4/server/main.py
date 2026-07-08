"""
main.py — V4 final. Clean logging, atomic one-pipeline-per-recording guarantee.

KEY FIXES vs previous version:
  1. Logging configured BEFORE uvicorn starts — no handler conflict.
     Uses logging.getLogger("survey") with explicit StreamHandler.
  2. TRANSFORMERS_OFFLINE removed — whisper-tiny downloads on first run.
  3. _processed flag is atomically checked+set without any await in between.
  4. _start_new_recording() called on FIRST chunk (not on audio_end).
  5. All partial tasks cancelled on audio_end before processing.
  6. Double TTS warm log removed — single log in tts_stream.warm_cache().

STARTUP ORDER:
  1. load_model()     — whisper-tiny + whisper-small (~5s first run)
  2. semantic_cache.load() — MiniLM + FAISS AVX2 pre-load (~2s)
  3. warm_cache()     — edge-tts pre-synthesis for all phrases (~10s)
  4. server_ready     — sent to client on WS connect
  5. start_call       — received from client after button click → intro plays
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# ── Env vars: silence libraries, preserve TRANSFORMERS_OFFLINE off ──────
os.environ.setdefault("TQDM_DISABLE",                  "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM",        "false")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY",        "error")
os.environ.setdefault("HF_HUB_VERBOSITY",             "error")
# NOTE: TRANSFORMERS_OFFLINE is intentionally NOT set here.
# Setting it to "1" blocks whisper-tiny from downloading on first run.
# The semantic cache model uses local_files_only=True in cache.py instead.

import warnings
warnings.filterwarnings("ignore")

from dotenv import load_dotenv
load_dotenv()

# ── Logging — set up BEFORE uvicorn imports to avoid handler conflict ────
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
))
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.handlers.clear()
_root.addHandler(_handler)

# Silence noisy third-party loggers
for _lib in ("sentence_transformers", "transformers", "huggingface_hub",
             "filelock", "urllib3", "requests", "httpx", "httpcore",
             "faster_whisper", "watchfiles", "uvicorn.access"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

log = logging.getLogger("survey")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from twilio_handler import handle_incoming_call, handle_twilio_ws

import config
import context
import intent as intent_mod
import pipeline
import stt_stream
from cache import semantic_cache
from intent import ALL_ACK_TEXTS
from survey_engine import ALL_PHRASES
from tts_stream import warm_cache

CLIENT_HTML = Path(__file__).parent.parent / "client" / "index.html"

PARTIAL_INTERVAL   = 15_000   # bytes between partial ASR checks
PARTIAL_MIN_BYTES  = 5_000    # minimum buffer size before attempting partial
PARTIAL_CONFIDENCE = 0.95     # minimum intent confidence for partial fire


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY not set — add it to .env")

    loop = asyncio.get_event_loop()

    # 1. Load both Whisper models (tiny for partial, small for full)
    await loop.run_in_executor(None, stt_stream.load_model)

    # 2. Load FAISS + MiniLM semantic cache
    await semantic_cache.load()

    # 3. Pre-warm edge-tts for all phrases + intent ack texts
    all_warm = list(dict.fromkeys(ALL_PHRASES + ALL_ACK_TEXTS))
    await warm_cache(all_warm)

    log.info("V4 ready  →  http://localhost:8000  (%d phrases cached)", len(all_warm))
    yield

    stats = semantic_cache.stats
    log.info("CACHE FINAL  hits=%d  misses=%d  hit_rate=%s",
             stats["hits"], stats["misses"], stats["hit_rate"])
    log.info("Shutdown complete.")


app = FastAPI(title="Voice Survey V4 — L&T Finance", version="4.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                  allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    if not CLIENT_HTML.exists():
        return HTMLResponse("<h1>client/index.html not found</h1>", 404)
    return HTMLResponse(CLIENT_HTML.read_text(encoding="utf-8"))


@app.get("/stats")
async def get_stats():
    return {"cache": semantic_cache.stats}


@app.get("/health")
async def get_health():
    """Check server readiness — useful to confirm Whisper is loaded before Twilio calls."""
    from tts_stream import _cache as _tts_cache
    model_loaded = stt_stream.get_whisper_model() is not None
    return {
        "status":     "ready" if model_loaded else "loading",
        "whisper":    "loaded" if model_loaded else "not_loaded",
        "tts_cached": len(_tts_cache),
        "mode":       "cloud_stt" if config.USE_GROQ_WHISPER else "local_whisper",
    }


@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()

    sid          = str(uuid.uuid4())
    session      = context.get_or_create(sid)
    collector    = stt_stream.AudioCollector()
    call_started = False

    # ── Per-recording state ───────────────────────────────────────────────
    # _processed: True once any pipeline (partial or full) has fired.
    # Reset at start of each new recording.
    _processed:      bool      = False
    _turn_seq:       int       = 0
    _stt_stream:     stt_stream.StreamingSTT = stt_stream.StreamingSTT()
    _partial_tasks: set        = set()
    # FIX 3: per-session lock — only ONE turn can run at a time
    _turn_lock = asyncio.Lock()

    log.info("WS   ▶  connected  session=%s", sid[:8])

    async def send(payload: dict):
        await ws.send_text(json.dumps(payload))

    def _reset_stt():
        """Called to prepare for next user speech."""
        nonlocal _processed, _turn_seq, _stt_stream
        _processed = False
        _turn_seq += 1
        _stt_stream = stt_stream.StreamingSTT()
        for t in list(_partial_tasks):
            if not t.done(): t.cancel()
        _partial_tasks.clear()

    try:
        await send({"type": "server_ready"})
        log.info("WS   ▶  server_ready sent  session=%s", sid[:8])

        async for raw in ws.iter_text():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "start_call" and not call_started:
                call_started = True
                log.info("WS   ▶  start_call received")
                await pipeline.run_session_start(ws, session)

            elif msg_type == "start_session":
                session = context.reset(sid)
                call_started = True
                _reset_stt()
                await pipeline.run_session_start(ws, session)

            # ── audio_chunk (Real-time VAD) ───────────────────────────────
            elif msg_type == "audio_chunk":
                raw_b64 = msg.get("data", "")
                if not raw_b64 or _processed:
                    continue

                chunk = base64.b64decode(raw_b64)
                
                # Check for end-of-speech via VAD
                eos_detected = await _stt_stream.process_chunk(chunk)
                
                if eos_detected and not _processed:
                    _processed = True
                    log.info("VAD  ▶  End of speech detected, triggering pipeline")
                    blob = _stt_stream.collector.assemble()
                    
                    # Run full pipeline — guarded by turn lock to prevent race with audio_end
                    async def _execute_turn():
                        if _turn_lock.locked():
                            log.warning("PIPE  ⚠  turn already running (VAD) — skipping duplicate")
                            return
                        async with _turn_lock:
                            try:
                                t0 = time.perf_counter()
                                transcript = await stt_stream.transcribe_blob(blob)
                                stt_ms = (time.perf_counter() - t0) * 1000
                                
                                await send({"type": "stt_done", "ms": round(stt_ms)})
                                # NOTE: pipeline.run_turn() sends transcript_final itself — no duplicate here
                                
                                if transcript:
                                    await pipeline.run_turn(ws, session, transcript)
                                else:
                                    log.warning("VAD  ⚠  No speech in blob, resetting")
                            except Exception as e:
                                log.error("PIPE ✘  %s", e, exc_info=True)
                                await send({"type": "error", "message": f"Pipeline error: {e}"})
                            finally:
                                _reset_stt()

                    asyncio.create_task(_execute_turn())
                        
            # ── audio_end (Fallback/Legacy) ───────────────────────────────
            elif msg_type == "audio_end":
                if _processed:
                    # FIX 3: VAD already handled this turn — skip audio_end entirely
                    log.debug("WS   ▶  audio_end received but already processed (VAD handled it)")
                    continue
                
                # FIX 3: also skip if a turn is already running (lock held)
                if _turn_lock.locked():
                    log.warning("PIPE  ⚠  audio_end: turn already running — skipping duplicate")
                    continue
                
                blob = _stt_stream.collector.assemble()
                if len(blob) < stt_stream.MIN_AUDIO_BYTES:
                    continue
                    
                _processed = True
                log.info("WS   ▶  audio_end received (VAD missed or timeout)")
                
                async def _execute_turn_fallback():
                    async with _turn_lock:
                        try:
                            transcript = await stt_stream.transcribe_blob(blob)
                            if transcript:
                                await send({"type": "stt_done", "ms": 100})
                                await pipeline.run_turn(ws, session, transcript)
                            else:
                                await send({"type": "error", "message": "Couldn't hear you."})
                        except Exception as e:
                            log.error("PIPE ✘  %s", e, exc_info=True)
                        finally:
                            _reset_stt()

                asyncio.create_task(_execute_turn_fallback())

    except WebSocketDisconnect:
        log.info("WS   ✔  disconnected  session=%s", sid[:8])
    except Exception as e:
        log.error("WS   ✘  %s", e, exc_info=True)
        try:
            await send({"type": "error", "message": str(e)})
        except Exception:
            pass


# ── Twilio phone call routes ─────────────────────────────────────────────────

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """POST webhook — Twilio calls this when someone dials the number."""
    return await handle_incoming_call(request)


@app.websocket("/twilio-ws")
async def twilio_ws(ws: WebSocket):
    """WebSocket — Twilio Media Streams connection per phone call."""
    await handle_twilio_ws(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)