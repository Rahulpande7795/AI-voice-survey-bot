"""
stt_stream.py — V4 STT pipeline: single Whisper model + Silero VAD.

BUGS FIXED:
  1. _decode_to_pcm had no fallback — if PyAV threw any exception
     (malformed WebM, partial chunk, unsupported codec) the function
     returned None silently, VAD never fired, and the turn hung forever.
     Fixed: added pydub fallback, then raw int16 last-resort fallback.

  2. _run_local passed buf.name = "audio.webm" hardcoded even when the
     input was a WAV file (RIFF header). faster-whisper uses the file
     extension to choose a demuxer, so WAV bytes with a ".webm" name
     caused silent decode failures returning empty transcripts.
     Fixed: _detect_format_name() reads magic bytes to set correct name.

  3. StreamingSTT.process_chunk decoded the ENTIRE accumulated blob every
     4KB, so for a 100KB recording it ran the full PyAV decoder ~25 times.
     Fixed: only decode a trailing 64KB window (≈2s of Opus audio).

Architecture:
  load_model()         — called once at startup via FastAPI lifespan
  AudioCollector       — accumulates raw WebM/WAV bytes, detects header
  StreamingSTT         — feeds chunks through VAD, returns True on EOS
  transcribe_blob()    — full transcription of the final assembled blob
  transcribe_partial() — fast partial transcription (same shared model)
"""
import asyncio
import io
import logging
import os
import threading
import time

import numpy as np

import config
from config import (
    ASR_BEAM_SIZE,
    ASR_COMPUTE_TYPE,
    ASR_DEVICE,
    ASR_THREADS,
    ASR_WORKERS,
    BEST_ASR_MODEL,
    CLOUD_STT_MODEL,
    USE_ENERGY_VAD,
    USE_GROQ_WHISPER,
    VAD_ENERGY_THRESHOLD,
    VAD_SILENCE_FRAMES,
    VAD_SPEECH_MIN_FRAMES,
)

log = logging.getLogger("survey")

MIN_AUDIO_BYTES = 2_000
WEBM_MAGIC      = b"\x1a\x45\xdf\xa3"
WAV_MAGIC       = b"RIFF"
STT_LANGUAGE    = os.getenv("STT_LANGUAGE", "en")

# ── Single shared Whisper model — loaded once, reused everywhere ──────────────
_model:     object | None = None
_model_vad: object | None = None
_load_lock  = threading.Lock()


def get_whisper_model():
    """Return the shared Whisper model after load_model() has run."""
    return _model


def load_model() -> None:
    """
    Load ONE Whisper model + Silero-VAD at startup.
    Idempotent — calling a second time is a no-op.
    """
    global _model, _model_vad

    with _load_lock:
        if _model is not None:
            log.debug("STT  ℹ  model already loaded — skipping")
            return

        # ── 1. Silero-VAD ─────────────────────────────────────────────────
        if not USE_ENERGY_VAD:
            try:
                import torch as _torch
                log.info("STT  ▶  loading silero-vad…")
                t0 = time.perf_counter()
                _torch.hub.set_dir("./.torch_hub_cache")
                _model_vad, _ = _torch.hub.load(
                    repo_or_dir  = "snakers4/silero-vad",
                    model        = "silero_vad",
                    force_reload = False,
                    trust_repo   = True,
                    verbose      = False,
                )
                log.info(
                    "STT  ✔  silero-vad ready  %.0f ms",
                    (time.perf_counter() - t0) * 1000,
                )
            except Exception as exc:
                log.warning(
                    "STT  ⚠  silero-vad failed (%s) — using energy VAD",
                    exc,
                )
                _model_vad = None

        # ── 2. Whisper ─────────────────────────────────────────────────────
        if not USE_GROQ_WHISPER:
            # ── SAFETY GUARD — block large models on CPU ──────────────────
            # .env may have WHISPER_MODEL=large-v3-turbo from old config.
            # Large models take 10+ minutes to load on CPU and will OOM.
            # config.py BEST_ASR_MODEL always overrides the .env value.
            _DANGEROUS_MODELS = {
                "large", "large-v1", "large-v2",
                "large-v3", "large-v3-turbo", "medium", "medium.en",
            }
            _env_model = os.getenv("WHISPER_MODEL", "")
            if _env_model.lower() in _DANGEROUS_MODELS:
                log.warning(
                    "STT  ⚠  .env WHISPER_MODEL=%s BLOCKED on CPU — "
                    "using config.BEST_ASR_MODEL=%s instead. "
                    "Comment out WHISPER_MODEL in .env to suppress this warning.",
                    _env_model, BEST_ASR_MODEL,
                )
            # Always use the config.py value — NEVER the raw .env value
            model_name = BEST_ASR_MODEL
            # ─────────────────────────────────────────────────────────────
            log.info(
                "STT  ▶  loading whisper-%s  threads=%d  workers=%d…",
                model_name, ASR_THREADS, ASR_WORKERS,
            )

            t0 = time.perf_counter()
            from faster_whisper import WhisperModel
            _model = WhisperModel(
                model_name,
                device       = ASR_DEVICE,
                compute_type = ASR_COMPUTE_TYPE,
                cpu_threads  = ASR_THREADS,
                num_workers  = ASR_WORKERS,
            )
            log.info(
                "STT  ✔  whisper-%s ready  %.0f ms",
                model_name, (time.perf_counter() - t0) * 1000,
            )

            # Pre-warm: silent pass so first real request pays no init cost
            try:
                silence = np.zeros(8000, dtype=np.float32)
                list(_model.transcribe(silence, language="en")[0])
                log.info("STT  ✔  model pre-warmed")
            except Exception as e:
                log.debug("STT  pre-warm skipped: %s", e)
        else:
            log.info("STT  ℹ  cloud STT mode — skipping local Whisper load")


