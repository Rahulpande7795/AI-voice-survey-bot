# 🎙 AI Voice Survey Bot — L&T Finance

A real-time Hindi/Hinglish voice bot built for automated payment verification calls at L&T Finance. The bot calls customers, asks about their EMI payment status, extracts structured data (date, amount, payment method, payer), and routes through a 12-node call script.

Built iteratively over 4 versions during my internship — starting from a 6.5s REST pipeline and ending at a <300ms Twilio-integrated system that runs on real phone calls.

---

## The Problem

L&T Finance needed to automate outbound payment verification calls in Hindi. A human agent would call a customer, ask a series of structured questions, and log the responses. The goal was to replicate this with an AI voice bot that:

- Speaks natural Hindi to customers
- Understands spoken responses (including variations, accents, Hindi/English mix)
- Follows a branching call script depending on answers
- Extracts and logs structured data at the end of each call

The engineering challenge: doing all of this fast enough to feel like a real conversation. Early versions took 6+ seconds per response turn. The final version handles structured responses in under 300ms.

---

## Latency Evolution

> Measured from India → Groq US servers, no GPU

| Version | What it introduced | Turn latency |
|---------|-------------------|-------------|
| **V1** | REST pipeline, Deepgram STT, GPT-4o, ElevenLabs TTS | 5000–6500ms |
| **V2** | WebSocket, Groq Whisper STT, streaming LLM, gTTS cache | 1800–3000ms |
| **V3** | Intent classifier + FAISS semantic cache + Hindi L&T script | ~400ms (intent path) |
| **V4** | Local faster-whisper + Silero VAD + edge-tts + Twilio phone calls | ~300ms (intent path) |

The 16× improvement from V1 → V4 on structured responses (yes/no/numeric) comes from three compounding decisions: switching from cloud to local STT, adding a regex classifier that bypasses the LLM for ~80% of turns, and pre-warming a 68-phrase TTS cache so audio synthesis costs 3–5ms instead of 200–1650ms.

---

## Architecture (V4 — current)

### Decision pipeline per turn

```
User speaks
    │
    ▼ Silero VAD detects end-of-speech
    │
    ▼ faster-whisper (local CPU, <50ms)
    │
    ├─ Layer 1: extractor.py    always runs · extracts date/amount/method/payer from text
    ├─ Layer 2: intent.py       regex classifier · <1ms · handles ~80% of turns
    ├─ Layer 3: cache.py        FAISS + exact-match · ~3ms · handles ~15% of turns
    └─ Layer 4: llm_stream.py   Groq Llama 3.1 8B · ~1300ms · only ~5% of turns
```

The key insight: for a payment verification call, most answers are predictable — "haan", "nahi", "UPI se", "15 tarikh ko". The regex classifier catches all of these in under 1ms with zero network cost. The LLM only runs for genuinely free-text responses like reasons for non-payment.

### L&T Finance call script (12 nodes)

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

### Two delivery channels

**Browser** — works at `http://localhost:8000`. Useful for demos, testing, and the early versions.

**Phone (Twilio)** — real outbound calls via Twilio Media Streams. The server bridges mulaw 8kHz audio from Twilio into the same STT → intent/cache/LLM → TTS pipeline, then re-encodes the response back to mulaw for Twilio to deliver to the caller.

```
Caller dials → Twilio POST → /incoming-call → TwiML
    │
    ▼ Twilio opens WebSocket to /twilio-ws
    │
mulaw 8kHz audio packets
    │
    ▼ audioop.ulaw2lin() → upsample to 16kHz → faster-whisper
    │
    ▼ intent/cache/LLM pipeline
    │
    ▼ edge-tts MP3 → pydub decode → int16 8kHz → audioop.lin2ulaw() → Twilio
```

---

## Version Breakdown

### V1 — Proof of Concept
**[`voice-survey-v1/`](./voice-survey-v1)**

```
Browser → REST → Deepgram STT → REST → GPT-4o → REST → ElevenLabs TTS → Browser
```

Three sequential REST calls per turn. Deepgram from India alone took 2.5–4s. The goal was just to prove the full pipeline worked end-to-end — 5 fixed questions, no branching, English only.

