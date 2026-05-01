from types import SimpleNamespace

from nipux_cli.config import ModelConfig
from nipux_cli.llm import OpenAIChatLLM


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
