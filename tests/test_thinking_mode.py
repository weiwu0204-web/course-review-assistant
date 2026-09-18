"""思考模式开关的测试：配置契约、成本估算联动、请求体 extra_body 透传。

背景（2026-09-04 官方文档逐家核实）：
- DeepSeek / 智谱 glm-4.7、4.6v / Kimi k2.6 / 通义 qwen3.7-plus 思考模式默认全开；
- 关闭参数各家不同：deepseek/zhipu/moonshot 用 {"thinking": {"type": "disabled"}}，
  通义用 {"enable_thinking": False}；OpenAI SDK 的非标准参数须走 extra_body；
- 复习文档是长文档结构化任务，产品默认关闭思考（省 30-50% 费用、更快），
  前端勾选「思考模式」才开启。

不调真实 API：AI_PROVIDERS.thinking_off 只是配置数据；请求链路用 fake client
monkeypatch 掉 build_client，断言 create(**kwargs) 收到的 extra_body 与开关一致。
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import AI_PROVIDERS, reload_settings
from app.main import app
from app.models import ExtractionResult, GenerateRequest, SlideContent
from app.services import ai_generator
from app.services.user_config import save_user_settings

client = TestClient(app)


def _isolate(monkeypatch, tmp_path):
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reload_settings()


def _activate(monkeypatch, tmp_path, provider_id: str, tier: str = ""):
    """隔离环境 + 以某服务商(档位)保存 key + 热重载，使 get_settings 反映该配置。"""
    _isolate(monkeypatch, tmp_path)
    save_user_settings(provider_id, "sk-test-key", tier)
    reload_settings()


def _minimal_result() -> ExtractionResult:
    return ExtractionResult(
        filename="x/课件.pptx",
        total_pages=1,
        slides=[SlideContent(page=1, text="第一章 计算机网络概述")],
    )


# ---------- 配置契约 ----------

def test_all_providers_have_thinking_off():
    """每家服务商都配了关闭思考的参数（前端默认关思考，缺了会静默烧推理 token）。"""
    for pid, cfg in AI_PROVIDERS.items():
        assert "thinking_off" in cfg and cfg["thinking_off"], pid


def test_zhipu_tiers_default_paid_free_overrides():
    """智谱默认付费档（glm-4.7 / glm-4.6v）；free 档覆盖为 flash 免费模型、价 0。

    （v0.6.4 后档位化：顶层=默认付费档，免费档走 effective_provider('zhipu','free')。）
    """
    from app.config import effective_provider

    paid = AI_PROVIDERS["zhipu"]
    assert paid["model"] == "glm-4.7"
    assert paid["vision_model"] == "glm-4.6v"
    assert paid["price_in"] == 2.0
    assert paid.get("default_tier") == "paid"

    eff, tid = effective_provider("zhipu", "free")
    assert tid == "free"
    assert eff["model"] == "glm-4.7-flash"
    assert eff["vision_model"] == "glm-4.6v-flash"
    assert eff["price_in"] == 0.0 and eff["vision_price_in"] == 0.0


def test_provider_off_syntax_matches_docs():
    """关闭参数与 2026-09-04 核实的官方文档一致（deepseek/zhipu/kimi 同构，qwen 不同）。"""
    assert AI_PROVIDERS["deepseek"]["thinking_off"] == {"thinking": {"type": "disabled"}}
    assert AI_PROVIDERS["zhipu"]["thinking_off"] == {"thinking": {"type": "disabled"}}
    assert AI_PROVIDERS["moonshot"]["thinking_off"] == {"thinking": {"type": "disabled"}}
    assert AI_PROVIDERS["qwen"]["thinking_off"] == {"enable_thinking": False}


# ---------- 成本估算联动 ----------

def test_estimate_thinking_false_costs_less(monkeypatch, tmp_path):
    """关思考时输出 token 不再乘 1.5 思考系数，估算费用明显更低。"""
    _isolate(monkeypatch, tmp_path)
    body = {"chars": 15000, "image_count": 0, "use_vision": False, "style": "详细"}
    on = client.post("/api/settings/estimate", json={**body, "thinking": True}).json()
    off = client.post("/api/settings/estimate", json={**body, "thinking": False}).json()
    assert on["cost_yuan"] > off["cost_yuan"] > 0
    # 系数 1.5：开思考的输出 token ≈ 关思考的 1.5 倍（整数取整允许 ±1）
    assert abs(on["tokens_out"] / off["tokens_out"] - 1.5) <= 0.01


def test_estimate_zhipu_free_is_zero(monkeypatch, tmp_path):
    """智谱免费档：怎么估都是 0 元，前端显示「约 0 元」而不是小数价。"""
    _activate(monkeypatch, tmp_path, "zhipu", "free")
    data = client.post("/api/settings/estimate", json={
        "chars": 15000, "image_count": 30, "use_vision": True, "style": "详细",
        "thinking": False,
    }).json()
    assert data["cost_yuan"] == 0.0
    assert data["cost_text"] == "约 0 元"


# ---------- 请求体 extra_body 链路 ----------

class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="生成的文档"),
                finish_reason="stop",
            )]
        )


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def _run_generate(monkeypatch, tmp_path, provider_id: str, thinking: bool):
    _activate(monkeypatch, tmp_path, provider_id)
    fake = _FakeClient()
    monkeypatch.setattr(ai_generator, "build_client", lambda settings: fake)
    req = GenerateRequest(filename="x/课件.pptx", use_vision=False, thinking=thinking)
    markdown, _, _ = ai_generator.generate_review_document(_minimal_result(), req)
    assert markdown == "生成的文档"
    return fake.chat.completions.calls[0]


def test_request_off_deepseek_extra_body(monkeypatch, tmp_path):
    """DeepSeek + 关思考 → extra_body 带 thinking.type=disabled。"""
    kwargs = _run_generate(monkeypatch, tmp_path, "deepseek", thinking=False)
    assert kwargs.get("extra_body") == {"thinking": {"type": "disabled"}}


def test_request_off_qwen_extra_body(monkeypatch, tmp_path):
    """通义 + 关思考 → extra_body 是 enable_thinking=False（参数名与别家不同）。"""
    kwargs = _run_generate(monkeypatch, tmp_path, "qwen", thinking=False)
    assert kwargs.get("extra_body") == {"enable_thinking": False}


def test_request_on_no_extra_body(monkeypatch, tmp_path):
    """开启思考 = 不传任何 thinking 参数（各家思考默认开启，无需显式启用）。"""
    kwargs = _run_generate(monkeypatch, tmp_path, "moonshot", thinking=True)
    assert "extra_body" not in kwargs