Stack: Deepgram Nova-2 · GPT-4o · ElevenLabs · FastAPI REST

---

### V2 — WebSocket + Streaming
**[`voice-survey-v2/`](./voice-survey-v2)**

Replaced the three REST calls with one persistent WebSocket connection. Switched Deepgram → Groq Whisper (same API key as the LLM, 216× real-time speed, 300–700ms from India vs 2.5–4s). Added LLM token streaming so responses appear on screen as they generate. Added a dynamic branching survey tree.

Key bug fixed: browser `MediaRecorder` emits the WebM EBML header (`1A 45 DF A3`) only once — on session reconnect, subsequent recordings send headerless blobs that every STT API rejects with `400`. `AudioCollector` scans each chunk for the magic bytes and drops pre-header data silently.

Stack: Groq Whisper v3-Turbo · Groq Llama 3.1 8B · gTTS (cached) · FastAPI WebSocket

---

### V3 — Intent Cache + Hindi + L&T Finance
**[`voice-survey-v3/`](./voice-survey-v3)**

The first version built specifically for L&T Finance. Replaced the generic survey with a 12-node Hindi payment verification call script. Added two layers before the LLM: a local regex intent classifier (<1ms, handles yes/no/numeric/UPI/dates) and a FAISS semantic cache using `all-MiniLM-L6-v2` embeddings (catches paraphrases like "haan bilkul" = "ji haan" at cosine similarity 0.85).

Also added sentence-level TTS streaming — instead of waiting for the full LLM response before synthesising audio, TTS fires as an `asyncio.Task` at each sentence boundary. First audio arrives 200–400ms earlier.

Stack: Groq Whisper (Hindi) · Groq Llama 3.1 8B · FAISS · sentence-transformers · gTTS (Hindi) · FastAPI WebSocket

---

### V4 — Twilio + Local Whisper (current)
**[`voice-survey-v4/`](./voice-survey-v4)**

Replaced cloud STT with local `faster-whisper tiny` (CPU int8) — eliminates the India→US STT round-trip entirely. Replaced energy VAD with Silero-VAD (neural, 512-sample frame chunking). Replaced gTTS with `edge-tts` (Microsoft Neural, `en-IN-NeerjaNeural`). Added Twilio Media Streams integration for real outbound phone calls.

Notable bugs fixed in V4: Silero-VAD was silently falling back to energy VAD because the code passed 16000-sample windows instead of 512-sample frames; a race condition between VAD and `audio_end` was firing the pipeline twice per turn; dynamic f-string TTS acks were causing 1650ms spikes that are now replaced with static cached phrases.

Stack: faster-whisper tiny · Silero-VAD · Groq Llama 3.1 8B · FAISS · edge-tts · Twilio Media Streams · FastAPI WebSocket

---

## Repo Structure

```
ai-voice-survey-bot/
├── voice-survey-v1/          ← REST proof of concept
│   ├── server/               (main.py, stt.py, llm.py, tts.py)
│   ├── client/index.html
│   └── README.md
│
├── voice-survey-v2/          ← WebSocket + streaming LLM
│   ├── server/               (main.py, pipeline.py, stt_stream.py, ...)
│   ├── client/index.html
│   └── README.md
│
├── voice-survey-v3/          ← Intent cache + Hindi + L&T Finance
│   ├── server/               (+ intent.py, cache.py, extractor.py)
│   ├── client/index.html
│   ├── .env.example
│   └── README.md
│
└── voice-survey-v4/          ← Twilio + local Whisper (production)
    ├── server/               (+ twilio_handler.py, config.py, make_call.py)
    ├── client/index.html
    ├── start_with_tunnel.py  ← one-command launcher (ngrok + server)
    ├── ARCHITECTURE.md       ← detailed system design and config notes
    ├── BUG_FIX_REPORT.md
    ├── FIXES_REPORT.md
    ├── TWILIO_FIXES_REPORT.md
    ├── TWILIO_SETUP.md
    ├── PHONE_TEST_REPORT.md
    ├── .env.example
    └── README.md
```

Each version folder has its own README with setup instructions and architecture details.

