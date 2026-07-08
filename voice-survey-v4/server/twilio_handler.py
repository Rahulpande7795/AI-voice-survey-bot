"""
twilio_handler.py — Twilio Media Streams integration for Voice Survey V4.

Twilio phone audio format:
  - Encoding:     mulaw (G.711 u-law)
  - Sample rate:  8000 Hz
  - Channels:     1 (mono)
  - Frame size:   160 samples = 20ms per packet
  - Transport:    base64-encoded JSON over WebSocket

Whisper requires:
  - Encoding:     float32 PCM
  - Sample rate:  16000 Hz
  - Channels:     1 (mono)

Conversion chain (incoming — Twilio → Whisper):
  mulaw 8kHz bytes → int16 PCM 8kHz → upsample 16kHz → float32 [-1, 1]

Conversion chain (outgoing — edge-tts → Twilio):
  edge-tts MP3 bytes → ffmpeg/av decode → int16 PCM 8kHz → mulaw → base64

New routes added to main.py:
  POST /incoming-call   — TwiML webhook, tells Twilio to open Media Stream
  WS   /twilio-ws       — Media Streams WebSocket per call
"""

import asyncio
import base64
import io
import json
import logging
import time
import uuid

import numpy as np
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

import config
import context
import pipeline
import stt_stream

log = logging.getLogger("survey")


# ─────────────────────────────────────────────────────────────────────────────
# Audio conversion helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_audioop():
    """Return audioop — stdlib on Python ≤3.12, audioop-lts on 3.13+."""
    try:
        import audioop
        return audioop
    except ImportError:
        import audioop_lts as audioop  # pip install audioop-lts
        return audioop


def mulaw_to_float32_16k(mulaw_bytes: bytes) -> np.ndarray:
    """
    Convert Twilio mulaw 8 kHz bytes → float32 PCM 16 kHz numpy array.
    Steps:
      1. mulaw → int16 PCM at 8 kHz   (audioop.ulaw2lin)
      2. 8 kHz → 16 kHz               (audioop.ratecv — linear interpolation)
      3. int16 bytes → float32 array  (normalise to [-1.0, 1.0])
    """
    ao = _get_audioop()
    pcm_8k  = ao.ulaw2lin(mulaw_bytes, 2)                         # → int16 bytes @ 8kHz
    pcm_16k, _ = ao.ratecv(pcm_8k, 2, 1, 8000, 16000, None)     # → int16 bytes @ 16kHz
    arr = np.frombuffer(pcm_16k, dtype=np.int16)
    return arr.astype(np.float32) / 32768.0


def mp3_to_mulaw_8k(mp3_bytes: bytes) -> bytes:
    """
    Convert edge-tts MP3 output → raw mulaw 8 kHz bytes for Twilio.
    Uses PyAV (already a project dependency) — no extra install needed.
    Falls back to ffmpeg subprocess if av is unavailable.
    """
    # ── Try PyAV first (fastest, in-process) ─────────────────────────────
    try:
        import av
        ao = _get_audioop()

        container = av.open(io.BytesIO(mp3_bytes))
        resampler  = av.AudioResampler(format="s16", layout="mono", rate=8000)
        pcm_chunks: list[bytes] = []
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                pcm_chunks.append(bytes(rf.planes[0]))
        container.close()

        pcm_8k  = b"".join(pcm_chunks)
        mulaw   = ao.lin2ulaw(pcm_8k, 2)
        return mulaw
    except Exception as e_av:
        log.debug("TWILIO mp3→mulaw PyAV failed (%s) — trying ffmpeg", e_av)

    # ── Fallback: subprocess ffmpeg ───────────────────────────────────────
    import subprocess
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0",
             "-ar", "8000", "-ac", "1", "-f", "mulaw", "pipe:1"],
            input=mp3_bytes, capture_output=True, timeout=5
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception as e_ff:
        log.error("TWILIO mp3→mulaw ffmpeg also failed: %s", e_ff)

    return b""


# ─────────────────────────────────────────────────────────────────────────────
# TwiML webhook
# ─────────────────────────────────────────────────────────────────────────────

