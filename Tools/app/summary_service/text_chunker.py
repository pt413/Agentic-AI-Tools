import re
from typing import List
import os

MAX_WORDS = int(os.getenv("MODEL_MAX_WORDS", 500))

def chunk_text(text: str, max_words: int = MAX_WORDS) -> List[str]:
    """
    Splits text into sentence-aware chunks based on max_words.
    Faster single-pass version.
    """
    chunks = []
    current_chunk = []
    current_words = 0

    for sentence in re.split(r'(?<=[.!?])\s+', text):
        word_count = len(sentence.split())
        if current_words + word_count > max_words:
            if current_chunk:
                chunks.append(' '.join(current_chunk))
            current_chunk = [sentence]
            current_words = word_count
        else:
            current_chunk.append(sentence)
            current_words += word_count

    if current_chunk:
        chunks.append(' '.join(current_chunk))

    return chunks
