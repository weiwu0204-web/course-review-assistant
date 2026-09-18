"""/api/settings/* 设置路由的单元测试。

不调真实 API、不写真实 %APPDATA%：
- 用环境变量 PPT_REVIEWER_CONFIG_DIR 把配置目录指到 tmp_path（user_config 支持）
- chdir 到 tmp_path，使 Settings() 读不到项目根目录的 .env（本机 .env 里有真实
  key，直接跑会污染断言），同时清掉 AI_* 环境变量

验证路径：
- 未配置 → status.configured=False（前端据此决定是否弹强制遮罩）
- providers：4 家服务商 + 默认 deepseek
- save → status：configured=True、source=user、key 尾部掩码
- save 后 get_settings() 立即用新值（reload_settings 热更新，运行中服务不停机）
- 用户配置优先级高于环境变量 / .env
- clear → 回到未配置
- verify：空 key / 未知服务商 的本地分支（真实网络探测不属于单测范围）
"""
from fastapi.testclient import TestClient

from app.api.settings_routes import _tail
from app.config import AI_PROVIDERS, get_settings, reload_settings
from app.main import app
from app.services.user_config import config_path

client = TestClient(app)


def _isolate(monkeypatch, tmp_path):
    """把「配置目录 + .env + 环境变量」全部隔离到 tmp_path 的空环境。"""
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reload_settings()  # 清 get_settings 缓存，避免上一用例残留


def test_status_unconfigured(monkeypatch, tmp_path):
    """空配置目录 + 无 .env → configured=False，前端应弹首次配置遮罩。"""
    _isolate(monkeypatch, tmp_path)
    data = client.get("/api/settings/status").json()
    assert data["configured"] is False


def test_providers_list(monkeypatch, tmp_path):
    """服务商下拉数据源：4 家、顺序与 AI_PROVIDERS 一致、默认 deepseek。"""
    _isolate(monkeypatch, tmp_path)
    data = client.get("/api/settings/providers").json()
    ids = [p["id"] for p in data["providers"]]
    assert ids == list(AI_PROVIDERS.keys())
    assert data["default"] == "deepseek"
    deepseek = data["providers"][0]
    assert deepseek["name"] and deepseek["model"] and deepseek["vision_model"]
    assert deepseek["vision_supported"] is True
    # 前端 cfgRenderNote() 依赖这些字段，缺了会显示 undefined
    for p in data["providers"]:
        assert {"id", "name", "model", "vision_model", "vision_supported", "note",
                "price_note", "topup_url"} <= set(p)
        assert p["topup_url"].startswith("https://")
        assert p["price_note"]


def test_estimate_defaults_to_deepseek(monkeypatch, tmp_path):
    """未配置时估算按默认服务商 DeepSeek 走，返回可展示的费用文案。"""
    _isolate(monkeypatch, tmp_path)
    data = client.post("/api/settings/estimate", json={
        "chars": 15000, "image_count": 30, "use_vision": True, "style": "详细",
    }).json()
    assert data["provider_name"] == AI_PROVIDERS["deepseek"]["name"]
    assert data["billed_model"] == "vision"
    assert data["tokens_in"] > 0 and data["tokens_out"] > 0
    assert data["cost_yuan"] > 0
    assert data["cost_text"].startswith("约 ¥")
    assert data["caveat"]


def test_estimate_follows_saved_provider(monkeypatch, tmp_path):
    """保存为 Kimi 后，估算自动按 Kimi 的单价走（前端每次生成前实时查）。

    moonshot 未声明 vision_price_* → 视觉计费时回退文本价（原生多模态同价）。
    """
    _isolate(monkeypatch, tmp_path)
    client.post("/api/settings/save", json={"provider_id": "moonshot", "api_key": "sk-k2.6-x"})
    data = client.post("/api/settings/estimate", json={
        "chars": 15000, "image_count": 30, "use_vision": True, "style": "详细",
    }).json()
    assert data["provider_name"] == AI_PROVIDERS["moonshot"]["name"]
    assert data["billed_model"] == "vision"
    assert data["price_in"] == AI_PROVIDERS["moonshot"]["price_in"]  # 无 vision 价 → 回退文本价
    assert data["price_out"] == AI_PROVIDERS["moonshot"]["price_out"]