def get_incoming_call_twiml() -> str:
    """
    TwiML response instructing Twilio to open a Media Stream WebSocket.
    Called when someone dials the Twilio phone number.

    IMPORTANT: reads os.getenv("PUBLIC_URL") FRESH on every call.
    Do NOT use config.PUBLIC_URL here — config is frozen at import time
    and start_with_tunnel.py writes PUBLIC_URL to .env AFTER the server
    has already started. os.getenv() sees the live environment variable
    that was set in the parent process before uvicorn was spawned.
    """
    import os as _os
    # Fresh read — NOT config.PUBLIC_URL (which is frozen at import time)
    public_url = _os.getenv("PUBLIC_URL", "").rstrip("/")

    if not public_url:
        raise ValueError(
            "PUBLIC_URL environment variable is not set. "
            "Run start_with_tunnel.py to set it automatically, or "
            "set it manually: $env:PUBLIC_URL='https://your-url.ngrok-free.app'"
        )

    ws_url = (
        public_url
        .replace("https://", "wss://")
        .replace("http://",  "ws://")
    )
    ws_url = f"{ws_url}/twilio-ws"
    log.info("TWILIO TwiML ws_url=%s", ws_url)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'    <Connect>\n'
        f'        <Stream url="{ws_url}" />\n'
        f'    </Connect>\n'
        f'    <Pause length="60"/>\n'
        "</Response>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TTS → Twilio sender
# ─────────────────────────────────────────────────────────────────────────────

