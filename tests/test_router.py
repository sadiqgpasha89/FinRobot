from finrobot.agents.qa_agents import parse_period, route


def FakeLLM(content):
    """A real LLMGateway whose 'figures' model replies with `content`."""
    from fakes import make_gateway
    return make_gateway(figures=[content])[0]


def test_period_parsing_is_done_in_code():
    assert parse_period("EBITDA in Q3 versus a year ago") == (None, 3)
    assert parse_period("Q2 2025 EBITDA miss") == (2025, 2)
    assert parse_period("the fourth quarter of FY2024") == (2024, 4)
    assert parse_period("how is the company doing") == (None, None)


def test_router_never_lets_an_invented_year_into_a_subquestion():
    """Regression: the router rewrote 'Q3 versus a year ago' as 'Q3 2024 ...' and Data-CoT answered about 2024."""
    llm = FakeLLM('{"company": "WM", "data_question": "What were EBITDA in Q3 2024 vs Q3 2023?", '
                  '"concept_question": "Why did EBITDA change?", "thesis_question": "Tell the story of FY2024."}')
    r = route(llm, "How did EBITDA change in Q3 versus a year ago?", "WM")
    assert "2024" not in r["data_question"] and "2024" not in r["thesis_question"]
    assert r["concept_question"] == "Why did EBITDA change?"          # clean sub-questions are kept
    assert (r["fiscal_year"], r["fiscal_quarter"]) == (None, 3)


def test_years_the_user_wrote_are_kept():
    llm = FakeLLM('{"company": "WM", "data_question": "EBITDA in Q2 2025?", "concept_question": "why", "thesis_question": "story"}')
    assert route(llm, "EBITDA in Q2 2025?", "WM")["data_question"] == "EBITDA in Q2 2025?"


def test_only_why_questions_run_the_explanation_agents():
    from finrobot.agents.qa_agents import wants_explanation
    for q in ("Why did EBITDA fall in Q2?", "What drove the margin expansion?", "Explain the miss", "What are the main reasons for the change?"):
        assert wants_explanation(q), q
    for q in ("What is net debt relative to EBITDA?", "What was revenue growth in Q1 year over year?", "How leveraged is WM?"):
        assert not wants_explanation(q), q


def test_structured_schemas_require_every_field():
    """A schema without `required` lets a model omit fields; gpt-oss left `company` out of the router's answer in a live run."""
    from finrobot.agents.grounding import GroundingOut
    from finrobot.agents.qa_agents import RouterOut
    from finrobot.agents.sections.writers import RiskRow, RisksOut
    for model in (RouterOut, GroundingOut, RisksOut, RiskRow):
        schema = model.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"]), model
