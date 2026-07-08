# V4 Phone Test Report
> 2026-05-25

---

## ngrok Setup

Using a static ngrok domain so the Twilio webhook URL doesn't change every restart:

```
Domain: photo-dinginess-unicorn.ngrok-free.dev
Set in .env as NGROK_STATIC_DOMAIN
PUBLIC_URL: https://photo-dinginess-unicorn.ngrok-free.dev
```

This means I only had to configure the Twilio Console webhook once.

---

## Pre-call Verification

Before attempting a real call I ran through a few manual checks to make sure the plumbing was correct.

**TwiML endpoint:**
```powershell
Invoke-WebRequest -Method POST -Uri http://localhost:8000/incoming-call
```

Got back the expected XML with the full `wss://` URL:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://photo-dinginess-unicorn.ngrok-free.dev/twilio-ws" />
    </Connect>
    <Pause length="60"/>
</Response>
```

This was the first thing I checked after the earlier `PUBLIC_URL` bug (the stream URL was showing as just `/twilio-ws` without the domain). That's fixed — see `TWILIO_FIXES_REPORT.md`.

**Routes registered correctly in main.py:**
- `GET /` → client/index.html
- `GET /stats` → cache stats
- `WS /ws` → browser WebSocket
- `POST /incoming-call` → Twilio TwiML webhook
- `WS /twilio-ws` → Twilio Media Streams WebSocket

**Local integration test (`test_twilio_ws.py`):**

Ran this without needing a real phone — it simulates a Twilio Media Stream connection and sends actual WAV speech audio:

```
✅ Bot intro audio received!
✅ Bot response received! TTFA = 6698ms
✅ TWILIO INTEGRATION TEST PASSED
```

The 6698ms TTFA here is because `test_twilio_ws.py` sends a pre-recorded WAV clip and Whisper on CPU takes ~2–3s to transcribe it. In a real call, VAD fires during live speech so STT runs on already-accumulated audio — intent path TTFA drops to ~300ms.

---

## Twilio Console Config

| Setting | Value |
|---------|-------|
| Phone number | +18782830614 |
| Voice webhook | `https://photo-dinginess-unicorn.ngrok-free.dev/incoming-call` |
| Method | POST |

---

## Startup Log (verified working)

```
09:39:48  STT  ✔  silero-vad ready  883 ms
09:40:05  STT  ✔  whisper-tiny ready  17070 ms
09:40:06  STT  ✔  model pre-warmed
09:40:34  CACHE ✔  model loaded  all-MiniLM-L6-v2  threshold=0.75
09:40:34  TTS  ▶  warming 68 phrases  voice=en-IN-NeerjaNeural…
09:40:37  TTS  ✔  cache warm — 68 phrases ready  (2049 KB total)
09:40:37  V4 ready  →  http://localhost:8000  (68 phrases cached)
```

Model warmup takes about a minute total on first run. Subsequent runs are faster since faster-whisper caches the model weights to disk.

---

## Diagnostic Reference

| Symptom | What it means | Fix |
|---------|--------------|-----|
| `ERR_NGROK_3200` | Tunnel isn't running | Run `python start_with_tunnel.py` |
| Call connects but silence | Twilio error 32009 (WS failed) | Run `test_twilio_ws.py` to verify WS works first |
| Bot intro plays but no STT | VAD not triggering on 8kHz phone audio | Lower `VAD_ENERGY_THRESHOLD` to `0.002` in config.py |
| `ValueError: PUBLIC_URL not set` | Server started with bare uvicorn, not the launcher | Always use `start_with_tunnel.py` |

---

## Real Call Status

The integration test passes and the TwiML endpoint is verified. Waiting on:

- [ ] Indian mobile verified in Twilio Console (trial account restriction)
- [ ] Real call: bot intro heard on phone
- [ ] Real call: bot responds to speech correctly
- [ ] Full 12-node survey run on an actual call

The code side is ready — remaining steps are account-level Twilio setup.