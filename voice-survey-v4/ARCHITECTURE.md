# AI Voice Survey V4 — Architecture & Project Overview

## What This Project Does

V4 is the production version of an AI voice bot I built for L&T Finance during my internship. It conducts automated Hindi/Hinglish payment verification calls — asking customers about their EMI payment status, extracting structured data (date, amount, method, payer), and routing through a 12-node call script.

Two delivery channels are supported:
- **Browser** — works at `http://localhost:8000`, useful for demos and testing
- **Phone (Twilio)** — real outbound calls via Twilio Media Streams, tested locally and verified with integration tests

The core engineering goal assigned to me was to get LLM-side latency under 300ms using semantic caching, NLP intent extraction, and partial ASR transcript processing. All three are implemented and working. The Twilio integration was added afterward to make it work on actual phone calls.

---

## Project Location

```
voice-survey-v4/
├── start_with_tunnel.py        ← one-command launcher (ngrok + server)
├── .env.example                ← copy to .env and fill in your keys
├── requirements.txt
├── server/
│   ├── main.py                 ← FastAPI app, WebSocket routes
│   ├── pipeline.py             ← survey orchestrator, run_turn()
│   ├── stt_stream.py           ← Whisper STT + Silero VAD
│   ├── tts_stream.py           ← edge-tts with LRU phrase cache
│   ├── cache.py                ← FAISS semantic cache + exact-match dict
│   ├── intent.py               ← local regex intent classifier
│   ├── extractor.py            ← structured field extraction
│   ├── llm_stream.py           ← Groq LLM streaming
│   ├── survey_engine.py        ← 12-node L&T Finance call script
│   ├── context.py              ← per-session state
│   ├── config.py               ← all model/VAD/cache config in one place
│   └── twilio_handler.py       ← Twilio Media Streams integration
└── client/
    └── index.html              ← browser UI
```

---

## How the Decision Pipeline Works

Each spoken turn goes through 4 layers in order, stopping at the first hit:

```
User speaks → STT transcript
      │
      ├─ Layer 1: extractor.py    always runs · pulls date/amount/method/payer from text
      ├─ Layer 2: intent.py       regex classifier · <1ms · handles ~80% of turns
      ├─ Layer 3: cache.py        FAISS + exact-match · ~3ms · handles ~15% of turns
      └─ Layer 4: llm_stream.py   Groq fallback · ~80ms TTFT · only ~5% of turns
```

The reason this ordering matters: for a payment verification call, most answers are predictable — "haan", "nahi", "UPI se", "15 tarikh ko". The regex classifier handles all of these instantly without any network call. The LLM only gets involved for genuinely free-text responses like reasons for non-payment.

**Cache has 3 sub-layers (fastest first):**
- Node-specific exact-match dict — O(1)
- Global exact-match dict — catches common words like "yes/haan/no/nahi/okay"
- FAISS vector search — ~3ms, cosine similarity at 0.75 threshold, catches paraphrases

---

## Survey Call Script (12 nodes)

```
INTRO → AVAILABILITY → PURPOSE → PAYMENT_CHECK
                                      │
                              ┌───────┴────────┐
                           yes (paid)       no (not paid)
                              │                │
                           WHO_PAID         REASON → CAPTURE_EXEC → CLOSE
                              │
                    DATE → METHOD → AMOUNT → CLOSE
```

The `AVAILABILITY` node branches to `CALLBACK → CLOSE` if the customer says they can't talk now.

---

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| ASR | faster-whisper tiny | CPU int8 · single shared instance |
| VAD | silero-vad | torch.hub · must use 512-sample frames at 16kHz |
| LLM | Groq API | llama-3.1-8b-instant · streaming tokens |
| TTS | edge-tts | en-IN-NeerjaNeural · 68-phrase LRU warm cache |
| Semantic cache | faiss-cpu + sentence-transformers | all-MiniLM-L6-v2 · threshold 0.75 |
| Server | FastAPI + uvicorn | WebSocket · asyncio |
| Phone | Twilio Media Streams | mulaw 8kHz ↔ float32 16kHz conversion |
| Tunnel | pyngrok | ngrok HTTPS tunnel for Twilio webhook |

---

## Performance (measured, India → Groq US)

| Turn type | Pipeline latency | Notes |
|-----------|-----------------|-------|
| Intent hit (~80% of turns) | 12–23ms | no network call at all |
| Cache hit (~15%) | 3–5ms | FAISS lookup only |
| LLM fallback (~5%) | ~1300ms | Groq round-trip from India |

The STT step (faster-whisper on CPU) adds ~50–300ms depending on utterance length. In the Twilio path, VAD fires while the user is still speaking, so STT runs on already-buffered audio and doesn't block the pipeline.

---

## Important Config Notes

A few things that are easy to break and took me time to figure out:

**Never change ASR model to large on CPU.** `whisper-tiny` loads in ~17s on CPU. `large-v3-turbo` would take 10+ minutes and likely OOM. There's a `_DANGEROUS_MODELS` guard in `stt_stream.py` that forces `tiny` even if `.env` says otherwise.

**silero-vad requires exactly 512 samples per call.** Passing the full 1s window (16000 samples) throws a `ValueError`. `_is_speech()` iterates in 512-sample chunks and takes the max probability.

**Don't use `config.PUBLIC_URL` in the Twilio handler.** `config.py` reads env vars at import time, before `start_with_tunnel.py` has written the ngrok URL to `.env`. `twilio_handler.py` calls `os.getenv("PUBLIC_URL")` fresh on every request.

**Run `start_with_tunnel.py` from the project root, not from inside `server/`.** uvicorn's `--reload` watches the current directory — if the launcher is inside `server/`, every file save triggers a reload that orphans the ngrok tunnel.

**CACHE_THRESHOLD is 0.75, not 0.85.** Was lowered intentionally after testing showed 0.85 was too strict for Hindi paraphrases.

---

## Twilio Audio Format

- **Incoming:** mulaw G.711 at 8kHz → `audioop.ulaw2lin()` → int16 PCM → upsample to 16kHz float32 → Whisper
- **Outgoing:** edge-tts MP3 → pydub decode → int16 at 8kHz → `audioop.lin2ulaw()` → base64 JSON → Twilio

---

## How to Start

```powershell
cd voice-survey-v4
.\.venv\Scripts\Activate.ps1
python start_with_tunnel.py
```

Wait for all four of these before testing:
```
STT  ✔  whisper-tiny ready
CACHE ✔  model loaded  threshold=0.75
TTS  ✔  cache warm — 68 phrases ready
V4 ready  →  http://localhost:8000
```

For phone calls, after startup update the Twilio Console webhook to the ngrok URL printed in the terminal. See `TWILIO_SETUP.md` for the full setup.

---

## Packages

```
fastapi · uvicorn[standard] · groq · edge-tts · faster-whisper
faiss-cpu · sentence-transformers · numpy · torch · torchaudio
PyAV · pydub · python-dotenv · websockets · soundfile
twilio · pyngrok · audioop-lts
```