async def send_tts_to_twilio(ws: WebSocket, stream_sid: str, text: str) -> None:
    """
    Synthesise `text` via edge-tts (using the shared warm cache),
    convert MP3 → mulaw 8 kHz, and stream back to the Twilio caller.

    This is the drop-in replacement for pipeline._speak() used by all
    Twilio-aware pipeline functions.
    """
    if not text or not text.strip():
        return
    try:
        from tts_stream import synthesise
        mp3_bytes = await synthesise(text)
        if not mp3_bytes:
            log.warning("TWILIO TTS empty for: %r", text[:50])
            return

        # Convert in thread executor — CPU-bound
        loop     = asyncio.get_event_loop()
        mulaw    = await loop.run_in_executor(None, mp3_to_mulaw_8k, mp3_bytes)
        if not mulaw:
            return

        payload  = base64.b64encode(mulaw).decode("ascii")
        await ws.send_text(json.dumps({
            "event":     "media",
            "streamSid": stream_sid,
            "media":     {"payload": payload},
        }))
        log.info("TWILIO TTS ✔  %d mulaw bytes  text=%r",
                 len(mulaw), text[:40])
    except Exception as e:
        log.error("TWILIO TTS ✘  %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Per-call session state
# ─────────────────────────────────────────────────────────────────────────────

class TwilioSession:
    """
    Manages state for one active Twilio phone call.

    Accumulates mulaw audio packets from Twilio, converts each to float32
    PCM, runs Silero VAD on every frame, and fires STT + pipeline when
    end-of-speech is detected — exactly mirroring the browser flow but
    without any WebM container.
    """

    # Minimum accumulated PCM samples before VAD is allowed to fire
    _MIN_SPEECH_SAMPLES = 16_000 * 1  # 1 second of 16kHz audio

    def __init__(self, ws: WebSocket, stream_sid: str, session_id: str):
        self.ws         = ws
        self.stream_sid = stream_sid
        self.sid        = session_id
        self.session    = context.get_or_create(session_id)

        # PCM accumulator — list of float32 arrays, one per Twilio packet
        self._pcm_frames: list[np.ndarray] = []
        self._total_samples: int           = 0

        # Concurrency guard — same pattern as browser handler
        self._processed  = False
        self._turn_lock  = asyncio.Lock()

        # VAD state — uses TWILIO_VAD_* thresholds (phone audio is noisier)
        self._speech_frames  = 0
        self._silence_frames = 0
        self._in_speech      = False
        self._last_speech_t  = time.time()

        # Empty-transcript retry counter
        self._empty_count: int = 0

    # ── Survey start ──────────────────────────────────────────────────────

    async def start_survey(self) -> None:
        """Play intro + first question when the stream starts."""
        log.info("TWILIO ▶  survey start  session=%s", self.sid[:8])
        await pipeline.run_session_start_twilio(
            self.ws, self.stream_sid, self.session, send_tts_to_twilio
        )

    # ── Incoming audio ────────────────────────────────────────────────────

    async def handle_media(self, payload_b64: str) -> None:
        """
        Process one 20ms Twilio audio packet (160 mulaw bytes → ~320 float32 samples @ 16kHz).
        Accumulates PCM and checks VAD on every frame.
        """
        if self._processed:
            return

        try:
            mulaw_bytes = base64.b64decode(payload_b64)
            if len(mulaw_bytes) < 10:
                return

            pcm_chunk = mulaw_to_float32_16k(mulaw_bytes)
            self._pcm_frames.append(pcm_chunk)
            self._total_samples += len(pcm_chunk)

            # Run VAD on this chunk
            eos = self._vad_check(pcm_chunk)
            if eos and not self._processed:
                if self._turn_lock.locked():
                    log.debug("TWILIO VAD EOS ignored — turn already locked")
                    return
                self._processed  = True
                assembled        = np.concatenate(self._pcm_frames)
                self._pcm_frames = []
                self._total_samples = 0
                asyncio.create_task(self._run_turn(assembled))

        except Exception as e:
            log.error("TWILIO handle_media error: %s", e)

    def _vad_check(self, pcm_chunk: np.ndarray) -> bool:
        """
        Per-packet VAD using phone-specific thresholds (TWILIO_VAD_*).

        Phone audio (mulaw 8kHz upsampled to 16kHz) has a higher noise floor
        than clean browser mic audio. Using the browser VAD thresholds caused
        0.12s false end-of-speech triggers (only 3–4 silence frames).

        FIX: use TWILIO_VAD_SILENCE_FRAMES=12 (384ms), TWILIO_VAD_SPEECH_MIN_FRAMES=5,
        and RMS energy > TWILIO_VAD_ENERGY_THRESHOLD instead of silero probability
        (silero was calibrated for clean 16kHz mic audio, not upsampled mulaw).

        Also requires TWILIO_MIN_SPEECH_DURATION_S of detected speech before
        end-of-speech can fire — prevents triggering on background noise.
        """
        # Need minimum buffered audio before firing
        if self._total_samples < self._MIN_SPEECH_SAMPLES:
            return False

        # Use RMS energy for phone audio — more reliable than silero on upsampled mulaw
        rms = float(np.sqrt(np.mean(pcm_chunk ** 2))) if len(pcm_chunk) > 0 else 0.0
        is_speech = rms > config.TWILIO_VAD_ENERGY_THRESHOLD

        if is_speech:
            self._speech_frames  += 1
            self._silence_frames  = 0
            if self._speech_frames >= config.TWILIO_VAD_SPEECH_MIN_FRAMES:
                if not self._in_speech:
                    log.debug("TWILIO VAD speech-start rms=%.4f", rms)
                self._in_speech      = True
                self._last_speech_t  = time.time()
        elif self._in_speech:
            self._silence_frames += 1
            if self._silence_frames >= config.TWILIO_VAD_SILENCE_FRAMES:
                sil_s = time.time() - self._last_speech_t
                # Guard: only fire EOS if we had enough real speech
                speech_duration_s = self._speech_frames * 0.032  # 32ms per frame
                if speech_duration_s < config.TWILIO_MIN_SPEECH_DURATION_S:
                    log.warning(
                        "TWILIO VAD ignoring EOS — speech too short: %.2fs (need %.2fs)",
                        speech_duration_s, config.TWILIO_MIN_SPEECH_DURATION_S,
                    )
                    # Reset so we can accumulate more speech
                    self._speech_frames  = 0
                    self._silence_frames = 0
                    self._in_speech      = False
                    return False
                log.info(
                    "TWILIO VAD end-of-speech  silence=%.2fs  speech=%.2fs",
                    sil_s, speech_duration_s,
                )
                return True

        return False

    # ── Turn execution ────────────────────────────────────────────────────

    async def _run_turn(self, pcm_audio: np.ndarray) -> None:
        """STT → pipeline for one completed utterance."""
        async with self._turn_lock:
            try:
                t0 = time.perf_counter()
                transcript = await stt_stream.transcribe_pcm(pcm_audio)
                stt_ms     = (time.perf_counter() - t0) * 1000
                log.info("TWILIO STT ✔  %.0fms  %r", stt_ms, transcript[:60])

                if not transcript or not transcript.strip():
                    self._empty_count += 1
                    log.warning(
                        "TWILIO STT empty  count=%d/3  duration=%.2fs",
                        self._empty_count, len(pcm_audio) / 16000,
                    )
                    if self._empty_count >= 3:
                        log.error("TWILIO 3× consecutive empty transcripts — ending call")
                        await send_tts_to_twilio(
                            self.ws, self.stream_sid,
                            "I'm having trouble hearing you clearly. "
                            "Please call back when you're in a quieter area. "
                            "Thank you. Goodbye.",
                        )
                        await self._hangup()
                    else:
                        prompts = [
                            "Sorry, I didn't catch that. Could you please repeat?",
                            "I'm still having trouble hearing you. Please speak a bit louder.",
                        ]
                        await send_tts_to_twilio(
                            self.ws, self.stream_sid,
                            prompts[min(self._empty_count - 1, 1)],
                        )
                    return

                # Good transcript — reset empty counter
                self._empty_count = 0
                await pipeline.run_turn_twilio(
                    self.ws, self.stream_sid,
                    self.session, transcript,
                    send_tts_to_twilio,
                )
            except Exception as e:
                log.error("TWILIO _run_turn error: %s", e)
            finally:
                # Reset VAD state for next utterance
                self._speech_frames  = 0
                self._silence_frames = 0
                self._in_speech      = False
                self._processed      = False

    async def _hangup(self) -> None:
        """Gracefully close the WebSocket after final TTS plays."""
        try:
            await asyncio.sleep(4)  # let final TTS audio play out
            await self.ws.close()
        except Exception as e:
            log.debug("TWILIO hangup close: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI route handlers (imported and registered in main.py)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_incoming_call(request: Request) -> Response:
    """
    POST /incoming-call
    Twilio calls this webhook when someone dials the phone number.
    Returns TwiML that opens a Media Streams WebSocket to /twilio-ws.
    """
    form   = await request.form()
    caller = form.get("From", "unknown")
    log.info("TWILIO ▶  incoming call  from=%s", caller)
    try:
        twiml = get_incoming_call_twiml()
        log.info("TWILIO ✔  TwiML generated OK")
        return Response(content=twiml, media_type="application/xml")
    except ValueError as e:
        log.error("TWILIO ✘  TwiML error: %s", e)
        # Return safe TwiML so the caller hears something instead of a 500
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Response>'
                '<Say>System configuration error. Please try again later.</Say>'
                '</Response>'
            ),
            media_type="application/xml",
        )