# ── Audio collection ──────────────────────────────────────────────────────────

class AudioCollector:
    """Accumulates raw WebM/WAV bytes, locating the container header first."""

    def __init__(self):
        self._chunks:       list[bytes] = []
        self._header_found: bool        = False

    def add(self, chunk: bytes) -> None:
        if len(chunk) < 50:
            return
        if not self._header_found:
            for magic in (WEBM_MAGIC, WAV_MAGIC):
                idx = chunk.find(magic)
                if idx != -1:
                    self._header_found = True
                    self._chunks.append(chunk[idx:])
                    return
            return
        self._chunks.append(chunk)

    def assemble(self) -> bytes:
        return b"".join(self._chunks)

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)

    def reset(self) -> None:
        self._chunks       = []
        self._header_found = False

    @property
    def has_header(self) -> bool:
        return self._header_found

    @property
    def total_bytes(self) -> int:
        return sum(len(c) for c in self._chunks)


# ── Audio decoder — PyAV → pydub → raw PCM fallback chain ────────────────────

def _decode_to_pcm_sync(audio_bytes: bytes) -> np.ndarray | None:
    """
    Decode audio bytes to 16 kHz mono float32 PCM.

    BUG FIX: original code only tried PyAV with a bare except returning None.
    Any decode failure (partial WebM chunk, codec issue, missing container
    header) silently returned None, which meant VAD never fired and the
    entire turn hung waiting for an end-of-speech signal that never came.

    Now tries three methods in order:
      1. PyAV   — handles WebM/Opus from browser MediaRecorder
      2. pydub  — handles WAV, MP3, any ffmpeg-supported format
      3. raw    — last resort: treat bytes as raw int16 PCM at 16 kHz
    """
    # ── Method 1: PyAV ────────────────────────────────────────────────────
    try:
        import av
        container = av.open(io.BytesIO(audio_bytes))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        all_samples: list[np.ndarray] = []
        for frame in container.decode(audio=0):
            resampled = resampler.resample(frame)
            if resampled:
                for f in resampled:
                    all_samples.append(f.to_ndarray())
        container.close()
        if all_samples:
            return (
                np.concatenate(all_samples)
                .flatten()
                .astype(np.float32) / 32768.0
            )
    except Exception as e_av:
        log.debug("STT decode PyAV failed (%s) — trying pydub", e_av)

    # ── Method 2: pydub ───────────────────────────────────────────────────
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(io.BytesIO(audio_bytes))
        seg = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        arr = np.frombuffer(seg.raw_data, dtype=np.int16)
        return arr.astype(np.float32) / 32768.0
    except Exception as e_pd:
        log.debug("STT decode pydub failed (%s) — trying raw int16", e_pd)

    # ── Method 3: raw int16 PCM last resort ─────────────────────────────────────
    try:
        # FIX: int16 requires even byte count — trim last byte if odd
        trimmed = audio_bytes if len(audio_bytes) % 2 == 0 else audio_bytes[:-1]
        arr = np.frombuffer(trimmed, dtype=np.int16)
        if len(arr) > 0:
            return arr.astype(np.float32) / 32768.0
    except Exception as e_raw:
        log.error("STT decode all three methods failed: %s", e_raw)

    return None


