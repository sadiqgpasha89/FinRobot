import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from fakes import DIM, FakeEmbeddings, ScriptedChatModel, make_gateway
from finrobot.config import Settings
from finrobot.llm.gateway import LLMError, LLMGateway, split_spec, text_of
from finrobot.llm.parsing import parse_json
from finrobot.llm.tools import Tool, render_result


class Out(BaseModel):
    answer: str
    n: int = 0


def test_model_spec_defaults_to_ollama_and_accepts_a_provider_prefix():
    assert split_spec("gpt-oss:20b") == ("ollama", "gpt-oss:20b")
    assert split_spec("gemma4:e4b") == ("ollama", "gemma4:e4b")
    assert split_spec("ollama:qwen3:8b") == ("ollama", "qwen3:8b")
    assert split_spec("anthropic:claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert split_spec("openai:gpt-5") == ("openai", "gpt-5")


def test_text_of_handles_string_and_block_content():
    assert text_of(AIMessage("plain")) == "plain"
    assert text_of(AIMessage(content=[{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "an"}, "swer"])) == "answer"


def test_ollama_models_are_configured_per_role():
    gw = LLMGateway(Settings(_env_file=None, ollama_host="http://h:1/", num_ctx=4096, llm_timeout=12))
    figures, narrative = gw.chat_model("figures", 0.0), gw.chat_model("narrative", 0.3)
    assert (figures.model, figures.reasoning, figures.num_ctx, figures.base_url) == ("gpt-oss:20b", "low", 4096, "http://h:1")   # effort level, trailing slash trimmed
    assert (narrative.model, narrative.reasoning, narrative.temperature) == ("gemma4:e4b", False, 0.3)                           # thinking off


def test_the_real_ollama_embeddings_client_is_built_from_settings():
    """The fakes bypass this constructor; a live run found it rejected keep_alive='10m' (it takes seconds as an int)."""
    emb = LLMGateway(Settings(_env_file=None, ollama_host="http://h:1/", embed_model="nomic-embed-text")).embeddings()
    assert (emb.model, emb.base_url, emb.keep_alive) == ("nomic-embed-text", "http://h:1", 600)


def test_a_hosted_provider_without_its_package_gives_an_actionable_error():
    gw = LLMGateway(Settings(_env_file=None, model_narrative="anthropic:claude-sonnet-5-5"))
    with pytest.raises(LLMError, match="uv sync --extra anthropic"):
        gw.chat_model("narrative")


def test_structured_output_returns_a_validated_model():
    gw, _ = make_gateway(figures=['{"answer": "42", "n": 3}'])
    out = gw.structured("figures", Out, [HumanMessage("q")])
    assert out == Out(answer="42", n=3)


def test_structured_output_falls_back_to_json_in_the_raw_text():
    class Sloppy(ScriptedChatModel):
        def with_structured_output(self, schema, *, include_raw=False, **kw):   # the provider failed to parse; the text has JSON in it
            from langchain_core.runnables import RunnableLambda
            return RunnableLambda(lambda m: {"raw": AIMessage('Sure! ```json\n{"answer": "ok"}\n```'), "parsed": None, "parsing_error": ValueError("x")})
    gw = LLMGateway(Settings(_env_file=None), chat_factory=lambda r, t: Sloppy())
    assert gw.structured("figures", Out, [HumanMessage("q")]) == Out(answer="ok")


def test_structured_output_is_none_when_the_model_cannot_produce_it():
    gw, _ = make_gateway(figures=["not json at all"])
    assert gw.structured("figures", Out, [HumanMessage("q")]) is None


def test_transient_errors_are_retried_and_others_are_not():
    gw, models = make_gateway(figures=[httpx.ConnectError("refused"), "recovered"])
    assert gw.invoke("figures", [HumanMessage("hi")]).content == "recovered"
    assert len(models["figures"].seen) == 2
    gw2, models2 = make_gateway(figures=[ValueError("bad request"), "never reached"])
    with pytest.raises(ValueError):
        gw2.invoke("figures", [HumanMessage("hi")])
    assert len(models2["figures"].seen) == 1


def test_tools_are_bound_only_when_given():
    gw, models = make_gateway(figures=["a", "b"])
    gw.invoke("figures", [HumanMessage("x")])
    gw.invoke("figures", [HumanMessage("x")], tools=[Tool("t", "d", {"type": "object", "properties": {}}, lambda: {})])
    assert [c["tools"] for c in models["figures"].seen] == [[], ["t"]]


def test_embeddings_are_batched_prefixed_and_dimension_checked():
    gw, _ = make_gateway()
    vectors = gw.embed([f"text {i}" for i in range(70)], "document")
    emb: FakeEmbeddings = gw.embeddings()   # type: ignore[assignment]
    assert [len(c) for c in emb.calls] == [32, 32, 6] and len(vectors) == 70 and len(vectors[0]) == DIM
    assert emb.calls[0][0] == "search_document: text 0"
    gw.embed(["q"], "query")
    assert emb.calls[-1] == ["search_query: q"]
    assert gw.embed([], "document") == []
    bad = LLMGateway(Settings(_env_file=None, embed_dim=10), embeddings=FakeEmbeddings())
    with pytest.raises(LLMError, match="768 dimensions"):
        bad.embed(["x"])


def test_health_reports_missing_models_and_an_unreachable_server(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "gpt-oss:20b"}, {"name": "nomic-embed-text:latest"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    h = LLMGateway(Settings(_env_file=None)).health()
    assert h["figures"].endswith("ok") and h["narrative"].endswith("NOT INSTALLED") and h["embed"].endswith("ok") and h["ok"] is False

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    h = LLMGateway(Settings(_env_file=None)).health()
    assert "unreachable" in h["ollama"] and h["ok"] is False


def test_parse_json_tolerates_fences_prose_and_garbage():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    assert parse_json("```\nno braces here\n```") is None          # used to slice off all but the last character
    assert parse_json("[1, 2]") is None and parse_json("") is None


def test_tool_results_are_truncated_with_a_visible_marker():
    assert render_result({"a": 1}) == '{"a": 1}'
    long = render_result({"x": "y" * 9000})
    assert len(long) < 6100 and long.endswith("…[truncated]")
