# Twilio Integration Fixes Report

## Fixes Applied

### Fix 1 — TwiML PUBLIC_URL stale read

**Root cause:**  
`config.py` executes `PUBLIC_URL = os.getenv("PUBLIC_URL", "")` at **import time**.
`start_with_tunnel.py` writes `PUBLIC_URL` to `.env` *after* the server has already
imported `config.py`. So `config.PUBLIC_URL` was always the old (empty) value when
`/incoming-call` was hit.

**Fix:**  
`get_incoming_call_twiml()` in `server/twilio_handler.py` now calls
`os.getenv("PUBLIC_URL")` **fresh on every request**, bypassing the frozen
`config.PUBLIC_URL`. The new `start_with_tunnel.py` also passes `PUBLIC_URL` as a
real environment variable to the uvicorn child subprocess, so `os.getenv()` sees
the correct value immediately on startup.

**Verified:** TwiML now shows full `wss://` URL ✅

---

### Fix 2 — WatchFiles reload killing ngrok

**Root cause:**  
`server/start_with_tunnel.py` was **inside** the `server/` directory. uvicorn
`--reload` watches its `cwd` (`server/`) for changes. Every time a `.py` file in
`server/` was saved, watchfiles triggered a reload — which restarted the uvicorn
process, orphaning the ngrok tunnel that was started in the same process.

**Fix:**
1. `server/start_with_tunnel.py` **deleted**.
2. New `start_with_tunnel.py` created at **project root** (outside `server/`).
3. uvicorn started with `--reload-dir server` so watchfiles only watches `server/`.
4. ngrok tunnel is managed by the **parent** `start_with_tunnel.py` process.
   uvicorn runs as a **child subprocess** — if uvicorn restarts, ngrok stays alive.

**Verified:** No "WatchFiles detected changes in 'start_with_tunnel.py'" ✅

---

### Fix 3 — large-v3-turbo safety guard

**Root cause:**  
`.env` had `WHISPER_MODEL=large-v3-turbo` (active, not commented). If the
`config.py` override ever failed, the server would attempt to load a 3 GB model
on CPU — causing a 10+ minute startup hang and likely OOM crash.

**Fix:**  
Added `_DANGEROUS_MODELS` guard in `load_model()` inside `server/stt_stream.py`.
If `.env` contains any large model name, the guard emits a WARNING and forces
`model_name = BEST_ASR_MODEL` (from `config.py`, always `"tiny"`).

**Verified:** Server loads `whisper-tiny` regardless of `.env` value ✅

---

### Fix 4 — .env cleaned

- `WHISPER_MODEL=large-v3-turbo` → commented out with explanation
- `CACHE_THRESHOLD=0.85` → corrected to `0.75`
- All Twilio credentials preserved
- `PUBLIC_URL` preset to `https://photo-dinginess-unicorn.ngrok-free.dev`
- `NGROK_STATIC_DOMAIN` documented (optional)

**Verified:** `^WHISPER_MODEL=` grep returns 0 matches ✅

---

## Verification Checks

| Check | Command | Result |
|-------|---------|--------|
| `start_with_tunnel.py` at root | `Test-Path "start_with_tunnel.py"` | ✅ True |
| `server/start_with_tunnel.py` deleted | `Test-Path "server\start_with_tunnel.py"` | ✅ False |
| No executable `config.PUBLIC_URL` ref | grep in twilio_handler.py | ✅ 0 matches |
| `os.getenv("PUBLIC_URL")` in twiml fn | grep in twilio_handler.py | ✅ 2 matches |
| `_DANGEROUS_MODELS` guard present | grep in stt_stream.py | ✅ 2 matches |
| No active `WHISPER_MODEL=` line in .env | `^WHISPER_MODEL=` grep | ✅ 0 matches |
| `dotenv.set_key` works | python import test | ✅ OK |
| All modified files syntax OK | `py_compile.compile()` | ✅ 3/3 pass |

---

## Test Results

### TwiML Verification

```
POST http://localhost:8000/incoming-call
```

| Field | Before | After |
|-------|--------|-------|
| `<Stream url=` | `/twilio-ws` (no domain) | `wss://photo-dinginess-unicorn.ngrok-free.dev/twilio-ws` |
| HTTP Status | 200 | 200 |
| Content-Type | application/xml | application/xml |

Result: ✅ Full `wss://` URL confirmed (live test)

### Local WS Test (test_twilio_ws.py)

- Bot intro audio received: **YES**
- Bot response received: **YES**
- TTFA: **5639ms** (Whisper CPU local — expected; ~300ms with Groq cloud STT)
- Result: **✅ TWILIO INTEGRATION TEST PASSED**

### Real Phone Call Test

> ⚠️ **Pending** — requires calling +18782830614 from a verified Twilio number.
> Run `python start_with_tunnel.py` (from project root), update the Twilio Console
> webhook URL, then call the number.

| Check | Status |
|-------|--------|
| Call connected | ⏳ Pending real call test |
| Intro heard | ⏳ Pending |
| Bot responded to speech | ⏳ Pending |
| Survey completed | ⏳ Pending |

---

## How to Run

```powershell
# From project root:
cd C:\Users\rahul\OneDrive\Desktop\Voice_survey\voice-survey-v4
.\.venv\Scripts\Activate.ps1
python start_with_tunnel.py
```

Wait for:
```
STT  ✔  whisper-tiny ready        ← "tiny" not "large"
CACHE ✔  model loaded  threshold=0.75
TTS  ✔  cache warm — 68 phrases ready
V4 ready  →  http://localhost:8000
```

Then in Twilio Console → Phone Numbers → +18782830614 → Voice → Webhook:
```
https://photo-dinginess-unicorn.ngrok-free.dev/incoming-call
```

See [TWILIO_SETUP.md](TWILIO_SETUP.md) for complete step-by-step instructions.
