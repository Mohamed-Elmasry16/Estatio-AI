"""
Thin client for the hosted embedding model.
Supports batched embeddings, with a timeout and retry/backoff on transient
API errors (network blips, rate limits, timeouts) — the pipeline's tenacity
wrapper only covers DB errors, so the embedding stage protects itself.
"""

from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.config import get_settings

settings = get_settings()

REQUEST_TIMEOUT_SECONDS = 60
RETRY_ATTEMPTS = 3

_client = None


def _get_client() -> OpenAI:
    global _client

    if _client is None:
        if not settings.nvidia_api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is not set — required for the vector builder stage."
            )

        _client = OpenAI(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    return _client


@retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, max=10),
    retry=retry_if_exception_type((APITimeoutError, APIConnectionError, APIError)),
)
def _create_embeddings(texts: list[str], input_type: str) -> list[float]:
    response = _get_client().embeddings.create(
        model=settings.embedding_model,
        input=texts,
        extra_body={
            "input_type": input_type,
        },
    )
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


def embed_text(
    text: str,
    input_type: str = "passage",
) -> list[float]:
    return _create_embeddings([text], input_type)[0]


def embed_texts(
    texts: list[str],
    input_type: str = "passage",
) -> list[list[float]]:
    if not texts:
        return []
    return _create_embeddings(texts, input_type)
