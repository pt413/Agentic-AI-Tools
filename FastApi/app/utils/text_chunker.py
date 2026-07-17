# utils/text_chunker.py

from typing import List

def chunk_text(
    text: str, 
    max_chars: int = 1000, 
    preserve_paragraphs: bool = True
) -> List[str]:
    """
    Split large text into chunks of at most max_chars.
    If preserve_paragraphs=True, tries not to split mid-paragraph.
    """
    if not text:
        return []

    if preserve_paragraphs:
        paragraphs = text.split("\n")
    else:
        paragraphs = [text]

    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If adding this paragraph exceeds limit, start new chunk
        if len(current_chunk) + len(para) + 1 > max_chars:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = para
        else:
            if current_chunk:
                current_chunk += "\n" + para
            else:
                current_chunk = para

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks
