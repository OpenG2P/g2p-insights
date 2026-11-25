"""Backend settings — extends openg2p_fastapi_common.Settings."""

from __future__ import annotations

from typing import List, Literal

from openg2p_fastapi_common.config import Settings as CommonSettings
from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict


class CostBand(BaseModel):
    rows: int
    # How much MORE work the plan does than simply reading the rows it must
    # read. See CostGateConfig for why this is a ratio and not an absolute.
    work_ratio: float


class CostGateConfig(BaseModel):
    """When to warn the user that a query is expensive.

    These were absolute planner-cost budgets, and that measured the wrong thing.
    Postgres costs a sequential scan in proportion to the size of the relation,
    so `SELECT COUNT(*) FROM fr_rpt_farmer` — the simplest question anyone can
    ask, one row out, milliseconds to run — priced at 2,144 against a budget of
    1,000 and was held back for confirmation. Nothing about it is expensive;
    the register merely has 100,000 rows in it, and on a real deployment it will
    have millions. Every registry-wide aggregate would have tripped the gate,
    and a warning that fires on almost every legitimate question teaches people
    to click through it — including on the queries it exists to catch.

    So cost is judged RELATIVE to the cheapest plan that could answer at all:
    the cost of reading the inputs. A plain aggregate over one scan lands at a
    ratio of about 1. Sorts, hash joins and nested loops push it up, and those
    are the plans actually worth warning about.

    `rows` still bounds the RESULT, which is a separate risk — the payload and
    the browser rather than the database.
    """

    green: CostBand = CostBand(rows=10_000, work_ratio=3.0)
    yellow: CostBand = CostBand(rows=1_000_000, work_ratio=25.0)
    # Absolute backstop in planner units. A plan can be perfectly efficient and
    # still be worth confirming if its inputs are enormous.
    max_scan_cost: int = 5_000_000


class PiiConfig(BaseModel):
    spacy_model: str = "en_core_web_lg"
    enabled_recognizers: List[str] = Field(
        default_factory=lambda: ["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION"]
    )


class EmbeddingsConfig(BaseModel):
    model_name: str = "all-MiniLM-L6-v2"
    cache_dir: str = "/tmp/insight-embeddings"


class LlmConfig(BaseModel):
    # Insight talks to exactly one provider: **OpenRouter** (an OpenAI-compatible
    # gateway). This keeps deployments simple — one key, one endpoint, any model
    # OpenRouter offers — and avoids per-vendor SDKs/keys.
    #
    # Master kill-switch. When False, Insight makes NO outbound LLM calls at
    # all: the client is never constructed, no API key is required, and the
    # natural-language endpoints return 503. Dashboards, Expert Lab, Catalog
    # and Audit continue to work. Use for air-gapped / data-sovereignty
    # deployments, or any site that does not want AI features enabled.
    #   INSIGHT_LLM__ENABLED=false
    enabled: bool = True
    # Any model id OpenRouter exposes, e.g. "openai/gpt-4o",
    # "qwen/qwen-2.5-72b-instruct", "meta-llama/llama-3.3-70b-instruct".
    #
    # Sonnet 4.5 is the default because the quality that matters here is
    # NL->SQL against an unfamiliar schema, where a weaker model's failure mode
    # is a query that parses, runs, and answers a subtly different question than
    # the one asked — which no amount of validation downstream can catch.
    # OpenRouter also exposes newer Sonnets (4.6, 5) if this proves not enough.
    model: str = "anthropic/claude-sonnet-4.5"
    max_tokens: int = 4096
    temperature: float = 0.0
    # OpenRouter API key (sk-or-...). May also be supplied via the
    # OPENROUTER_API_KEY environment variable.
    api_key: str = ""
    # OpenRouter's OpenAI-compatible base URL. Overridable only for testing
    # against a compatible gateway; leave as-is in normal use.
    base_url: str = "https://openrouter.ai/api/v1"
    # Qwen3 hybrid thinking. When True, prepend `/think` to the user message;
    # when False, prepend `/no_think`. Other models ignore the directive.
    enable_thinking: bool = False
    # Optional headers — OpenRouter uses HTTP-Referer + X-Title for app
    # attribution. Pass as e.g. INSIGHT_LLM__EXTRA_HEADERS='{"HTTP-Referer":"https://insight.qa2.openg2p.org","X-Title":"OpenG2P Insight"}'
    extra_headers: dict = {}


class KAnonymityConfig(BaseModel):
    default_k: int = 5


class SupersetConfig(BaseModel):
    url: str = ""
    auth_provider: Literal["db", "ldap"] = "db"
    username: str = ""
    password: str = ""


class MapsConfig(BaseModel):
    # Browser-reachable URL of the embedded Evidence.dev static site that powers
    # the Maps tab. Empty => the Maps tab is hidden.
    evidence_url: str = ""


class MasterDataConfig(BaseModel):
    url: str = ""
    sender_app_mnemonic: str = "g2p-insight"
    cache_ttl_seconds: int = 86_400


class Settings(CommonSettings):
    """
    Insight settings.

    Env layout (prefix INSIGHT_ with nested __ delimiter — note the SINGLE
    underscore after INSIGHT):
      INSIGHT_SUPERSET__URL=...
      INSIGHT_LLM__MODEL=anthropic/claude-sonnet-4.5
      INSIGHT_LLM__API_KEY=sk-or-...     (or OPENROUTER_API_KEY)
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

    # ---- The REGISTRY database (read-only) ---------------------------------
    # Where the reporting views live. Separate from the app's own database,
    # which holds sessions and the audit log and none of the registry's data —
    # the Ask tab plans SQL against the registry and must execute it there.
    #
    # Empty means "no registry configured": the planner then says so, rather
    # than defaulting to localhost and failing with a connection error that
    # names neither the database it wanted nor the one it tried.
    registry_db_hostname: str = ""
    registry_db_port: int = 5432
    registry_db_dbname: str = ""
    registry_db_username: str = ""
    registry_db_password: str = ""

    @property
    def registry_db_datasource(self) -> str:
        if not (self.registry_db_hostname and self.registry_db_dbname):
            return ""
        return (
            f"postgresql+asyncpg://{self.registry_db_username}:"
            f"{self.registry_db_password}@{self.registry_db_hostname}:"
            f"{self.registry_db_port}/{self.registry_db_dbname}"
        )

    @property
    def registry_db_summary(self) -> str:
        """Host and database only — never the password, this is logged."""
        return (f"{self.registry_db_username}@{self.registry_db_hostname}:"
                f"{self.registry_db_port}/{self.registry_db_dbname}")

    pii: PiiConfig = PiiConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    llm: LlmConfig = LlmConfig()
    cost_gate: CostGateConfig = CostGateConfig()
    k_anonymity: KAnonymityConfig = KAnonymityConfig()
    superset: SupersetConfig = SupersetConfig()
    masterdata: MasterDataConfig = MasterDataConfig()
    maps: MapsConfig = MapsConfig()
