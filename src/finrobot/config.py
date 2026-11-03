"""Settings, read from the environment (and a .env file when present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    sec_user_agent: str = field(default_factory=lambda: _env("SEC_USER_AGENT"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://finrobot:finrobot@127.0.0.1:5433/finrobot"))
    s3_endpoint: str = field(default_factory=lambda: _env("S3_ENDPOINT", "127.0.0.1:9000"))
    s3_access_key: str = field(default_factory=lambda: _env("S3_ACCESS_KEY", "finrobot"))
    s3_secret_key: str = field(default_factory=lambda: _env("S3_SECRET_KEY", "finrobot-secret"))
    s3_bucket: str = field(default_factory=lambda: _env("S3_BUCKET", "finrobot"))
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model_figures: str = field(default_factory=lambda: _env("FINROBOT_MODEL_FIGURES", "gpt-oss:20b"))
    model_narrative: str = field(default_factory=lambda: _env("FINROBOT_MODEL_NARRATIVE", "gemma4:e4b"))
    embed_model: str = field(default_factory=lambda: _env("FINROBOT_EMBED_MODEL", "nomic-embed-text"))
    embed_dim: int = 768
    num_ctx: int = field(default_factory=lambda: int(_env("FINROBOT_NUM_CTX", "8192")))
    alphavantage_key: str = field(default_factory=lambda: _env("ALPHAVANTAGE_API_KEY"))
    output_dir: Path = field(default_factory=lambda: Path(_env("FINROBOT_OUTPUT_DIR", str(ROOT / "output"))))
    cache_dir: Path = field(default_factory=lambda: Path(_env("FINROBOT_CACHE_DIR", str(ROOT / "data_cache"))))

    def model_for(self, role: str) -> str:
        """Roles: 'figures' (numbers, fact-checks) or 'narrative' (prose, routing of ideas)."""
        return self.model_figures if role == "figures" else self.model_narrative


settings = Settings()
