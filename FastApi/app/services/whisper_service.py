from faster_whisper import WhisperModel
import requests
import tempfile
import os

# Try GPU first, fallback to CPU if CUDA is not available
try:
    model = WhisperModel(
        "large-v3",
        device="cuda",
        compute_type="float16"
    )
except Exception:
    model = WhisperModel(
        "large-v3",
        device="cpu",
        compute_type="int8"
    )


def transcribe_audio_from_url(audio_url: str):

    response = requests.get(audio_url, timeout=60)
    response.raise_for_status()

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(response.content)
            temp_path = tmp.name

        # 1️⃣ Original language transcription
        segments, info = model.transcribe(
            temp_path,
            beam_size=5,
            vad_filter=True,
            task="transcribe"
        )

        original_text = " ".join([segment.text for segment in segments]).strip()

        # 2️⃣ English translation
        segments_en, _ = model.transcribe(
            temp_path,
            beam_size=5,
            vad_filter=True,
            task="translate"
        )

        translated_text = " ".join([segment.text for segment in segments_en]).strip()

        return {
            "text": original_text,
            "translated_text": translated_text,
            "language": info.language
        }

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)