async def _decode_to_pcm(audio_bytes: bytes) -> np.ndarray | None:
    """Async wrapper — runs sync decode in thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _decode_to_pcm_sync, audio_bytes)


# ── VAD decision ──────────────────────────────────────────────────────────────

def _is_speech(window: np.ndarray) -> tuple[bool, float]:
    """
    Return (is_speech, probability/rms).
    Silero when loaded, energy-RMS otherwise.

    CRITICAL FIX: silero-vad requires EXACTLY 512 samples per forward call
    at 16kHz. We chunk the window into 512-sample frames and take the max
    speech probability across all frames. This is the correct usage pattern.
    """
    if _model_vad is not None:
        try:
            import torch as _torch
            FRAME_LEN = 512  # silero-vad requirement at 16kHz
            max_prob = 0.0
            # Process each 512-sample frame
            for i in range(0, len(window) - FRAME_LEN + 1, FRAME_LEN):
                frame = window[i:i + FRAME_LEN]
                tensor = _torch.from_numpy(frame.copy()).unsqueeze(0)  # shape: (1, 512)
                prob = _model_vad(tensor, 16_000).item()
                if prob > max_prob:
                    max_prob = prob
            if max_prob > 0:
                return max_prob > 0.45, max_prob
        except Exception as exc:
            log.debug("STT VAD silero error: %s", exc)
    rms = float(np.sqrt(np.mean(window ** 2)))
    return rms > VAD_ENERGY_THRESHOLD, rms


# ── Streaming STT ─────────────────────────────────────────────────────────────

class StreamingSTT:
    """
    Event-driven STT: accumulates audio, applies VAD per chunk,
    returns True from process_chunk() when end-of-speech is detected.
    """

    # Re-run VAD decode every N new bytes to avoid CPU overload
    _DECODE_EVERY_BYTES = 3_000   # was 4000 — check VAD more frequently for faster EOS
    # Only decode trailing 64 KB (~2s of Opus at 64kbps) for VAD
    _WINDOW_BYTES       = 65_536

    def __init__(self):
        self.collector          = AudioCollector()
        self.in_speech          = False
        self.last_speech_time   = time.time()
        self._last_decode_bytes = 0
        self._silence_frames    = 0
        self._speech_frames     = 0

    async def process_chunk(self, chunk: bytes) -> bool:
        """
        Feed a raw audio chunk.
        Returns True when end-of-speech silence threshold is crossed.
        """
        self.collector.add(chunk)
        if not self.collector.has_header:
            return False

        current_bytes = self.collector.total_bytes
        if current_bytes - self._last_decode_bytes < self._DECODE_EVERY_BYTES:
            return False

        try:
            snapshot     = self.collector.snapshot()
            # BUG FIX: only decode trailing window, not entire blob
            window_bytes = (
                snapshot[-self._WINDOW_BYTES:]
                if len(snapshot) > self._WINDOW_BYTES
                else snapshot
            )
            pcm = await _decode_to_pcm(window_bytes)
            self._last_decode_bytes = current_bytes

            if pcm is None or len(pcm) == 0:
                return False

            # Use last 1-second window (16 000 samples @ 16 kHz) for VAD
            vad_window = pcm[-16_000:]
            if len(vad_window) < 512:
                return False

            is_speech, prob = _is_speech(vad_window)
            now = time.time()

            if is_speech:
                self._speech_frames  += 1
                self._silence_frames  = 0
                if self._speech_frames >= VAD_SPEECH_MIN_FRAMES:
                    if not self.in_speech:
                        log.debug("STT  VAD  speech start  prob=%.3f", prob)
                    self.in_speech        = True
                    self.last_speech_time = now
            elif self.in_speech:
                self._silence_frames += 1
                if self._silence_frames >= VAD_SILENCE_FRAMES:
                    sil_s = now - self.last_speech_time
                    log.info(
                        "STT  VAD  end-of-speech  prob=%.3f  silence=%.2fs",
                        prob, sil_s,
                    )
                    return True

        except Exception as exc:
            log.error("STT  VAD  error: %s", exc)

        return False


# ── Format detection ──────────────────────────────────────────────────────────

def _detect_format_name(audio_bytes: bytes) -> str:
    """
    BUG FIX: faster-whisper uses the filename hint to select a demuxer.
    Hardcoding "audio.webm" for WAV input caused silent transcription
    failures (empty output) because the wrong demuxer was chosen.
    """
    if len(audio_bytes) >= 4:
        if audio_bytes[:4] == WAV_MAGIC:
            return "audio.wav"
        if audio_bytes[:4] == WEBM_MAGIC:
            return "audio.webm"
    return "audio.webm"   # default — libav usually handles it anyway


# ── Hallucination detection ────────────────────────────────────────────────────

def _is_hallucination(text: str) -> bool:
    """
    Detect Whisper hallucination patterns and reject them.

    Whisper-tiny produces these when audio has no clear speech:
      - Repetitive numbers: '1, 2, 5, 25, 25, 25, 25, 25...'
      - Repeated words:     'the the the the the the the'
      - Very short garbage:  '.', '...', single char

    Returns True if text is likely a hallucination (should be discarded).
    """
    if not text:
        return False

    import re as _re
    from collections import Counter as _Counter

    stripped = text.strip()

    # Pattern 0: only punctuation / whitespace
    if not any(c.isalnum() for c in stripped):
        return True

    # Pattern 1: Repetitive comma-separated tokens (classic number hallucination)
    # e.g. '1, 2, 5, 25, 25, 25, 25, 25, 25, 25'
    tokens = [t.strip() for t in stripped.split(',') if t.strip()]
    if len(tokens) >= 8:
        counts = _Counter(tokens)
        most_common_val, most_common_n = counts.most_common(1)[0]
        if most_common_n / len(tokens) >= 0.35:
            return True
        # Also reject if all tokens are short numbers
        if all(_re.match(r'^\d{1,3}$', t) for t in tokens[:8]):
            return True

    # Pattern 2: Repeated word (e.g. 'the the the the')
    words = stripped.lower().split()
    if len(words) >= 6:
        word_counts = _Counter(words)
        top_word, top_count = word_counts.most_common(1)[0]
        # Very high repetition of ANY single word (including 'the') signals hallucination
        if top_count / len(words) >= 0.65:
            return True
        # Moderate repetition of non-common words
        if top_count / len(words) >= 0.55 and top_word not in {'i', 'a'}:
            return True

    # Pattern 3: Extremely long pure-digit string (no real speech would do this)
    digits_only = _re.sub(r'[\s,.]', '', stripped)
    if digits_only.isdigit() and len(digits_only) > 25:
        return True

    return False


_INITIAL_PROMPT = (
    "This is an L&T Finance payment verification call in India. "
    "Common responses: Yes, No, UPI, NEFT, RTGS, IMPS, cash, "
    "credit card, debit card, Google Pay, PhonePe, Paytm. "
    "Payment dates: January, February, March, April, May, June, "
    "July, August, September, October, November, December. "
    "Amounts in rupees: hundred, thousand, lakh. "
    "The caller may speak in Hindi or English."
)


# ── Transcription functions ───────────────────────────────────────────────────

def _run_local(model, audio_bytes: bytes) -> str:
    """Run Whisper transcription synchronously (called via thread executor)."""
    buf      = io.BytesIO(audio_bytes)
    buf.name = _detect_format_name(audio_bytes)   # BUG FIX
    try:
        segs, info = model.transcribe(
            buf,
            language                   = STT_LANGUAGE if STT_LANGUAGE != "auto" else None,
            beam_size                  = ASR_BEAM_SIZE,
            best_of                    = 1,
            temperature                = 0.0,
            vad_filter                 = True,
            vad_parameters             = {
                "min_silence_duration_ms": 200,
                "threshold": 0.35,
            },
            condition_on_previous_text = False,
            word_timestamps            = False,
            no_speech_threshold        = 0.6,
            initial_prompt             = _INITIAL_PROMPT,
        )
        text = " ".join(s.text.strip() for s in segs).strip()
        if _is_hallucination(text):
            log.warning("STT  ⚠  hallucination rejected (browser): %r", text[:60])
            return ""
        return text
    except Exception as exc:
        log.error("STT  ✘  local transcribe: %s", exc)
        return ""


async def _transcribe_groq(audio_bytes: bytes) -> str:
    """Cloud STT via Groq Whisper API."""
    import requests
    api_key = os.getenv("GROQ_API_KEY", "")
    try:
        fmt  = _detect_format_name(audio_bytes)
        mime = "audio/wav" if fmt.endswith(".wav") else "audio/webm"
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers = {"Authorization": f"Bearer {api_key}"},
                files   = {"file": (fmt, audio_bytes, mime)},
                data    = {"model": CLOUD_STT_MODEL},
                timeout = 10,
            ),
        )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        log.error(
            "STT  ✘  Groq HTTP %d: %s", resp.status_code, resp.text[:200]
        )
    except Exception as e:
        log.error("STT  ✘  Groq STT error: %s", e)
    return ""


async def transcribe_blob(audio_bytes: bytes) -> str:
    """
    Full transcription of the final assembled audio blob.
    Routes to cloud or local model depending on config.
    """
    if len(audio_bytes) < MIN_AUDIO_BYTES:
        return ""
    if USE_GROQ_WHISPER:
        return await _transcribe_groq(audio_bytes)
    if _model is None:
        raise RuntimeError("STT model not loaded — call load_model() at startup")
    return await asyncio.get_event_loop().run_in_executor(
        None, _run_local, _model, audio_bytes
    )


async def transcribe_partial(audio_bytes: bytes) -> str:
    """
    Fast partial transcription during recording (same shared model).
    No separate tiny/small model split needed.
    """
    if _model is None or len(audio_bytes) < MIN_AUDIO_BYTES:
        return ""
    return await asyncio.get_event_loop().run_in_executor(
        None, _run_local, _model, audio_bytes
    )


# ── Twilio PCM path ────────────────────────────────────────────────────────────

def _run_local_pcm(model, pcm_audio: np.ndarray) -> str:
    """
    Run Whisper directly on a float32 numpy array (no container file).
    faster-whisper accepts numpy arrays natively — used for Twilio calls
    where audio arrives as raw mulaw → PCM, with no WebM/WAV container.
    """
    # Minimum 300ms of audio — reject tiny fragments that cause hallucination
    MIN_SAMPLES = int(0.3 * 16_000)  # 4800 samples @ 16kHz
    if len(pcm_audio) < MIN_SAMPLES:
        log.debug("STT  PCM skipping — too short: %.2fs", len(pcm_audio) / 16000)
        return ""

    try:
        segs, info = model.transcribe(
            pcm_audio,
            language                   = STT_LANGUAGE if STT_LANGUAGE != "auto" else None,
            beam_size                  = ASR_BEAM_SIZE,
            best_of                    = 1,
            temperature                = 0.0,
            vad_filter                 = True,
            vad_parameters             = {
                "min_silence_duration_ms": 200,
                "threshold": 0.35,
            },
            condition_on_previous_text = False,
            word_timestamps            = False,
            no_speech_threshold        = 0.6,
            initial_prompt             = _INITIAL_PROMPT,
        )
        text = " ".join(s.text.strip() for s in segs).strip()
        if _is_hallucination(text):
            log.warning("STT  ⚠  hallucination rejected (PCM): %r", text[:60])
            return ""
        return text
    except Exception as exc:
        log.error("STT  ✘  local PCM transcribe: %s", exc)
        return ""


async def transcribe_pcm(pcm_audio: np.ndarray) -> str:
    """
    Transcribe raw float32 16 kHz PCM numpy array directly.

    Used for Twilio phone calls — audio arrives as mulaw 8kHz packets
    that we convert to float32 16kHz numpy arrays. No WebM/WAV container
    exists, so transcribe_blob() cannot be used here.

    Routes to cloud (Groq) or local Whisper depending on config.
    """
    if pcm_audio is None or len(pcm_audio) < 1600:   # < 0.1 s
        return ""

    if USE_GROQ_WHISPER:
        # Encode as WAV bytes for Groq API
        import soundfile as _sf
        buf = io.BytesIO()
        _sf.write(buf, pcm_audio, 16000, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return await _transcribe_groq(buf.read())

    # FIX 1: Lazy-load guard — model should be loaded by lifespan, but if
    # a Twilio call arrives before startup completes, load now rather than
    # crashing with RuntimeError which produces an empty transcript.
    global _model
    if _model is None:
        log.warning(
            "STT  ⚠  transcribe_pcm() called before model loaded — "
            "loading now (this should only happen once on very fast calls)"
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, load_model)
        if _model is None:
            log.error("STT  ✘  model still None after lazy load — returning empty")
            return ""

    return await asyncio.get_event_loop().run_in_executor(
        None, _run_local_pcm, _model, pcm_audio
    )