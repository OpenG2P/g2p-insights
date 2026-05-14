"""Backend settings — extends openg2p_fastapi_common.Settings."""

from __future__ import annotations

from typing import List, Literal

from openg2p_fastapi_common.config import Settings as CommonSettings
from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict


class CostBand(BaseModel):
    rows: int
    cost: int


class CostGateConfig(BaseModel):
    green: CostBand = CostBand(rows=10_000, cost=1_000)
    yellow: CostBand = CostBand(rows=1_000_000, cost=100_000)


class PiiConfig(BaseModel):
    spacy_model: str = "en_core_web_lg"
    enabled_recognizers: List[str] = Field(
        default_factory=lambda: ["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION"]
    )


class EmbeddingsConfig(BaseModel):
    model_name: str = "all-MiniLM-L6-v2"
    cache_dir: str = "/tmp/insight-embeddings"


class LlmConfig(BaseModel):
    # "anthropic"         — native Anthropic API (Claude).
    # "openai_compatible" — any OpenAI-compatible endpoint (OpenRouter, Groq,
    #                       self-hosted vLLM, Together, …). Set `base_url`.
    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    model: str = "claude-opus-4-7"
    max_tokens: int = 4096
    temperature: float = 0.0
    # For anthropic: ANTHROPIC_API_KEY. For openai_compatible: that provider's
    # key (e.g. an OpenRouter `sk-or-...`).
    api_key: str = ""
    # Used only by openai_compatible. e.g. "https://openrouter.ai/api/v1".
    base_url: str = ""
    # Qwen3 hybrid thinking. When True, prepend `/think` to the user message;
    # when False, prepend `/no_think`. Other models ignore the directive.
    enable_thinking: bool = False
    # Optional headers — OpenRouter wants HTTP-Referer + X-Title for app
    # attribution. Pass as e.g. INSIGHT_LLM__EXTRA_HEADERS='{"HTTP-Referer":"https://insight.qa2.openg2p.org","X-Title":"OpenG2P Insight"}'
    extra_headers: dict = {}


class KAnonymityConfig(BaseModel):
    default_k: int = 5


class SupersetConfig(BaseModel):
    url: str = ""
    auth_provider: Literal["db", "ldap"] = "db"
    username: str = ""
    password: str = ""


class MasterDataConfig(BaseModel):
    url: str = ""
    sender_app_mnemonic: str = "g2p-insight"
    cache_ttl_seconds: int = 86_400


class Settings(CommonSettings):
    """
    Insight settings.

    Env layout (with prefix INSIGHT__ and nested __ delimiter):
      INSIGHT__SUPERSET__URL=...
      INSIGHT__LLM__MODEL=claude-opus-4-7
      ANTHROPIC_API_KEY=...        (read into llm.api_key)
    """

    model_config = SettingsConfigDict(
        env_prefix="insight_",
        env_file=".env",
        extra="allow",
        env_nested_delimiter="__",
    )

    openapi_title: str = "OpenG2P Insight"
    openapi_description: str = "AI-driven analytics for OpenG2P registries"
    openapi_version: str = "0.1.0"
    openapi_common_api_prefix: str = "/v1/insight"

    service_id: str = "openg2p.insight"
    api_version: str = "1.0"

    catalog_path: str = "/app/catalog"

    pii: PiiConfig = PiiConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    llm: LlmConfig = LlmConfig()
    cost_gate: CostGateConfig = CostGateConfig()
    k_anonymity: KAnonymityConfig = KAnonymityConfig()
    superset: SupersetConfig = SupersetConfig()
    masterdata: MasterDataConfig = MasterDataConfig()
