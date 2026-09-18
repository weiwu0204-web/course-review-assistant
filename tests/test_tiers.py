"""档位（tier）支持测试：智谱付费/免费档解析 + 端点契约。

v0.6.5 需求：智谱默认付费档（glm-4.7 / glm-4.6v）；设置面板可选免费档
（glm-4.7-flash / glm-4.6v-flash，0 元）——免费共享池高峰超载 429 是 0.6.4 的实测教训。
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import AI_PROVIDERS, effective_provider, reload_settings
from app.main import app
from app.services.user_config import save_user_settings

client = TestClient(app)


def _isolate(monkeypatch, tmp_path):
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reload_settings()


def _save(monkeypatch, tmp_path, provider_id: str, tier: str = ""):
    _isolate(monkeypatch, tmp_path)
    save_user_settings(provider_id, "sk-test", tier)
    reload_settings()


# ---------- effective_provider 纯函数 ----------


def test_effective_single_tier_provider_untouched():
    """无 tiers 的服务商（moonshot 等）原样返回、tier 置空，非法 tier 也忽略。"""
    cfg, tid = effective_provider("moonshot", "free")
    assert tid == ""
    assert cfg["model"] == AI_PROVIDERS["moonshot"]["model"]


def test_effective_zhipu_default_paid_and_free_override():
    cfg, tid = effective_provider("zhipu", "")
    assert tid == "paid"
    assert cfg["model"] == "glm-4.7" and cfg["price_in"] == 2.0

    cfg, tid = effective_provider("zhipu", "free")
    assert tid == "free"
    assert cfg["model"] == "glm-4.7-flash" and cfg["vision_model"] == "glm-4.6v-flash"
    assert cfg["price_in"] == 0.0 and cfg["vision_price_in"] == 0.0

    cfg, tid = effective_provider("zhipu", "nonsense")  # 非法档回退默认付费
    assert tid == "paid" and cfg["model"] == "glm-4.7"


def test_effective_unknown_provider_returns_none():
    cfg, tid = effective_provider("no-such-provider", "")
    assert cfg is None and tid == ""


# ---------- 端点契约 ----------


def test_providers_carry_tiers(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    data = client.get("/api/settings/providers").json()
    zp = next(p for p in data["providers"] if p["id"] == "zhipu")
    paid, free = zp["tiers"]
    assert paid["id"] == "paid" and paid["label"] == "付费档（稳定）"
    assert paid["free"] is False
    assert paid["model"] == "glm-4.7"  # 档位元数据带出该档模型（前端切档展示用）
    assert free["id"] == "free" and free["label"] == "免费档（0 元）"
    assert free["free"] is True
    assert free["model"] == "glm-4.7-flash" and free["vision_model"] == "glm-4.6v-flash"
    assert zp["default_tier"] == "paid"
    ds = next(p for p in data["providers"] if p["id"] == "deepseek")
    assert ds["tiers"] == [] and ds["default_tier"] == ""


def test_status_reports_saved_tier(monkeypatch, tmp_path):
    _save(monkeypatch, tmp_path, "zhipu", "free")
    st = client.get("/api/settings/status").json()
    assert st["configured"] is True
    assert st["provider_id"] == "zhipu"
    assert st["tier"] == "free"
    assert st["tier_label"] == "免费档（0 元）"


def test_save_without_tier_defaults_to_paid(monkeypatch, tmp_path):
    """不带 tier 保存（含旧版配置）→ 默认付费档，档位化后智谱不再默认免费。"""
    _save(monkeypatch, tmp_path, "zhipu")
    st = client.get("/api/settings/status").json()
    assert st["tier"] == "paid"


def test_estimate_uses_saved_tier(monkeypatch, tmp_path):
    body = {
        "chars": 15000, "image_count": 0, "use_vision": False,
        "style": "详细", "thinking": False,
    }
    _save(monkeypatch, tmp_path, "zhipu", "free")
    free = client.post("/api/settings/estimate", json=body).json()
    assert free["cost_yuan"] == 0.0

    _save(monkeypatch, tmp_path, "zhipu")  # 默认付费档
    paid = client.post("/api/settings/estimate", json=body).json()
    assert paid["cost_yuan"] > 0.0
    assert paid["price_in"] == 2.0


def test_verify_probes_free_tier_model(monkeypatch, tmp_path):
    """verify 带 tier=free → 探测请求的 model 是免费档 glm-4.7-flash。"""
    _isolate(monkeypatch, tmp_path)
    calls: dict = {}

    class _FakeCompletions:
        def create(self, **kw):
            calls["kwargs"] = kw
            raise RuntimeError("probe captured")

    class _FakeClient:
        def __init__(self, *a, **kw):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", _FakeClient)
    client.post(
        "/api/settings/verify",
        json={"provider_id": "zhipu", "api_key": "sk-x", "tier": "free"},
    )
    assert calls["kwargs"]["model"] == "glm-4.7-flash"
