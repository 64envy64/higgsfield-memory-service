from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Read from environment variables.

    All knobs are tunable from compose.yml. Defaults are chosen so the service
    boots and `/health` returns 200 even with no OPENAI_API_KEY (lexical-only mode).
    """

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(
        default="postgresql://memory:memory@db:5432/memory",
        description="Postgres+pgvector DSN.",
    )
    auth_token: str = Field(
        default="",
        description="If non-empty, required as Authorization: Bearer <token> on protected endpoints.",
    )
    log_level: str = Field(default="INFO")

    # LLM / embeddings — primary path uses OpenAI.
    extraction_model: str = Field(default="gpt-4o-mini")
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_dim: int = Field(default=1536)

    # Hard ceilings.
    max_payload_bytes: int = Field(default=512 * 1024)         # 512KB body cap.
    max_turn_messages: int = Field(default=64)
    # Keep /turns under the evaluator's 60s timeout even when OpenAI is slow:
    # turn embedding and extraction run in parallel, then memory batch embedding
    # gets its own short budget. On timeout we fall back to lexical/rule paths.
    extraction_timeout_s: float = Field(default=25.0)

    # NOTE: an LLM reranker was scaffolded in earlier iterations and removed
    # in v0.9.2 (see PLAN.md §11 and CHANGELOG). Weighted RRF over heterogeneous
    # retrievers covers most of the precision win at zero LLM cost. Re-introduce
    # only with eval evidence that RRF is leaving precision on the table.

    # Recall tuning.
    default_recall_k: int = Field(default=12)
    rrf_k: int = Field(default=60)
    min_relevance_cosine: float = Field(default=0.30)         # gate threshold for vector hits

    @property
    def openai_api_key(self) -> str:
        """OPENAI_API_KEY is read from the env without the MEMORY_ prefix."""
        import os
        return os.environ.get("OPENAI_API_KEY", "").strip()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
