import pyttsx3
import re
import io
import asyncio
import tempfile
import os
from threading import Timer

async def generate_tts(text: str) -> bytes:
    """Generate speech from text using pyttsx3 and return WAV audio bytes."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_tts, text)

def _sync_tts(text: str) -> bytes:
    """Synchronous pyttsx3 TTS function returning WAV audio bytes with auto-delete after 3 mins."""
    clean_text = re.sub(r"[*_`#>\[\]\(\)]", "", text)
    # clean_text = re.sub(r"\s{2,}", " ", clean_text).strip()
    
    engine = pyttsx3.init()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
        temp_file_name = tmp_file.name

    engine.save_to_file(clean_text, temp_file_name)
    engine.runAndWait()

    with open(temp_file_name, "rb") as f:
        audio_bytes = f.read()

    Timer(180, lambda: os.path.exists(temp_file_name) and os.remove(temp_file_name)).start()

    return audio_bytes