def test_estimate_use_vision_toggles_zhipu_pricing(monkeypatch, tmp_path):
    """智谱文本(2/8)与视觉 glm-4.6v(2/6) 输出单价不同：开关读图应切换计费档。"""
    _isolate(monkeypatch, tmp_path)
    client.post("/api/settings/save", json={"provider_id": "zhipu", "api_key": "sk-glm-x"})

    off = client.post("/api/settings/estimate", json={
        "chars": 1000, "image_count": 10, "use_vision": False, "style": "精简",
    }).json()
    assert off["billed_model"] == "text"
    assert off["price_out"] == AI_PROVIDERS["zhipu"]["price_out"]  # 8

    on = client.post("/api/settings/estimate", json={
        "chars": 1000, "image_count": 10, "use_vision": True, "style": "精简",
    }).json()
    assert on["billed_model"] == "vision"
    assert on["price_out"] == AI_PROVIDERS["zhipu"]["vision_price_out"]  # 6
    assert on["tokens_in"] > off["tokens_in"]  # 多出图片 token


def test_save_then_status(monkeypatch, tmp_path):
    """保存后 status 应显示已配置、来源 user、key 只回显尾部掩码。"""
    _isolate(monkeypatch, tmp_path)
    key = "sk-test-abcdef123456"
    r = client.post("/api/settings/save", json={"provider_id": "moonshot", "api_key": key})
    assert r.status_code == 200 and r.json()["ok"] is True
    st = client.get("/api/settings/status").json()
    assert st["configured"] is True
    assert st["provider_id"] == "moonshot"
    assert st["provider_name"] == AI_PROVIDERS["moonshot"]["name"]
    assert st["source"] == "user"
    assert st["api_key_tail"] == _tail(key)
    assert "sk-test-abcdef123456" not in st["api_key_tail"]  # 不回显整串


def test_save_hot_reload(monkeypatch, tmp_path):
    """运行中保存 key → reload_settings 后 get_settings() 立即返回新值。"""
    _isolate(monkeypatch, tmp_path)
    reload_settings()
    get_settings()  # 先让 lru_cache 缓存一份"未配置"的 Settings
    key = "sk-hot-reload-99"
    client.post("/api/settings/save", json={"provider_id": "qwen", "api_key": key})
    s = get_settings()  # save 端点内部已 reload_settings()
    assert s.ai_api_key == key
    assert s.ai_model == AI_PROVIDERS["qwen"]["model"]
    assert s.ai_base_url == AI_PROVIDERS["qwen"]["base_url"]


def test_user_config_overrides_env(monkeypatch, tmp_path):
    """有用户配置时，其 key/base_url/模型应覆盖环境变量。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_API_KEY", "sk-env-old")
    reload_settings()
    assert get_settings().ai_api_key == "sk-env-old"  # 无用户配置时环境变量生效
    client.post("/api/settings/save", json={"provider_id": "zhipu", "api_key": "sk-user-new"})
    s = get_settings()
    assert s.ai_api_key == "sk-user-new"
    assert s.ai_base_url == AI_PROVIDERS["zhipu"]["base_url"]
    assert s.ai_model == AI_PROVIDERS["zhipu"]["model"]


def test_clear_returns_unconfigured(monkeypatch, tmp_path):
    """清除后回到未配置，配置文件本身被删除。"""
    _isolate(monkeypatch, tmp_path)
    client.post("/api/settings/save", json={"provider_id": "deepseek", "api_key": "sk-x-1"})
    assert client.get("/api/settings/status").json()["configured"] is True
    r = client.post("/api/settings/clear")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not config_path().exists()
    assert client.get("/api/settings/status").json()["configured"] is False


def test_verify_empty_key(monkeypatch, tmp_path):
    """空 key 走本地校验分支，不发请求。"""
    _isolate(monkeypatch, tmp_path)
    data = client.post("/api/settings/verify", json={"provider_id": "deepseek", "api_key": "   "}).json()
    assert data["ok"] is False and data["kind"] == "empty"


def test_verify_unknown_provider(monkeypatch, tmp_path):
    """未知服务商 → 400。"""
    _isolate(monkeypatch, tmp_path)
    r = client.post("/api/settings/verify", json={"provider_id": "nope", "api_key": "sk-x"})
    assert r.status_code == 400


def test_save_unknown_provider(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert client.post("/api/settings/save", json={"provider_id": "nope", "api_key": "sk-x"}).status_code == 400


def test_tail_masks_short_and_long():
    """掩码函数：超 4 位显示 …尾4；太短只显示 …。"""
    assert _tail("sk-1234567890") == "…7890"
    assert _tail("abc") == "…"
