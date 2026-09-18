"""生成退避重试契约测试：Settings 默认值与 OpenAI client 的 max_retries/timeout 透传。

背景：429 限流 / 5xx 服务端错误属瞬时故障，openai SDK 默认重试 2 次；
项目里显式设到 3 次并给足总超时（视觉请求上传几十 MB base64、长文档
生成可能耗时数分钟）。失败/被拒请求不计费，重试安全。
"""
from app.config import Settings
from app.services.ai_generator import build_client


def test_settings_retry_defaults():
    s = Settings()
    assert s.ai_max_retries == 3
    assert s.ai_timeout_seconds == 600


def test_settings_retry_env_override(monkeypatch):
    monkeypatch.setenv("AI_MAX_RETRIES", "5")
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "300")
    s = Settings()
    assert s.ai_max_retries == 5
    assert s.ai_timeout_seconds == 300


def test_build_client_passes_retry_and_timeout():
    """构造 OpenAI 客户端不发网络请求，可直接断言透传契约。"""
    s = Settings(ai_api_key="sk-test", ai_base_url="https://example.com/v1",
                 ai_max_retries=3, ai_timeout_seconds=600)
    client = build_client(s)
    assert client.api_key == "sk-test"
    # SDK 会把 base_url 规整为 URL 对象并补尾部斜杠
    assert str(client.base_url).rstrip("/") == "https://example.com/v1"
    assert client.max_retries == 3
    assert client.timeout == 600


def test_build_client_custom_values():
    s = Settings(ai_api_key="sk-x", ai_base_url="https://a.b/v1",
                 ai_max_retries=7, ai_timeout_seconds=120)
    client = build_client(s)
    assert client.max_retries == 7
    assert client.timeout == 120
