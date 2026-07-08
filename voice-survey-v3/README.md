# 🎙 AI Voice Survey V3 — Intent Cache + Hindi + L&T Finance

> Real-time Hindi/Hinglish voice bot for L&T Finance payment verification calls.
> V3 introduces a local intent classifier, FAISS semantic cache, and a 12-node Hindi call script — cutting decision latency from ~1800ms to ~400ms on structured responses.

---

## What Changed from V2

V2 was fast for an English survey bot but still sent every answer through Groq's LLM. For a payment verification call where most answers are "haan", "nahi", or a number, that's unnecessary. V3 adds two layers before the LLM:

1. **Local intent classifier** — regex-based, handles ~70% of turns in <1ms with zero network cost
2. **FAISS semantic cache** — catches paraphrases like "haan bilkul" = "ji haan" using sentence embeddings, ~5ms

The LLM only runs for genuinely free-text answers (reasons for non-payment, contact details).

---

## Live Demo Metrics

> Measured from India → Groq US servers (no GPU, no Mumbai deployment)

| Version | STT | LLM | TTS | **Total/turn** |
|---------|-----|-----|-----|----------------|
| V1 | 2500–4000ms | 1800ms | 1100–4000ms | **5000–6500ms** |
| V2 | 300–700ms | 1300–1800ms | 10ms (cached) | **1800–3000ms** |
| V3 — LLM path | 300–700ms | 1300ms | 10ms | **1800–2500ms** |
| V3 — Cache hit | 300–700ms | 0ms | 10ms | **~400ms** |
| V3 — Intent hit | 300–700ms | 0ms | 10ms | **~50ms decision** |

---

## Decision Pipeline (per turn)

```
Transcript arrives
      │
      ▼
┌─────────────────────┐        ✓ hit → <1ms decision
│  Intent Classifier  │ ───────────────────────────→ ack + next_node
│  (local, regex)     │
└────────┬────────────┘
         │ miss
         ▼
┌─────────────────────┐        ✓ hit → ~5ms decision
│  Semantic FAISS     │ ───────────────────────────→ ack + next_node
│  Cache (MiniLM)     │
└────────┬────────────┘
         │ miss
         ▼
┌─────────────────────┐        always → ~1300ms
│  Groq Llama 3.1 8B  │ ───────────────────────────→ ack + cache.add()
│  (streaming tokens) │
└─────────────────────┘
```

---

## L&T Finance Call Script (12 nodes)

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

**End-of-call data captured (logged + sent to client):**
```json
{
  "payer": "self",
  "payment_date": "15/04",
  "payment_method": "upi",
  "payment_amount": 5000
}
```

---

## V3 System Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Browser Client                        │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │ Hover-to-  │  │  Waveform    │  │  3-Panel UI      │ │
│  │ record mic │  │  visualiser  │  │  (Question /     │ │
│  │ (WebM/Opus)│  │  (canvas)    │  │   Chat / Data)   │ │
│  └─────┬──────┘  └──────────────┘  └──────────────────┘ │
└────────┼─────────────────────────────────────────────────┘
         │ WebSocket (100ms chunks, base64 WebM)
         ▼
┌──────────────────────────────────────────────────────────┐
│              FastAPI Server (main.py)                    │
│                                                          │
│  AudioCollector (EBML header fix) → stt_stream.py       │
│       │                                                  │
│       ▼ Groq Whisper Large-v3-Turbo (language=hi)       │
│  transcript                                              │
│       │                                                  │
│       ▼ pipeline.py                                      │
│  ┌────┴───────────────────────────────────┐             │
│  │ 1. extractor.py  (always, <1ms)        │             │
│  │ 2. intent.py     (regex, <1ms)         │             │
│  │ 3. cache.py      (FAISS MiniLM, ~5ms)  │             │
│  │ 4. llm_stream.py (Groq Llama, ~1300ms) │             │
│  └────┬───────────────────────────────────┘             │
│       │                                                  │
│       ▼ tts_stream.py (gTTS, lang=hi, cached)           │
│  MP3 chunks streamed back over WebSocket                │
└──────────────────────────────────────────────────────────┘
```

---

## Key Engineering Decisions

**1. Intent classifier before LLM**
For a payment verification call, the majority of answers are structured: "haan", "nahi", "5000 rupaye", "UPI se", "15 tarikh ko". A regex classifier handles all of these in <1ms with zero network cost. The LLM is only invoked for genuinely free-text answers. This is the single biggest latency win in V3.

**2. FAISS over a key-value cache**
A key-value cache only hits on exact string matches. Spoken language varies: "haan bilkul", "ji haan", "ho gaya payment" all mean the same thing. `all-MiniLM-L6-v2` embeds these into similar vectors; FAISS cosine similarity search at threshold 0.85 catches paraphrases without false positives.

**3. Split TTS calls (ack + question separately)**
Combining `ack_text + " " + next_question` into one gTTS call sounds natural but the combined string never matches the cache (because `ack_text` is unique LLM output). This caused 3000ms+ TTS on every turn in V2. Splitting into two calls means the fixed survey question always hits the pre-warmed cache in <15ms.

**4. Sentence-level TTS streaming**
V2 waited for the full LLM acknowledgement before starting TTS. V3 buffers tokens until a sentence boundary (`. ! ? ।`) and fires TTS on each sentence as an `asyncio.Task`. First audio arrives 200–400ms earlier.

```
V2:  [tokens streaming ──────────────] → [TTS full ack] → audio
V3:  [tokens streaming] [sentence 1] → TTS fires
                        [sentence 2] → TTS fires
