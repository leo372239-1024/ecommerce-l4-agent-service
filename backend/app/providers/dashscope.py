import math
from dataclasses import dataclass

import httpx

from app.core.config import Settings


class ProviderError(RuntimeError):
    """Safe error: never include API keys, request bodies, or response bodies."""


@dataclass(frozen=True)
class RankedDocument:
    index: int
    score: float


class DashScopeClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def _post(self, path: str, body: dict) -> dict:
        key = self.settings.dashscope_api_key.get_secret_value()
        if not key:
            raise ProviderError("DASHSCOPE_API_KEY is not configured")
        try:
            base = (
                self.settings.dashscope_embedding_api_base
                if path == "embeddings"
                else self.settings.dashscope_api_base
            )
            response = await self.client.post(
                f"{base.rstrip('/')}/{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=self.settings.provider_timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise ProviderError(f"dashscope transport failure: {type(error).__name__}") from None
        if not response.is_success:
            raise ProviderError(f"dashscope HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("dashscope invalid JSON") from None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not 1 <= len(texts) <= 10 or any(not text.strip() for text in texts):
            raise ValueError("embedding requires 1-10 nonempty texts")
        payload = await self._post(
            "embeddings",
            {
                "model": self.settings.embedding_model,
                "input": texts,
                "dimensions": self.settings.embedding_dimensions,
                "encoding_format": "float",
            },
        )
        try:
            rows = payload["data"]
            if len(rows) != len(texts):
                raise ValueError
            by_index = {}
            for row in rows:
                index = row["index"]
                vector = row["embedding"]
                if type(index) is not int or index not in range(len(texts)) or index in by_index:
                    raise ValueError
                if len(vector) != self.settings.embedding_dimensions:
                    raise ValueError
                if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                    raise ValueError
                by_index[index] = vector
            return [by_index[index] for index in range(len(texts))]
        except (KeyError, TypeError, ValueError):
            raise ProviderError("dashscope embedding response contract mismatch") from None

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 5
    ) -> list[RankedDocument]:
        if not query.strip() or not documents or any(not text.strip() for text in documents):
            raise ValueError("rerank requires query and nonempty documents")
        if not 1 <= top_n <= len(documents):
            raise ValueError("top_n must be within candidate count")
        payload = await self._post(
            "reranks",
            {
                "model": self.settings.reranker_model,
                "documents": documents,
                "query": query,
                "top_n": top_n,
                "instruct": "Given a web search query, retrieve relevant passages that answer the query.",
            },
        )
        try:
            results = payload["results"]
            if len(results) != top_n:
                raise ValueError
            seen = set()
            ranked = []
            for result in results:
                index, score = result["index"], result["relevance_score"]
                if type(index) is not int or index not in range(len(documents)) or index in seen:
                    raise ValueError
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError
                seen.add(index)
                ranked.append(RankedDocument(index, float(score)))
            return ranked
        except (KeyError, TypeError, ValueError):
            raise ProviderError("dashscope rerank response contract mismatch") from None
