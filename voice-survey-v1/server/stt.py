"""
stt.py — Deepgram Nova-2 batch transcription wrapper.
Sends raw audio bytes and returns the transcript string.
"""

import os
from deepgram import DeepgramClient, PrerecordedOptions, BufferSource


async def transcribe(audio_bytes: bytes, mime_type: str = "audio/wav") -> str:
    """
    Transcribe audio bytes using Deepgram Nova-2 (batch / REST).

    Args:
        audio_bytes: Raw audio data (WAV, WebM, MP4, OGG …)
        mime_type:   MIME type of the audio buffer.

    Returns:
        Transcript string, or empty string on failure.
    """
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise EnvironmentError("DEEPGRAM_API_KEY is not set in the environment.")

    client = DeepgramClient(api_key)

    payload: BufferSource = {"buffer": audio_bytes}

    options = PrerecordedOptions(
        model="nova-2",
        language="en-US",
        punctuate=True,
        smart_format=True,
    )

    try:
        response = await client.listen.asyncprerecorded.v("1").transcribe_file(
            payload, options
        )
        transcript = (
            response.results.channels[0].alternatives[0].transcript
        )
        return transcript.strip()
    except Exception as exc:
        raise RuntimeError(f"Deepgram transcription failed: {exc}") from exc