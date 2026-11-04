"""Typed settings, read from the environment (and a .env file when present).

Environment variable names are unchanged from earlier versions (SEC_USER_AGENT, DATABASE_URL, FINROBOT_MODEL_FIGURES, ...), so
existing .env files keep working. Models are written as ``[provider:]name``: ``gpt-oss:20b`` is an Ollama model,
``anthropic:claude-sonnet-5-5`` or ``openai:gpt-5`` select a hosted provider (install the matching extra).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"


def _find_root() -> Path:
    """The source checkout when running from one (pyproject.toml two levels above the package), else the working directory.
    Installed wheels must never write into site-packages."""
    candidate = PACKAGE_DIR.parents[1]
    return candidate if (candidate / "pyproject.toml").exists() else Path.cwd()


ROOT = _find_root()
# Langfuse and LangSmith read their own variables (LANGFUSE_*, LANGSMITH_*) straight from os.environ, so a .env file must be exported.
load_dotenv(ROOT / ".env")


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", populate_by_name=True, protected_namespaces=(),
                                      env_ignore_empty=True)   # `LANGSMITH_TRACING=` left blank in a .env must mean "unset", not a crash

    # --- runtime -------------------------------------------------------------------------------
    environment: Literal["development", "test", "production"] = Field("development", validation_alias=_alias("FINROBOT_ENV"))
    log_level: str = Field("INFO", validation_alias=_alias("FINROBOT_LOG_LEVEL"))
    log_json: bool = Field(False, validation_alias=_alias("FINROBOT_LOG_JSON"))

    # --- data sources --------------------------------------------------------------------------
    sec_user_agent: str = Field("", validation_alias=_alias("SEC_USER_AGENT"))
    alphavantage_key: str = Field("", validation_alias=_alias("ALPHAVANTAGE_API_KEY"))

    # --- storage -------------------------------------------------------------------------------
    database_url: str = Field("postgresql://finrobot:finrobot@127.0.0.1:5433/finrobot", validation_alias=_alias("DATABASE_URL"))
    db_pool_min: int = Field(1, ge=0, validation_alias=_alias("FINROBOT_DB_POOL_MIN"))
    db_pool_max: int = Field(8, ge=1, validation_alias=_alias("FINROBOT_DB_POOL_MAX"))
    s3_endpoint: str = Field("127.0.0.1:9000", validation_alias=_alias("S3_ENDPOINT"))
    s3_access_key: str = Field("finrobot", validation_alias=_alias("S3_ACCESS_KEY"))
    s3_secret_key: SecretStr = Field(SecretStr("finrobot-secret"), validation_alias=_alias("S3_SECRET_KEY"))
    s3_bucket: str = Field("finrobot", validation_alias=_alias("S3_BUCKET"))
    s3_secure: bool = Field(False, validation_alias=_alias("S3_SECURE"))
    output_dir: Path = Field(ROOT / "output", validation_alias=_alias("FINROBOT_OUTPUT_DIR"))
    cache_dir: Path = Field(ROOT / "data_cache", validation_alias=_alias("FINROBOT_CACHE_DIR"))

    # --- models --------------------------------------------------------------------------------
    ollama_host: str = Field("http://127.0.0.1:11434", validation_alias=_alias("OLLAMA_HOST"))
    model_figures: str = Field("gpt-oss:20b", validation_alias=_alias("FINROBOT_MODEL_FIGURES"))
    model_narrative: str = Field("gemma4:e4b", validation_alias=_alias("FINROBOT_MODEL_NARRATIVE"))
    embed_model: str = Field("nomic-embed-text", validation_alias=_alias("FINROBOT_EMBED_MODEL"))
    embed_dim: int = Field(768, validation_alias=_alias("FINROBOT_EMBED_DIM"))
    embed_doc_prefix: str = Field("search_document: ", validation_alias=_alias("FINROBOT_EMBED_DOC_PREFIX"))
    embed_query_prefix: str = Field("search_query: ", validation_alias=_alias("FINROBOT_EMBED_QUERY_PREFIX"))
    num_ctx: int = Field(8192, validation_alias=_alias("FINROBOT_NUM_CTX"))
    llm_timeout: float = Field(600.0, gt=0, validation_alias=_alias("FINROBOT_LLM_TIMEOUT"))
    llm_max_retries: int = Field(3, ge=1, validation_alias=_alias("FINROBOT_LLM_MAX_RETRIES"))
    llm_concurrency: int = Field(1, ge=1, validation_alias=_alias("FINROBOT_LLM_CONCURRENCY"))   # 1 = a single GPU slot
    grounding_check: bool = Field(True, validation_alias=_alias("FINROBOT_GROUNDING_CHECK"))

    # --- API -----------------------------------------------------------------------------------
    api_keys: str = Field("", validation_alias=_alias("FINROBOT_API_KEYS"))          # comma separated; empty = auth off (dev only)
    cors_origins: str = Field("", validation_alias=_alias("FINROBOT_CORS_ORIGINS"))  # comma separated
    max_workers: int = Field(1, ge=1, validation_alias=_alias("FINROBOT_MAX_WORKERS"))
    max_queue: int = Field(20, ge=1, validation_alias=_alias("FINROBOT_MAX_QUEUE"))
    max_peers: int = Field(10, ge=0)

    # --- observability (the SDKs read their own LANGFUSE_* / LANGSMITH_* variables; these are for our own switches) ---
    langfuse_public_key: str = Field("", validation_alias=_alias("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: SecretStr = Field(SecretStr(""), validation_alias=_alias("LANGFUSE_SECRET_KEY"))
    langsmith_tracing: bool = Field(False, validation_alias=_alias("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"))
    langsmith_api_key: SecretStr = Field(SecretStr(""), validation_alias=_alias("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"))
    langsmith_project: str = Field("finrobot", validation_alias=_alias("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"))
    trace_inputs: bool = Field(True, validation_alias=_alias("FINROBOT_TRACE_INPUTS"))   # false: send metadata and scores only

    @field_validator("output_dir", "cache_dir", mode="before")
    @classmethod
    def _path(cls, v):
        return Path(v).expanduser()

    # --- derived ---------------------------------------------------------------------------------
    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key.get_secret_value())

    @property
    def langsmith_enabled(self) -> bool:
        return bool(self.langsmith_tracing and self.langsmith_api_key.get_secret_value())

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def model_for(self, role: str) -> str:
        """Roles: 'figures' (numbers, fact-checks, routing) or 'narrative' (prose)."""
        return self.model_figures if role == "figures" else self.model_narrative

    def production_problems(self) -> list[str]:
        """Things that must not be left at their development defaults when serving real traffic."""
        problems = []
        placeholder = "change-me"   # the value .env.example ships with
        if not self.api_key_list:
            problems.append("FINROBOT_API_KEYS is empty: the API would be unauthenticated")
        elif any(placeholder in k for k in self.api_key_list):
            problems.append("FINROBOT_API_KEYS still has the placeholder from .env.example")
        s3 = self.s3_secret_key.get_secret_value()
        if s3 == "finrobot-secret" or self.s3_access_key == "finrobot" or placeholder in s3 or placeholder in self.s3_access_key:
            problems.append("S3_ACCESS_KEY / S3_SECRET_KEY are the development defaults or placeholders")
        if "finrobot:finrobot@" in self.database_url or placeholder in self.database_url:
            problems.append("DATABASE_URL uses the development password or a placeholder")
        if not self.sec_user_agent:
            problems.append("SEC_USER_AGENT is empty: SEC EDGAR requires contact details")
        return problems


settings = Settings()
