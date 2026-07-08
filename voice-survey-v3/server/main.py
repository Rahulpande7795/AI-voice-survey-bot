"""
main.py — V3 FastAPI + WebSocket server.

FIXES IN THIS REVISION:
  1. server_ready sent immediately on WS accept — button enables instantly.
  2. Intro/Q1 audio only plays AFTER client sends start_call (autoplay fix).
  3. HuggingFace startup HTTP spam eliminated — offline mode after first download.
  4. All third-party loggers silenced at os.environ level before any imports.
  5. Duplicate TTS warm log removed (was calling warm_cache twice).
"""

# ── Silence everything BEFORE any imports ─────────────────────────────────
import os
os.environ["TQDM_DISABLE"]                 = "1"
os.environ["TOKENIZERS_PARALLELISM"]       = "false"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"]= "1"
os.environ["TRANSFORMERS_VERBOSITY"]       = "error"
os.environ["HF_HUB_VERBOSITY"]            = "error"
# After first download, skip ALL network checks → startup ~2s instead of ~12s
# Remove this line only if you want to force a model update check.
os.environ["TRANSFORMERS_OFFLINE"]         = "1"
os.environ["HF_DATASETS_OFFLINE"]          = "1"

import warnings
warnings.filterwarnings("ignore")

import base64
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import context
import pipeline
import stt_stream
from cache import semantic_cache
from intent import ALL_ACK_TEXTS
from survey_engine import ALL_PHRASES
from tts_stream import warm_cache

# ── Logging setup — silence noisy libs ───────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-12s %(message)s",
    datefmt="%H:%M:%S",
)
for _lib in (
    "sentence_transformers", "transformers", "huggingface_hub",
    "filelock", "urllib3", "requests", "httpx", "httpcore",
    "hf_transfer", "fsspec",
):
    logging.getLogger(_lib).setLevel(logging.ERROR)

log = logging.getLogger("survey")

CLIENT_HTML = Path(__file__).parent.parent / "client" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY not set — add it to .env")

    # 1. Load semantic cache model
    #    First run: downloads model (~22MB, ~10s)
    #    Subsequent runs: loads from disk (~2s) because TRANSFORMERS_OFFLINE=1
    await semantic_cache.load()

    # 2. Pre-warm TTS for survey phrases + all intent ack texts
    #    This is ONE call — previously it was being called twice (bug fixed)
    all_warm = list(dict.fromkeys(ALL_PHRASES + ALL_ACK_TEXTS))
    log.info("TTS  ▶  warming %d phrases (survey + ack texts)…", len(all_warm))
    await warm_cache(all_warm)

    log.info("V3 ready  →  http://localhost:8000  (%d phrases cached)", len(all_warm))
    yield

    stats = semantic_cache.stats
    log.info("CACHE FINAL  hits=%d  misses=%d  hit_rate=%s",
             stats["hits"], stats["misses"], stats["hit_rate"])
    log.info("Shutdown complete.")


app = FastAPI(title="Voice Survey V3 — L&T Finance", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    if not CLIENT_HTML.exists():
        return HTMLResponse("<h1>client/index.html not found</h1>", status_code=404)
    return HTMLResponse(CLIENT_HTML.read_text(encoding="utf-8"))


@app.get("/stats")
async def cache_stats():
    return {"cache": semantic_cache.stats}


@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()

    session_id = str(uuid.uuid4())
    session    = context.get_or_create(session_id)
    collector  = stt_stream.AudioCollector()
    call_started = False   # True after client sends start_call

    log.info("WS   ▶  connected  session=%s", session_id[:8])

    async def send(payload: dict):
        await ws.send_text(json.dumps(payload))

    try:
        # ── FIX: send server_ready IMMEDIATELY so the button enables ──────
        # Do NOT play audio here — browser will block it (autoplay policy).
        # The client shows "Start Call" button; clicking it sends start_call.
        await send({"type": "server_ready"})
        log.info("WS   ▶  server_ready sent  session=%s", session_id[:8])

        async for raw in ws.iter_text():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            # ── Client clicked "Start Call" → NOW play audio ──────────────
            if msg_type == "start_call" and not call_started:
                call_started = True
                log.info("WS   ▶  start_call received — playing intro")
                await pipeline.run_session_start(ws, session)

            elif msg_type == "start_session":
                # Restart button pressed
                session      = context.reset(session_id)
                collector.reset()
                call_started = True
                await pipeline.run_session_start(ws, session)

            elif msg_type == "audio_chunk":
                raw_b64 = msg.get("data", "")
                if raw_b64:
                    try:
                        collector.add(base64.b64decode(raw_b64))
                    except Exception:
                        pass

            elif msg_type == "audio_end":
                blob       = collector.assemble()
                had_header = collector.has_header
                collector.reset()

                log.info("STT  ▶  %.1f KB  header=%s", len(blob)/1024, had_header)

                if not had_header or len(blob) < stt_stream.MIN_AUDIO_BYTES:
                    await send({"type": "error",
                                "message": "Audio too short — hold the button and speak clearly."})
                    continue

                t0 = time.perf_counter()
                try:
                    transcript = await stt_stream.transcribe_blob(blob)
                except Exception as e:
                    log.error("STT  ✘  %s", e)
                    await send({"type": "error", "message": f"Transcription failed: {e}"})
                    continue

                stt_ms = (time.perf_counter() - t0) * 1000
                log.info("STT  ✔  %r  (%.0f ms)", transcript[:60], stt_ms)
                await send({"type": "stt_done", "ms": round(stt_ms)})

                if not transcript:
                    await send({"type": "error",
                                "message": "Couldn't hear you — please try again."})
                    continue

                try:
                    await pipeline.run_turn(ws, session, transcript)
                except Exception as e:
                    log.error("PIPE ✘  %s", e, exc_info=True)
                    await send({"type": "error", "message": f"Pipeline error: {e}"})

    except WebSocketDisconnect:
        log.info("WS   ✔  disconnected  session=%s", session_id[:8])
    except Exception as e:
        log.error("WS   ✘  %s", e)
        try:
            await send({"type": "error", "message": str(e)})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)