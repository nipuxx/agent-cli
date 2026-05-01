from types import SimpleNamespace

from nipux_cli.config import ModelConfig
from nipux_cli.llm import OpenAIChatLLM, _enrich_openrouter_generation_usage


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18, cost=0.00042)
        message = SimpleNamespace(content="ok", tool_calls=[])
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(id="gen_test", model="provider/model", choices=[choice], usage=usage)


def test_chat_llm_omits_redundant_tool_choice(monkeypatch):
    fake_completions = _FakeCompletions()
    monkeypatch.setenv("TEST_API_KEY", "test")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

        chat = SimpleNamespace(completions=fake_completions)

    monkeypatch.setattr("nipux_cli.llm.OpenAI", FakeOpenAI)

    llm = OpenAIChatLLM(ModelConfig(model="test/model", base_url="https://example.test/v1", api_key_env="TEST_API_KEY"))
    response = llm.next_action(messages=[{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "noop"}}])

    assert response.content == "ok"
    assert response.usage["prompt_tokens"] == 11
    assert response.usage["completion_tokens"] == 7
    assert response.usage["cost"] == 0.00042
    assert response.model == "provider/model"
    assert response.response_id == "gen_test"
    assert fake_completions.kwargs["tools"]
    assert "tool_choice" not in fake_completions.kwargs


def test_chat_llm_complete_response_returns_usage(monkeypatch):
    fake_completions = _FakeCompletions()
    monkeypatch.setenv("TEST_API_KEY", "test")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

        chat = SimpleNamespace(completions=fake_completions)

    monkeypatch.setattr("nipux_cli.llm.OpenAI", FakeOpenAI)

    llm = OpenAIChatLLM(ModelConfig(model="test/model", base_url="https://example.test/v1", api_key_env="TEST_API_KEY"))
    response = llm.complete_response(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert response.usage["prompt_tokens"] == 11
    assert response.usage["completion_tokens"] == 7
    assert response.usage["cost"] == 0.00042
    assert response.model == "provider/model"
    assert response.response_id == "gen_test"
    assert fake_completions.kwargs["model"] == "test/model"


def test_openrouter_generation_usage_enriches_cost_and_tokens(monkeypatch):
    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"data":{"total_cost":"0.0042","native_tokens_prompt":123,'
                b'"native_tokens_completion":45,"native_tokens_total":168}}'
            )

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeHTTPResponse()

    monkeypatch.setattr("nipux_cli.llm.urllib.request.urlopen", fake_urlopen)

    usage = _enrich_openrouter_generation_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "estimated": False},
        response_id="gen_123",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-test",
    )

    assert captured["url"] == "https://openrouter.ai/api/v1/generation?id=gen_123"
    assert captured["timeout"] == 5
    assert usage["cost"] == 0.0042
    assert usage["prompt_tokens"] == 123
    assert usage["completion_tokens"] == 45
    assert usage["total_tokens"] == 168