```

---

## Tech Stack

| Layer | V1 | V2 | V3 |
|-------|----|----|-----|
| Transport | HTTP REST | WebSocket | WebSocket |
| STT | Deepgram Nova-2 | Groq Whisper v3-Turbo | Groq Whisper v3-Turbo (Hindi) |
| LLM | Groq Llama 3.1 8B | Groq Llama 3.1 8B | Groq Llama 3.1 8B + Intent bypass |
| TTS | gTTS (no cache) | gTTS + cache | gTTS + cache (Hindi, co.in TLD) |
| Intent | — | Survey engine only | Local regex classifier |
| Semantic cache | — | — | FAISS + sentence-transformers |
| Data extraction | — | — | Regex extractor (date/amount/method) |
| Frontend | Hold-to-record | Hover-to-record, waveform, latency HUD | 3-panel, live data capture, source badge |
| Language | English | English | Hindi / Hinglish (env-configurable) |

---

## File Structure

```
voice-survey-v3/
├── server/
│   ├── main.py           # FastAPI app, WebSocket handler, startup
│   ├── pipeline.py       # Intent → Cache → LLM decision chain, sentence TTS
│   ├── intent.py         # Local regex intent classifier (<1ms)
│   ├── cache.py          # SemanticCache: FAISS + MiniLM embeddings
│   ├── extractor.py      # Structured data extraction per node
│   ├── llm_stream.py     # Groq Llama streaming, sentence-boundary buffering
│   ├── stt_stream.py     # Groq Whisper STT, AudioCollector (EBML fix)
│   ├── tts_stream.py     # gTTS async wrapper, in-memory cache, warm_cache()
│   ├── survey_engine.py  # L&T Finance 12-node call script (Hindi + English)
│   └── context.py        # Session state, data_capture, 30-min TTL
├── client/
│   └── index.html        # 3-panel UI: question, mic zone, live data capture
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup & Running

### Prerequisites
- Python 3.10+
- [Groq API key](https://console.groq.com) (free tier: 28,800 sec/day audio + LLM)
- No GPU required

### Install

```bash
cd voice-survey-v3

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:
```env
GROQ_API_KEY=gsk_your_key_here

# Hindi mode (recommended for L&T Finance)
STT_LANGUAGE=hi
TTS_LANG=hi
SURVEY_LANGUAGE=hi

# English mode (for testing)
# STT_LANGUAGE=en
# TTS_LANG=en
# SURVEY_LANGUAGE=en

CACHE_THRESHOLD=0.85
TTS_TLD=co.in
```

### Run

```bash
cd server
uvicorn main:app --reload --port 8000
```

Expected startup output:
```
TTS  ▶  warming 13 phrases (lang=hi)…
CACHE ✔  model loaded (all-MiniLM-L6-v2)  threshold=0.85
TTS  ✔  cache warm — 13 phrases ready
Voice Survey V3 (L&T Finance) ready  →  http://localhost:8000
```

Open `http://localhost:8000` in Chrome/Edge. Hover the mic zone to speak.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | — | **Required.** Groq console API key |
| `STT_LANGUAGE` | `en` | `hi` for Hindi, `en` for English |
| `TTS_LANG` | `en` | `hi` for Hindi gTTS output |
| `SURVEY_LANGUAGE` | `en` | `hi` loads Hindi call script |
| `CACHE_THRESHOLD` | `0.85` | Semantic similarity cutoff (0.0–1.0) |
| `CACHE_MODEL` | `all-MiniLM-L6-v2` | HuggingFace embedding model |
| `TTS_TLD` | `com` | `co.in` for Indian English accent |

---

## Log Output Per Turn

```bash
# Intent hit — no LLM call
INTENT_HIT  node=PAYMENT_CHECK   type=yes    conf=0.96  → WHO_PAID
EXTRACT     {'payment_check_raw': 'haan ho gayi hai'}
TTS  ✔  ack 12ms (source=intent)
PIPE ✔  PAYMENT_CHECK → WHO_PAID  via=intent  47ms

# Cache hit — no LLM call
CACHE HIT   node=DATE   sim=0.912  → "Theek hai, 15/04 note kar li."
TTS  ✔  ack 11ms (source=cache)
PIPE ✔  DATE → METHOD  via=cache  22ms

# LLM call — free text reason
LLM_CALL    node=REASON  1342ms  "Samajh gaye, financial difficulty..."
CACHE ADD   node=REASON
PIPE ✔  REASON → CAPTURE_EXEC  via=llm  2087ms

# End of call
CAPTURE_FINAL  {'payer': 'self', 'payment_date': '15/04',
                'payment_method': 'upi', 'payment_amount': 5000}
```

---

*Built by Rahul Pande · Vocab-AI Internship · 2025–2026*