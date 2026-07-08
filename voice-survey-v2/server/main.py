"""
main.py — Voice Survey V2: FastAPI + WebSocket server.

Startup sequence:
  1. Load .env  (needs only GROQ_API_KEY — Deepgram no longer needed)
  2. Pre-warm TTS cache for all survey phrases
  3. Serve client/index.html at GET /
  4. Accept WebSocket connections at ws://localhost:8000/ws

Each WebSocket connection = one independent survey session.
"""
import base64
import json
import logging
import os
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
from survey_engine import ALL_PHRASES
from tts_stream import warm_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("survey")

CLIENT_HTML = Path(__file__).parent.parent / "client" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY not set — add it to .env")
    await warm_cache(ALL_PHRASES)
    log.info("Voice Survey V2 ready  →  http://localhost:8000")
    yield
    log.info("Shutting down.")


app = FastAPI(title="Voice Survey V2", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                  allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    if not CLIENT_HTML.exists():
        return HTMLResponse("<h1>client/index.html not found</h1>", status_code=404)
    return HTMLResponse(CLIENT_HTML.read_text(encoding="utf-8"))


@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()

    session_id = str(uuid.uuid4())
    session    = context.get_or_create(session_id)
    collector  = stt_stream.AudioCollector()

    log.info("WS   ▶  connected  session=%s", session_id[:8])

    async def send(payload: dict):
        await ws.send_text(json.dumps(payload))

    try:
        # Auto-start: speak intro + Q1 on connect
        await pipeline.run_session_start(ws, session)

        async for raw in ws.iter_text():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "start_session":
                session = context.reset(session_id)
                collector.reset()
                log.info("WS   ▶  session reset")
                await pipeline.run_session_start(ws, session)

            elif msg_type == "audio_chunk":
                raw_b64 = msg.get("data", "")
                if raw_b64:
                    collector.add(base64.b64decode(raw_b64))

            elif msg_type == "audio_end":
                audio_blob  = collector.assemble()
                had_header  = collector.has_header
                collector.reset()

                log.info("STT  ▶  %.1f KB  header=%s", len(audio_blob) / 1024, had_header)

                if not had_header or len(audio_blob) < stt_stream.MIN_AUDIO_BYTES:
                    await send({
                        "type":    "error",
                        "message": "Audio too short — hold the button and speak clearly.",
                    })
                    continue

                t0 = time.perf_counter()
                try:
                    transcript = await stt_stream.transcribe_blob(audio_blob, "audio/webm")
                except Exception as e:
                    log.error("STT  ✘  %s", e)
                    await send({"type": "error", "message": f"Transcription failed: {e}"})
                    continue

                stt_ms = (time.perf_counter() - t0) * 1000
                log.info("STT  ✔  %r  (%.0f ms)", transcript[:60], stt_ms)

                # Forward STT latency so client can show it
                await send({"type": "stt_done", "ms": round(stt_ms)})

                if not transcript:
                    await send({"type": "error", "message": "Couldn't hear you — please try again."})
                    continue

                try:
                    await pipeline.run_turn(ws, session, transcript)
                except Exception as e:
                    log.error("PIPE ✘  %s", e)
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