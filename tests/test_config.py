from finrobot.config import Settings


def make(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_existing_environment_variable_names_still_work(monkeypatch):
    s = make(monkeypatch, DATABASE_URL="postgresql://u:p@db/x", FINROBOT_MODEL_FIGURES="anthropic:claude-sonnet-5-5", OLLAMA_HOST="http://o:1",
             S3_SECRET_KEY="hunter2", FINROBOT_NUM_CTX="4096", FINROBOT_GROUNDING_CHECK="0", SEC_USER_AGENT="FinRobot me@example.com")
    assert s.database_url == "postgresql://u:p@db/x" and s.model_for("figures") == "anthropic:claude-sonnet-5-5"
    assert s.model_for("narrative") == "gemma4:e4b" and s.num_ctx == 4096 and s.grounding_check is False and s.ollama_host == "http://o:1"


def test_secrets_never_appear_in_a_repr(monkeypatch):
    s = make(monkeypatch, S3_SECRET_KEY="hunter2", LANGFUSE_SECRET_KEY="sk-lf-1", LANGSMITH_API_KEY="ls-1")
    assert "hunter2" not in repr(s) and "sk-lf-1" not in repr(s) and "ls-1" not in repr(s)


def test_tracing_backends_switch_on_only_with_their_keys(monkeypatch):
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGSMITH_API_KEY", "LANGSMITH_TRACING"):
        monkeypatch.setenv(k, "")
    assert not Settings(_env_file=None).langfuse_enabled and not Settings(_env_file=None).langsmith_enabled
    s = make(monkeypatch, LANGFUSE_PUBLIC_KEY="pk", LANGFUSE_SECRET_KEY="sk")
    assert s.langfuse_enabled and not s.langsmith_enabled
    assert not make(monkeypatch, LANGSMITH_TRACING="true").langsmith_enabled          # tracing flag without a key is not enough
    assert make(monkeypatch, LANGSMITH_TRACING="true", LANGSMITH_API_KEY="k").langsmith_enabled


def test_production_refuses_development_defaults(monkeypatch):
    for k in ("FINROBOT_API_KEYS", "S3_ACCESS_KEY", "S3_SECRET_KEY", "DATABASE_URL", "SEC_USER_AGENT"):
        monkeypatch.delenv(k, raising=False)
    problems = Settings(_env_file=None).production_problems()
    assert len(problems) == 4
    ok = make(monkeypatch, FINROBOT_API_KEYS="k1, k2", S3_ACCESS_KEY="ak", S3_SECRET_KEY="sk", DATABASE_URL="postgresql://app:s3cret@db/finrobot",
              SEC_USER_AGENT="FinRobot me@example.com")
    assert ok.production_problems() == [] and ok.api_key_list == ["k1", "k2"]


def test_production_refuses_the_placeholders_shipped_in_env_example(monkeypatch):
    s = make(monkeypatch, FINROBOT_API_KEYS="change-me-api-key", S3_ACCESS_KEY="change-me-access", S3_SECRET_KEY="change-me-secret-key-min-8",
             DATABASE_URL="postgresql://finrobot:change-me-postgres@db/finrobot", SEC_USER_AGENT="FinRobot me@example.com")
    assert len(s.production_problems()) == 3
