"""
config.py — Single source of truth for all model configuration.
Overrides .env values. All modules import from here.

Run benchmark_asr.py / benchmark_tts.py / benchmark_llm.py to auto-populate
the BEST_* fields below with measured winners.
"""
import os

# ── ASR ──────────────────────────────────────────────────────────────────────
# Options: "tiny", "tiny.en", "base", "base.en"
# "tiny.en" is faster but English-only (no Hindi)
# "tiny" supports Hindi + English — use this as default
BEST_ASR_MODEL   = "tiny"           # updated by benchmark_asr.py
ASR_THREADS      = 6                # cpu_threads for WhisperModel
ASR_WORKERS      = 2                # num_workers for WhisperModel
ASR_BEAM_SIZE    = 2                # 2 = one extra candidate sequence, better accuracy on 5+ word sentences
ASR_COMPUTE_TYPE = "int8"           # fastest on CPU
ASR_DEVICE       = "cpu"

# Cloud STT fallback (set True if local STT > 400ms consistently)
USE_CLOUD_STT       = False
CLOUD_STT_PROVIDER  = "groq"
CLOUD_STT_MODEL     = "whisper-large-v3-turbo"

# Legacy compat alias used by old stt_stream.py code
USE_GROQ_WHISPER = USE_CLOUD_STT

# ── TTS ──────────────────────────────────────────────────────────────────────
BEST_TTS_VOICE       = "en-IN-NeerjaNeural"   # updated by benchmark_tts.py
BEST_TTS_VOICE_HINDI = "hi-IN-SwaraNeural"    # Hindi voice

# ── LLM ──────────────────────────────────────────────────────────────────────
BEST_LLM_MODEL = "llama-3.1-8b-instant"       # updated by benchmark_llm.py
LLM_TIMEOUT_S  = 3.0                           # max wait for Groq API

# ── Cache ─────────────────────────────────────────────────────────────────────
CACHE_THRESHOLD   = 0.75    # was 0.85 — lower = more cache hits
CACHE_MAX_ENTRIES = 500     # LRU eviction limit

# ── VAD ──────────────────────────────────────────────────────────────────────
# Set USE_ENERGY_VAD = True if silero-vad fails to download/load
USE_ENERGY_VAD        = False   # True = use RMS energy fallback instead of silero
VAD_SILENCE_FRAMES    = 6       # 6 × 32ms = 192ms silence = end of speech (was 8 = 256ms)
VAD_SPEECH_MIN_FRAMES = 3       # need this many speech frames before triggering (was 4)
VAD_ENERGY_THRESHOLD  = 0.005   # RMS threshold for energy VAD fallback (was 0.008, more sensitive)

# ── Twilio Phone VAD (separate thresholds for mulaw 8kHz upsampled audio) ────
# Phone audio after mulaw→PCM conversion has higher noise floor than browser mic.
# These conservative settings prevent 0.12s false end-of-speech triggers.
TWILIO_VAD_SILENCE_FRAMES    = 12     # 12 × 32ms = 384ms silence — real EOS on phone
TWILIO_VAD_SPEECH_MIN_FRAMES = 5      # need 5 frames of speech before EOS can fire
TWILIO_VAD_ENERGY_THRESHOLD  = 0.015  # higher RMS threshold for phone noise floor
TWILIO_MIN_SPEECH_DURATION_S = 0.8    # reject STT if < 800ms of detected speech


# ── Twilio ────────────────────────────────────────────────────────────────────
import os as _os
TWILIO_ACCOUNT_SID  = _os.getenv("TWILIO_ACCOUNT_SID",  "")
TWILIO_AUTH_TOKEN   = _os.getenv("TWILIO_AUTH_TOKEN",   "")
TWILIO_PHONE_NUMBER = _os.getenv("TWILIO_PHONE_NUMBER", "")
PUBLIC_URL          = _os.getenv("PUBLIC_URL",          "")
# Phone audio format: Twilio sends mulaw 8kHz, Whisper needs float32 16kHz
TWILIO_SAMPLE_RATE  = 8000
WHISPER_SAMPLE_RATE = 16000
