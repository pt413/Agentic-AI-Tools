def normalize_tavily_results(results):
    chunks = []
    for r in results:
        content = r.get("content")
        if not content:
            continue

        chunks.append({
            "source": "web:tavily",
            "chunks": content,
            "metadata": {
                "title": r.get("title"),
                "url": r.get("url")
            }
        })
    return chunks
