# 🎙 AI Voice Survey V4 — Twilio + Local Whisper

> Real-time Hindi/Hinglish payment verification bot for L&T Finance.
> V4 adds local faster-whisper STT, Silero VAD, edge-tts, and Twilio Media Streams for real outbound phone calls.

---

## What Changed in V4

V3 worked well in the browser but still relied on Groq cloud for STT (300–700ms India→US round-trip) and gTTS for speech output. V4 addresses both:

| Component | V3 | V4 |
|-----------|----|----|
| STT | Groq Whisper (cloud, 300–700ms) | faster-whisper tiny (local CPU, <50ms) |
| VAD | Energy threshold | Silero-VAD (neural, 512-sample frames) |
| TTS | gTTS (Google, ~200ms) | edge-tts (Microsoft Neural, en-IN-NeerjaNeural) |
| Phone calls | Browser only | Twilio Media Streams (real outbound calls) |
| TTS cache | 13 phrases | 68 phrases (pre-warmed at startup) |
| Decision latency | ~400ms (intent path) | ~300ms (intent path, no STT network hop) |

---

## Architecture

### Decision pipeline (per turn)

```
User speaks → VAD detects end-of-speech → faster-whisper STT
      │
      ├─ Layer 1: extractor.py    always runs · extracts date/amount/method/payer
      ├─ Layer 2: intent.py       regex classifier · <1ms · ~80% of turns
      ├─ Layer 3: cache.py        FAISS + exact-match · ~3ms · ~15% of turns
      └─ Layer 4: llm_stream.py   Groq Llama fallback · ~1300ms · ~5% of turns
```

### Twilio call flow

```
Caller dials +18782830614
    │
    ▼ Twilio POST → /incoming-call
Server returns TwiML (connects WebSocket to /twilio-ws)
    │
    ▼ Twilio opens WebSocket
Server plays intro + first question (edge-tts → mulaw → Twilio)
    │
    ▼ Caller speaks
mulaw 8kHz packets → VAD → upsample to 16kHz → faster-whisper
    │
    ▼ Transcript
intent/cache/LLM pipeline → edge-tts → mulaw → Twilio → caller hears response
```

### Audio format conversion (Twilio path)

- **Incoming:** mulaw G.711 8kHz → `audioop.ulaw2lin()` → int16 PCM → upsample → float32 16kHz → Whisper
- **Outgoing:** edge-tts MP3 → pydub decode → int16 8kHz → `audioop.lin2ulaw()` → base64 JSON → Twilio

---

## Performance (measured)

> faster-whisper runs locally — no India→US STT round-trip

| Turn type | Pipeline | Notes |
|-----------|----------|-------|
| Intent hit (~80%) | 12–23ms | no network call at all |
| Cache hit (~15%) | 3–5ms | FAISS lookup only |
| LLM fallback (~5%) | ~1300ms | Groq round-trip from India |

**TTS cache:** 68 phrases pre-warmed at startup → ack latency 3–5ms (was up to 1650ms in V3 for dynamic phrases)

---

## Survey Call Script (12 nodes)

```
INTRO
  └─ AVAILABILITY ──── no ──→ CALLBACK → CLOSE
          │ yes
          ▼
       PURPOSE
          │
    PAYMENT_CHECK ──── no ──→ REASON → CAPTURE_EXEC → CLOSE
          │ yes
          ▼
       WHO_PAID ─── other ──→ CAPTURE_PAYER
          │ self                    │
          └────────────────────────→ DATE → METHOD → AMOUNT → CLOSE
```

**Structured data extracted per call:**

| Node | Field | Example |
|------|-------|---------|
| `DATE` | `payment_date` | `"15/04"` |
| `AMOUNT` | `payment_amount` | `5000` |
| `METHOD` | `payment_method` | `"upi"` |
| `WHO_PAID` | `payer` | `"self"` / `"spouse"` |
| `REASON` | `nonpayment_reason` | `"financial_hardship"` |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| ASR | faster-whisper tiny (CPU int8) |
| VAD | silero-vad (512-sample frame chunking) |
| LLM | Groq llama-3.1-8b-instant (streaming) |
| TTS | edge-tts en-IN-NeerjaNeural (68-phrase LRU cache) |
| Semantic cache | faiss-cpu + all-MiniLM-L6-v2 (threshold 0.75) |
| Backend | FastAPI + uvicorn (WebSocket, asyncio) |
| Phone | Twilio Media Streams |
| Tunnel | pyngrok (static ngrok domain) |

---

## File Structure

