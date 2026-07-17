from tavily import TavilyClient
import os

class TavilySearchService:
    def __init__(self):
        self.client = TavilyClient(
            api_key=os.getenv("TAVILY_API_KEY")
        )

    def search(self, query: str, top_k: int = 5, include_domains: list[str] | None=None):
        response = self.client.search(
            query=query,
            max_results=top_k,
            include_domains=include_domains,
            search_depth="advanced",
            include_raw_content=False
        )
        return response.get("results", [])
