# TWILIO SETUP — Voice Survey V4

Complete guide to connecting the AI Voice Survey System to a real phone number via Twilio.

---

## Prerequisites Checklist

- [x] Twilio account with a phone number (+18782830614)
- [x] `twilio`, `pyngrok`, `audioop-lts` installed
- [x] Twilio credentials in `.env`
- [x] ngrok installed (`winget install ngrok`)
- [x] ngrok auth token in `.env`

---

## Quick Start (One Command)

```powershell
cd C:\Users\rahul\OneDrive\Desktop\Voice_survey\voice-survey-v4
.\.venv\Scripts\Activate.ps1
python server/start_with_tunnel.py
```

This will:
1. Open an ngrok HTTPS tunnel on port 8000
2. Print your public URL (copy the webhook URL)
3. Auto-update `PUBLIC_URL` in `.env`
4. Start the FastAPI server

---

## Step-by-Step Twilio Console Setup

### 1. Get your webhook URL

After running `start_with_tunnel.py`, you'll see output like:

```
================================================================
  ngrok tunnel   : https://abc123.ngrok-free.app
  Twilio webhook : https://abc123.ngrok-free.app/incoming-call
  Browser UI     : https://abc123.ngrok-free.app/
================================================================
```

Copy the **Twilio webhook** URL.

### 2. Set the webhook in Twilio Console

1. Go to https://console.twilio.com
2. Navigate to **Phone Numbers → Manage → Active numbers**
3. Click your number: **+18782830614**
4. Under **Voice Configuration**:
   - "A Call Comes In" → **Webhook**
   - URL: `https://YOUR_NGROK_URL.ngrok-free.app/incoming-call`
   - HTTP Method: **POST**
5. Click **Save**

> ⚠️ **Important**: The ngrok URL changes every time you restart `start_with_tunnel.py`.  
> Update the Twilio webhook URL each time you restart.

---

## What Happens On a Call

```
Caller dials +18782830614
    ↓
Twilio sends POST to /incoming-call
    ↓
Server returns TwiML (connects to /twilio-ws Media Stream)
    ↓
Twilio opens WebSocket to /twilio-ws
    ↓
Server plays intro + first question (edge-tts → mulaw → Twilio)
    ↓
Caller speaks → Twilio sends mulaw audio packets (20ms each)
    ↓
Server: mulaw → float32 16kHz → VAD → STT → intent/cache/LLM
    ↓
Server: response text → edge-tts MP3 → mulaw → Twilio
    ↓
Caller hears the bot response
    ↓
Loop until survey complete
```

---

## Audio Format Details

| Direction | Format |
|-----------|--------|
| Twilio → Server | mulaw G.711, 8000 Hz, 1 channel, 160 samples/packet (20ms) |
| Server → Twilio | mulaw G.711, 8000 Hz, 1 channel, base64-encoded JSON |
| Internal STT | float32 PCM, 16000 Hz, 1 channel |
| TTS source | MP3 (edge-tts) → converted to mulaw on the fly |

---

## Running the Local Integration Test

With the server already running, open a second terminal:

```powershell
cd C:\Users\rahul\OneDrive\Desktop\Voice_survey\voice-survey-v4
.\.venv\Scripts\Activate.ps1
python server/test_twilio_ws.py
```

Expected output:
```
Connecting to ws://localhost:8000/twilio-ws…
✔ Sent: connected + start events
Waiting for bot intro audio (up to 60s for model warmup)…
✅ Bot intro audio received!
Sending synthetic 500Hz tone (1.5s) as user speech…
✔ Sent 75 audio chunks (1500ms of speech)
Waiting for bot response audio (up to 30s)…
✅ Bot response received! TTFA = 450ms
==================================================
✅ TWILIO INTEGRATION TEST PASSED
   TTFA: 450ms
==================================================
```

---

## Verifying the TwiML Webhook

```powershell
# With server running:
Invoke-WebRequest -Method POST -Uri http://localhost:8000/incoming-call | Select-Object -ExpandProperty Content
```

Expected response:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://YOUR_NGROK_URL/twilio-ws" />
    </Connect>
    <Pause length="60"/>
</Response>
```

---

## Troubleshooting

### "Bot never sent intro audio"
- Check server logs for `TWILIO PIPE ▶  session start`
- Ensure `pydub` and `ffmpeg` are accessible: `ffmpeg -version`
- Check that ngrok tunnel is running

### TTFA > 5000ms
- TTS warmup: first call after server restart pays the full edge-tts synthesis cost
- Subsequent calls use the warm cache (< 10ms)
- STT on CPU: ~2000ms for local Whisper; use Groq (`USE_CLOUD_STT=True`) for 300ms

### "audioop module not found"
```powershell
pip install audioop-lts
```

### WebSocket closes immediately
- Ensure server is running and accessible
- Check that `/twilio-ws` route is registered (look for it in server startup logs)
- Test locally first with `test_twilio_ws.py` before connecting Twilio

### ngrok URL changes on restart
- Always run `start_with_tunnel.py` (not `uvicorn` directly) — it updates `.env` automatically
- After each restart, update the webhook URL in Twilio Console

### "PUBLIC_URL is not set"
- Run `start_with_tunnel.py` instead of starting uvicorn directly
- Or manually set `PUBLIC_URL=https://your-url.ngrok-free.app` in `.env`

---

## Production Deployment

For production (no ngrok):
1. Deploy to a server with a fixed public IP / domain
2. Set `PUBLIC_URL=https://your-domain.com` in environment
3. Enable HTTPS (required by Twilio)
4. Point Twilio webhook to `https://your-domain.com/incoming-call`

Recommended: AWS Elastic Beanstalk, Railway, or Render for easy HTTPS deployment.

---

## Files Added in This Integration

| File | Purpose |
|------|---------|
| `server/twilio_handler.py` | Core module: mulaw conversion, TwiML, WS handler |
| `server/start_with_tunnel.py` | One-command: ngrok + server launcher |
| `server/test_twilio_ws.py` | Local integration test (no real phone needed) |

### Modified Files

| File | Change |
|------|--------|
| `server/config.py` | Added Twilio env vars block |
| `server/stt_stream.py` | Added `transcribe_pcm()`, `_run_local_pcm()` |
| `server/pipeline.py` | Added `run_session_start_twilio()`, `run_turn_twilio()` |
| `server/main.py` | Registered `/incoming-call` and `/twilio-ws` routes |
| `.env` | Added Twilio + ngrok credentials, `PUBLIC_URL` placeholder |
