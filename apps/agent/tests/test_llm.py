from reproagent_agent.config import AgentSettings
from reproagent_agent.llm.base import ChatMessage, LLMAdapter
from reproagent_agent.llm.factory import get_llm_adapter
from reproagent_agent.llm.qwen import QwenAdapter


def test_agent_settings_contain_no_supabase_or_service_keys():
    fields = set(AgentSettings.model_fields)
    assert not any("supabase" in name for name in fields)
    assert not any("service_role" in name for name in fields)


def test_factory_returns_qwen_adapter():
    settings = AgentSettings(
        qwen_api_key="sk-test",
        qwen_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        qwen_model="qwen-plus",
        _env_file=None,
    )
    adapter = get_llm_adapter(settings)
    assert isinstance(adapter, LLMAdapter)
    assert isinstance(adapter, QwenAdapter)
    assert adapter.model == "qwen-plus"
    assert str(adapter._client.base_url).startswith(
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )


def test_chat_message_validation():
    message = ChatMessage(role="user", content="Summarize this paper")
    assert message.role == "user"
