# Voice Survey V1

A low-latency voice survey chatbot — batch pipeline edition.

```
STT (Deepgram Nova-2) → LLM (GPT-4o) → TTS (ElevenLabs)
```

Users speak answers; the bot transcribes, understands, and replies in audio.

---

## Prerequisites

| Tool    | Version   |
|---------|-----------|
| Python  | 3.11+     |
| pip     | any       |

---

## Quick start

### 1. Clone / copy the project

```bash
# If you received this as a zip, just unzip it.
cd voice-survey-v1
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment variables

```bash
cp .env.example .env
# Open .env and fill in your API keys:
#   DEEPGRAM_API_KEY   — https://console.deepgram.com
#   OPENAI_API_KEY     — https://platform.openai.com/api-keys
#   ELEVENLABS_API_KEY — https://elevenlabs.io (free tier: 10k chars/month)
```

### 5. Run the server

```bash
cd server
uvicorn main:app --reload --port 8000
```

### 6. Open the UI

Visit [http://localhost:8000](http://localhost:8000) in your browser.

---

## Usage

1. The bot speaks the first question aloud.
2. **Hold** the microphone button and speak your answer.
3. **Release** — the audio is transcribed, the LLM generates a response, and the bot speaks back.
4. Repeat for all 5 questions.
5. The survey ends with a thank-you message.

---

## Project structure

```
voice-survey-v1/
├── server/
│   ├── main.py        ← FastAPI app (3 endpoints + UI serving)
│   ├── stt.py         ← Deepgram Nova-2 batch wrapper
│   ├── llm.py         ← GPT-4o survey conductor
│   ├── tts.py         ← ElevenLabs TTS wrapper
│   └── survey.json    ← Static 5-question survey definition
├── client/
│   └── index.html     ← Single-page UI (no build step required)
├── .env.example
├── requirements.txt
└── README.md
```

---

## API reference

| Method | Path          | Request body / form          | Response                              |
|--------|---------------|------------------------------|---------------------------------------|
| GET    | `/`           | —                            | HTML page                             |
| POST   | `/transcribe` | `multipart/form-data` audio  | `{"text": "…"}`                       |
| POST   | `/respond`    | `{"transcript","history"}`   | `{"response","next_question","done"}` |
| POST   | `/synthesise` | `{"text": "…"}`              | `audio/mpeg` bytes                    |

---

## Acceptance checklist (V1)

- [ ] Server starts at `localhost:8000` with no errors
- [ ] `GET /` returns the survey UI
- [ ] Record button captures mic audio and POSTs to `/transcribe`
- [ ] Transcript is displayed and sent to `/respond`
- [ ] Bot acknowledgement + next question plays as audio
- [ ] All 5 questions complete → `done: true` → done screen shown
- [ ] Chat log visible throughout the session

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Microphone access denied" | Allow microphone in browser permissions |
| 502 on `/transcribe` | Check `DEEPGRAM_API_KEY` in `.env` |
| 502 on `/respond` | Check `OPENAI_API_KEY` in `.env` |
| 502 on `/synthesise` | Check `ELEVENLABS_API_KEY` in `.env` |
| No audio plays | Browser may block autoplay — click somewhere on the page first |

---

## Next steps

Once V1 is working, proceed to **V2** for WebSocket streaming and dynamic survey branching, targeting 350–650 ms end-to-end latency.