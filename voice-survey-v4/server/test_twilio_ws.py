"""
test_twilio_ws.py — Local integration test for the Twilio Media Streams pipeline.

Simulates a full Twilio phone call WebSocket session:
  1. Sends "connected" + "start" events (as Twilio would)
  2. Waits for bot intro audio (mulaw base64 media events)
  3. Sends a simulated user response (silence + tone WAV → mulaw)
  4. Waits for bot reply audio
  5. Sends "stop" and reports PASS / FAIL

Run with the server already started on port 8000:
    python server/start_with_tunnel.py   (in one terminal)
    python server/test_twilio_ws.py       (in another)

Or run server directly first:
    cd server && python -m uvicorn main:app --port 8000
"""
import asyncio
import base64
import json
import struct
import time
import wave
import io
import sys
import os

# ── websockets library check ─────────────────────────────────────────────────
try:
    import websockets
except ImportError:
    print("Installing websockets for test client…")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "websockets"], check=True)
    import websockets

# ── audioop (stdlib or audioop-lts) ─────────────────────────────────────────
try:
    import audioop
except ImportError:
    import audioop_lts as audioop

SERVER_URL       = "ws://localhost:8000/twilio-ws"
FAKE_STREAM_SID  = "MZtest00000000000000000000000000"
FAKE_CALL_SID    = "CAtest00000000000000000000000000"


# ─────────────────────────────────────────────────────────────────────────────
# Generate synthetic mulaw audio (1 second of 500Hz tone at 8kHz)
# Used in absence of a real WAV file — no file dependency needed.
# ─────────────────────────────────────────────────────────────────────────────

def _make_tone_mulaw_chunks(duration_s: float = 1.5,
                             freq_hz: float = 500.0,
                             sample_rate: int = 8000,
                             chunk_samples: int = 160) -> list[str]:
    """
    Generate a pure-tone as mulaw 8kHz base64 chunks (20ms each).
    Produces convincing "speech-like" audio for VAD purposes.
    (Real speech from a WAV file works better — see wav_to_mulaw_chunks below.)
    """
    import math
    n_samples = int(duration_s * sample_rate)
    # Build int16 PCM samples
    pcm_int16 = bytearray()
    for i in range(n_samples):
        val = int(16000 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        pcm_int16 += struct.pack("<h", val)

    mulaw = audioop.lin2ulaw(bytes(pcm_int16), 2)

    chunks = []
    for i in range(0, len(mulaw), chunk_samples):
        chunk = mulaw[i:i + chunk_samples]
        chunks.append(base64.b64encode(chunk).decode("ascii"))
    return chunks


def wav_to_mulaw_chunks(wav_path: str, chunk_samples: int = 160) -> list[str]:
    """
    Read a WAV file and return mulaw 8kHz base64 chunks.
    The WAV file can be any sample rate — it will be resampled.
    """
    with wave.open(wav_path, "rb") as wf:
        orig_rate = wf.getframerate()
        raw_pcm   = wf.readframes(wf.getnframes())

    # Ensure 16-bit mono
    if wf.getsampwidth() != 2:
        raise ValueError("WAV must be 16-bit PCM")

    # Resample to 8kHz if needed
    if orig_rate != 8000:
        raw_pcm, _ = audioop.ratecv(raw_pcm, 2, 1, orig_rate, 8000, None)

    mulaw  = audioop.lin2ulaw(raw_pcm, 2)
    chunks = []
    for i in range(0, len(mulaw), chunk_samples):
        chunks.append(base64.b64encode(mulaw[i:i + chunk_samples]).decode("ascii"))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Main test
# ─────────────────────────────────────────────────────────────────────────────

async def run_test():
    print(f"Connecting to {SERVER_URL}…")

    try:
        async with websockets.connect(SERVER_URL, ping_interval=None) as ws:

            # ── Step 1: Send Twilio handshake ──────────────────────────────
            await ws.send(json.dumps({"event": "connected", "protocol": "Call"}))

            await ws.send(json.dumps({
                "event": "start",
                "start": {
                    "streamSid": FAKE_STREAM_SID,
                    "callSid":   FAKE_CALL_SID,
                    "tracks":    ["inbound"],
                    "customParameters": {},
                },
            }))
            print("✔ Sent: connected + start events")

            # ── Step 2: Wait for bot intro audio ───────────────────────────
            print("Waiting for bot intro audio (up to 60s for model warmup)…")
            intro_received = False
            deadline = time.perf_counter() + 60.0
            while time.perf_counter() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(raw)
                    if data.get("event") == "media":
                        if not intro_received:
                            print("✅ Bot intro audio received!")
                            intro_received = True
                except asyncio.TimeoutError:
                    if intro_received:
                        break   # silence after intro = done
                    print("   … still waiting for intro …", end="\r")

            if not intro_received:
                print("❌ FAIL: bot never sent intro audio")
                await ws.send(json.dumps({"event": "stop"}))
                return False

            await asyncio.sleep(0.5)   # mimic human reaction time

            # ── Step 3: Send simulated user speech ─────────────────────────
            # Try a real WAV file first, fall back to synthetic tone
            test_wav = os.path.join(os.path.dirname(__file__), "test_audio", "yes_available.wav")
            if os.path.exists(test_wav):
                print(f"Sending user speech from WAV: {test_wav}")
                chunks = wav_to_mulaw_chunks(test_wav)
            else:
                print("Sending synthetic 500Hz tone (1.5s) as user speech…")
                chunks = _make_tone_mulaw_chunks(duration_s=1.5)

            t_send = time.perf_counter()
            for chunk_b64 in chunks:
                await ws.send(json.dumps({
                    "event": "media",
                    "media": {"payload": chunk_b64},
                }))
                await asyncio.sleep(0.02)   # 20ms real-time pacing

            print(f"✔ Sent {len(chunks)} audio chunks ({len(chunks) * 20}ms of speech)")

            # ── Step 4: Wait for bot response ──────────────────────────────
            print("Waiting for bot response audio (up to 30s)…")
            response_received = False
            ttfa_ms = 0.0
            deadline = time.perf_counter() + 30.0
            while time.perf_counter() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(raw)
                    if data.get("event") == "media":
                        if not response_received:
                            ttfa_ms = (time.perf_counter() - t_send) * 1000
                            print(f"✅ Bot response received! TTFA = {ttfa_ms:.0f}ms")
                            response_received = True
                except asyncio.TimeoutError:
                    if response_received:
                        break

            # ── Step 5: Send stop ───────────────────────────────────────────
            await ws.send(json.dumps({"event": "stop"}))
            print()

            # ── Result ──────────────────────────────────────────────────────
            if intro_received and response_received:
                print("=" * 50)
                print("✅ TWILIO INTEGRATION TEST PASSED")
                print(f"   TTFA: {ttfa_ms:.0f}ms")
                print("=" * 50)
                return True
            else:
                print("=" * 50)
                print("❌ TWILIO INTEGRATION TEST FAILED")
                if not intro_received:
                    print("   • Bot never sent intro audio")
                if not response_received:
                    print("   • Bot never responded to user speech")
                print("=" * 50)
                return False

    except ConnectionRefusedError:
        print("❌ Cannot connect — is the server running on port 8000?")
        print("   Run: cd server && python -m uvicorn main:app --port 8000")
        return False
    except Exception as e:
        print(f"❌ Test error: {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    ok = asyncio.run(run_test())
    sys.exit(0 if ok else 1)