---

## Quick Start (V4)

```bash
git clone https://github.com/Rahulpande7795/ai-voice-survey-bot.git
cd ai-voice-survey-bot/voice-survey-v4

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

pip install -r requirements.txt

cp .env.example .env
# Fill in GROQ_API_KEY (required), Twilio keys (for phone calls), ngrok token
```

**Browser mode:**
```bash
cd server
uvicorn main:app --reload --port 8000
# Open http://localhost:8000
```

**Phone call mode:**
```bash
python start_with_tunnel.py
# Starts ngrok tunnel + server together
# Set the printed webhook URL in Twilio Console once
```

Wait for all four startup lines:
```
STT  ✔  whisper-tiny ready
CACHE ✔  model loaded  threshold=0.75
TTS  ✔  cache warm — 68 phrases ready
V4 ready  →  http://localhost:8000
```

See [`voice-survey-v4/TWILIO_SETUP.md`](./voice-survey-v4/TWILIO_SETUP.md) for the full phone call setup.

---

## Tech Stack Across Versions

| Layer | V1 | V2 | V3 | V4 |
|-------|----|----|----|----|
| Transport | REST | WebSocket | WebSocket | WebSocket + Twilio |
| STT | Deepgram Nova-2 | Groq Whisper | Groq Whisper (Hindi) | faster-whisper (local) |
| VAD | — | Energy | Energy | Silero-VAD |
| LLM | GPT-4o | Groq Llama 3.1 8B | Groq Llama 3.1 8B | Groq Llama 3.1 8B |
| Intent | — | Survey engine | Regex classifier | Regex classifier |
| Semantic cache | — | — | FAISS + MiniLM | FAISS + MiniLM |
| TTS | ElevenLabs | gTTS + cache | gTTS + cache (Hindi) | edge-tts + 68-phrase cache |
| Phone | — | — | — | Twilio Media Streams |
| Language | English | English | Hindi / Hinglish | Hindi / Hinglish |

---

## Key Engineering Decisions

**Groq Whisper over Deepgram** — 216× real-time processing speed, same `GROQ_API_KEY` as the LLM, Hindi support, and 300–700ms from India vs 2.5–4s for Deepgram. The bottleneck with Deepgram wasn't processing speed — it was geography.

**Intent classifier before LLM** — ~80% of payment call answers are structured (yes/no, UPI, a date, a number). A regex classifier handles all of these in <1ms with zero network cost. This is the single biggest latency win across the project.

**FAISS over key-value cache** — spoken language varies. "Haan bilkul", "ji haan", "ho gaya" all mean yes. Exact-match caching misses all of them. `all-MiniLM-L6-v2` + FAISS cosine similarity at 0.75–0.85 threshold catches paraphrases reliably.

**Split TTS calls** — combining `ack_text + " " + next_question` into one call sounds natural but the string never hits the cache (ack text is unique LLM output). Splitting means the fixed survey question always hits the pre-warmed cache in <15ms.

**Local faster-whisper** — eliminates the India→US STT network hop entirely. `whisper-tiny` on CPU runs in <50ms for short utterances. The dangerous models guard in `stt_stream.py` prevents accidentally loading `large-v3-turbo` on CPU (10+ minute load, likely OOM).

---

## Performance Summary

```
V1 — REST pipeline:
  Total per turn:    5000–6500ms

V2 — WebSocket + Groq Whisper:
  Total per turn:    1800–3000ms   (~3× faster)

V3 — Intent classifier + FAISS cache:
  Intent hit:        ~400ms        (~15× faster than V1)
  Cache hit:         ~500ms
  LLM fallback:      1800–2500ms

V4 — Local STT + Twilio:
  Intent hit:        ~300ms        (~20× faster than V1)
  Cache hit:         ~175ms
  LLM fallback:      ~500ms
```

---

## Links

- **GitHub:** [github.com/Rahulpande7795](https://github.com/Rahulpande7795)
- **LinkedIn:** [linkedin.com/in/rahul-pande-dev](https://linkedin.com/in/rahul-pande-dev)

---

*Built by Rahul Pande · Vocab-AI Internship · 2025–2026*