```
voice-survey-v4/
├── start_with_tunnel.py        ← one-command launcher (ngrok + server)
├── .env.example
├── requirements.txt
├── ARCHITECTURE.md             ← detailed system design and config notes
├── BUG_FIX_REPORT.md           ← 6 bugs fixed in this version
├── FIXES_REPORT.md             ← browser-side fixes (VAD, race conditions, PCM)
├── TWILIO_FIXES_REPORT.md      ← Twilio integration fixes
├── TWILIO_SETUP.md             ← step-by-step Twilio Console guide
├── PHONE_TEST_REPORT.md        ← integration test results
└── server/
    ├── main.py                 ← FastAPI app, WebSocket routes, /health endpoint
    ├── pipeline.py             ← survey orchestrator, run_turn()
    ├── stt_stream.py           ← faster-whisper + Silero VAD
    ├── tts_stream.py           ← edge-tts, 68-phrase LRU cache
    ├── cache.py                ← FAISS semantic cache + exact-match dict
    ├── intent.py               ← regex intent classifier
    ├── extractor.py            ← structured field extraction
    ├── llm_stream.py           ← Groq streaming
    ├── survey_engine.py        ← 12-node L&T Finance call script
    ├── context.py              ← per-session state
    ├── config.py               ← all model/VAD/cache config
    ├── twilio_handler.py       ← Twilio Media Streams handler
    └── make_call.py            ← trigger an outbound call
```

---

## Setup

### Prerequisites
- Python 3.10+
- [Groq API key](https://console.groq.com) (free tier sufficient)
- Twilio account with a phone number (for phone calls)
- ngrok account (free tier, static domain optional)
- No GPU required

### Install

```bash
cd voice-survey-v4

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Fill in `.env`:
```env
GROQ_API_KEY=gsk_your_key_here

# Twilio (required for phone calls)
TWILIO_ACCOUNT_SID=your_sid
TWILIO_AUTH_TOKEN=your_token
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx

# ngrok
NGROK_AUTH_TOKEN=your_ngrok_token
NGROK_STATIC_DOMAIN=your-static-domain.ngrok-free.dev   # optional but recommended
```

### Run (browser mode)

```bash
cd server
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` in Chrome/Edge.

### Run (with Twilio phone calls)

```bash
# From project root — starts ngrok tunnel + server together
python start_with_tunnel.py
```

Wait for all four startup lines before testing:
```
STT  ✔  whisper-tiny ready
CACHE ✔  model loaded  threshold=0.75
TTS  ✔  cache warm — 68 phrases ready
V4 ready  →  http://localhost:8000
```

Then set the Twilio Console webhook to the ngrok URL printed in the terminal. See `TWILIO_SETUP.md` for the full guide.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | — | **Required** for LLM fallback |
| `CACHE_THRESHOLD` | `0.75` | Semantic similarity cutoff (0.0–1.0) |
| `TWILIO_ACCOUNT_SID` | — | Required for phone calls |
| `TWILIO_AUTH_TOKEN` | — | Required for phone calls |
| `TWILIO_PHONE_NUMBER` | — | Your Twilio number |
| `NGROK_AUTH_TOKEN` | — | Required for `start_with_tunnel.py` |
| `NGROK_STATIC_DOMAIN` | — | Optional — prevents URL changing on restart |

---

## Startup Log (expected)

```
STT  ✔  silero-vad ready  883ms
STT  ✔  whisper-tiny ready  17070ms
STT  ✔  model pre-warmed
CACHE ✔  model loaded  all-MiniLM-L6-v2  threshold=0.75
TTS  ▶  warming 68 phrases  voice=en-IN-NeerjaNeural…
TTS  ✔  cache warm — 68 phrases ready  (2049 KB total)
V4 ready  →  http://localhost:8000
```

Model warmup takes ~1 minute on first run. Subsequent runs are faster since faster-whisper caches weights to disk.

---

## Key Bugs Fixed in V4

Full details in `BUG_FIX_REPORT.md` and `FIXES_REPORT.md`. Short list:

- **Silero VAD 512-sample frame fix** — was passing 16000 samples, caused `ValueError` on every call, fell back silently to energy VAD
- **Race condition: VAD + audio_end double-firing** — added `asyncio.Lock()` per session
- **AMOUNT/DATE/METHOD TTS spike** — dynamic f-string acks (1650ms) replaced with static cached phrases (3–5ms)
- **Raw PCM odd-byte crash** — `np.frombuffer` on odd-length byte array, fixed by trimming to even
- **Whisper hallucination on silence** — added `_is_hallucination()` filter + `no_speech_threshold=0.6`
- **Twilio `PUBLIC_URL` stale read** — `config.py` frozen at import time, fixed to `os.getenv()` per request

---

## Packages

```
fastapi · uvicorn[standard] · groq · edge-tts · faster-whisper
faiss-cpu · sentence-transformers · numpy · torch · torchaudio
PyAV · pydub · python-dotenv · websockets · soundfile
twilio · pyngrok · audioop-lts
```

---

*Built by Rahul Pande · Vocab-AI Internship · 2025–2026*