# Voice Survey V2

> **WebSocket · Streaming LLM · Dynamic Branching · gTTS**

V2 replaces V1's three sequential REST calls with a single persistent WebSocket connection, switches from Deepgram to Groq Whisper for STT, adds real-time LLM token streaming, and uses a dynamic survey tree instead of five fixed questions.

---

## Quick Start

```bash
cd voice-survey-v2

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Mac/Linux

pip install -r requirements.txt

cp .env.example .env
# Edit .env — only one key needed:
#   GROQ_API_KEY — https://console.groq.com (free tier)

cd server
uvicorn main:app --reload --port 8000
# Open http://localhost:8000
```

**.env — only this line needed:**
```
GROQ_API_KEY=gsk_...
```

---

## What Changed from V1

| Feature | V1 | V2 |
|---------|----|----|
| Transport | 3 REST calls per turn | 1 persistent WebSocket |
| STT | Deepgram Nova-2 (2.5–4s from India) | Groq Whisper (0.3–0.7s, same API key as LLM) |
| LLM output | Full JSON, then display | Tokens stream to screen live |
| TTS calls per turn | 2 (ack + question separately) | 1 combined call, in-memory cache |
| Survey | 5 fixed questions | Dynamic tree: pos / neg / neu branches |
| Latency | 5–6.5s | **1.8–3.0s** |

**Why Groq Whisper over Deepgram:**
Deepgram from India had a 2.5–4s round-trip due to geography — the processing wasn't slow, the network was. Groq Whisper runs at 216× real-time on their LPU infrastructure and brought STT down to 300–700ms. Using the same `GROQ_API_KEY` for both STT and LLM also means one fewer account and API key to manage.

---

## File Structure

```
voice-survey-v2/
├── server/
│   ├── main.py           ← FastAPI app + WebSocket handler
│   ├── pipeline.py       ← Orchestrates STT → LLM → TTS per turn
│   ├── stt_stream.py     ← Groq Whisper batch STT (WebM-aware collector)
│   ├── llm_stream.py     ← Groq Llama streaming acknowledgement
│   ├── tts_stream.py     ← gTTS async wrapper + in-memory cache
│   ├── survey_engine.py  ← Dynamic branching logic
│   └── context.py        ← Per-session state manager (30min TTL)
├── client/
│   └── index.html        ← Single-page streaming UI
├── requirements.txt
└── .env.example
```

---

## How V2 Works

### Request flow per turn

```
[You speak]
    │
    ▼  WebSocket (binary audio chunks, 150ms each)
[Server collects audio]
    │
    ▼  Groq Whisper (complete WebM blob)
[Text transcript]
    │
    ▼  Groq Llama 3.1 8B (streaming tokens)
[Acknowledgement — tokens appear on screen live]
    │
    ▼  gTTS (one combined TTS call, cached)
[MP3 audio chunks → browser plays]
    │
    ▼  Survey engine decides next node
[Next question spoken + shown]
    │
    └── repeat until Q_close
```

### WebSocket protocol

```
Client → Server:
  {type: "audio_chunk", data: "<base64>"}   ← 150ms chunks while holding button
  {type: "audio_end"}                        ← button released
  {type: "start_session"}                    ← restart survey

Server → Client:
  {type: "transcript_final",  text}          ← what Whisper heard
  {type: "llm_token",         text}          ← each LLM token (shown live)
  {type: "audio_chunk",       data: base64}  ← MP3 audio stream
  {type: "audio_ready"}                      ← play buffered audio
  {type: "next_question", text, node}        ← next question + branch name
  {type: "survey_done"}                      ← all done
  {type: "error",         message}           ← something failed
```

### Survey branching tree

```
Q1: "How satisfied are you? (1-5)"
  │
  ├── Score 1-2 (negative)  →  Q2_neg: "What went wrong?"
  ├── Score 3   (neutral)   →  Q2_neu: "What would improve things?"
  └── Score 4-5 (positive)  →  Q2_pos: "What made it great?"
              │
              └── Q3: "Would you recommend us?"
                        │
                        └── Q_close: "Thank you!" → DONE
```

Score is extracted from spoken answers ("four", "4", "4 out of 5" all work). If no number is found, keyword sentiment is used as fallback.

### The WebM EBML header bug

WebM audio files start with a magic header (`1A 45 DF A3`). The browser's `MediaRecorder` sometimes fires its first `ondataavailable` event before this header is written — sending headerless chunks to the STT API causes a `400` error. `AudioCollector` scans each incoming chunk for the magic bytes and only starts collecting from the chunk that contains the header. Pre-header chunks are silently dropped.

This was discovered when the first recording attempt of every session was failing with `400 Bad Request`. The fix brought the error rate to zero across all subsequent sessions.

---

## Latency (measured, India → Groq US)

| Stage | V2 time | Notes |
|-------|---------|-------|
| STT | 300–700ms | Groq Whisper, vs 2.5–4s Deepgram in V1 |
| LLM | 1300–1900ms | Groq Llama 3.1 8B, tokens visible as they stream |
| TTS | 10ms (cached) | gTTS result cached per phrase after first call |
| **Total** | **1.8–3.0s** | ~3× faster than V1 |

The TTS cache is the main trick here. The fixed survey questions never change, so after the first call each phrase returns in <15ms from memory.

---

## Tech Stack

| Layer | Tool | Notes |
|-------|------|-------|
| STT | Groq Whisper v3-Turbo | Same API key as LLM, 216× real-time |
| LLM | Groq Llama 3.1 8B | Streaming tokens, free tier |
| TTS | gTTS | Free, no API key, in-memory cache |
| Server | FastAPI + uvicorn | Async-native, WebSocket support |
| Frontend | Vanilla JS | No build step |

---

## Acceptance Checklist

- [ ] WebSocket connects on page load (green dot in header)
- [ ] First question speaks automatically
- [ ] Hold button → mic records → release → transcribed
- [ ] LLM acknowledgement tokens appear on screen while audio plays
- [ ] Score 1-2 → bot goes to negative path (Q2_neg)
- [ ] Score 4-5 → bot goes to positive path (Q2_pos)
- [ ] Score 3 → bot goes to neutral path (Q2_neu)
- [ ] Survey completes with thank-you message
- [ ] Start Over button resets and restarts cleanly