async def handle_twilio_ws(ws: WebSocket) -> None:
    """
    WS /twilio-ws
    Twilio Media Streams WebSocket — one connection per phone call.
    Twilio sends JSON events: connected → start → media* → stop.
    Server sends JSON events: media (for TTS audio back to caller).
    """
    await ws.accept()
    log.info("TWILIO ▶  WebSocket connected")

    twilio_session: TwilioSession | None = None

    try:
        async for raw in ws.iter_text():
            try:
                data  = json.loads(raw)
                event = data.get("event", "")

                # ── Twilio handshake ──────────────────────────────────────
                if event == "connected":
                    log.info("TWILIO  connected  protocol=%s",
                             data.get("protocol", "?"))

                # ── Stream start — spin up session ────────────────────────
                elif event == "start":
                    start_data = data.get("start", {})
                    stream_sid = start_data.get("streamSid", "")
                    call_sid   = start_data.get("callSid",   "")
                    session_id = str(uuid.uuid4())

                    log.info("TWILIO ▶  stream start  streamSid=%.12s  callSid=%.12s",
                             stream_sid, call_sid)

                    twilio_session = TwilioSession(ws, stream_sid, session_id)
                    # Start survey asynchronously so we don't block the event loop
                    asyncio.create_task(twilio_session.start_survey())

                # ── Inbound audio ─────────────────────────────────────────
                elif event == "media":
                    if twilio_session:
                        payload = data.get("media", {}).get("payload", "")
                        if payload:
                            await twilio_session.handle_media(payload)

                # ── Call ended ────────────────────────────────────────────
                elif event == "stop":
                    log.info("TWILIO ✔  stream stop")
                    break

            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.error("TWILIO WS message error: %s", e, exc_info=True)

    except WebSocketDisconnect:
        log.info("TWILIO  WebSocket disconnected")
    except Exception as e:
        log.error("TWILIO WS fatal error: %s", e, exc_info=True)
