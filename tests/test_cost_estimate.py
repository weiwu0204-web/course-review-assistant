"""成本预估模块与生成错误翻译的单元测试。"""

from app.api.routes import _friendly_ai_error
from app.config import AI_PROVIDERS, reload_settings
from app.services.cost_estimate import (
    CHARS_PER_TOKEN,
    TOKENS_PER_IMAGE,
    estimate_generation_cost,
    format_cost,
)

DEEPSEEK = AI_PROVIDERS["deepseek"]


def _isolate(monkeypatch, tmp_path):
    """friendly 错误翻译依赖 current_provider()——必须隔离真实用户配置，
    否则本机 %APPDATA% 里残留的配置（开发/冒烟时 save 过）会让断言指向别家。"""
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reload_settings()


def test_tokens_scaling():
    """输入 token = 汉字/1.5 + 固定开销；带图时每张按该服务商的实测口径加。"""
    chars = int(CHARS_PER_TOKEN * 1000)  # 1000 token 的字数
    est = estimate_generation_cost(DEEPSEEK, chars=chars, image_count=0, use_vision=False)
    assert est["tokens_in"] >= 1000 + 1500 * 0.9  # 字面 token + 固定开销（允许浮点差）
    assert est["tokens_out"] > 0

    with_img = estimate_generation_cost(DEEPSEEK, chars=0, image_count=5, use_vision=True)
    assert with_img["tokens_in"] == 5 * DEEPSEEK["vision_tokens_per_image"] + 1500
    assert with_img["billed_model"] == "vision"


def test_image_token_falls_back_to_conservative_default():
    """没有实测口径的服务商沿用保守的 384，不跟着 DeepSeek 的 110 走——
    宁可高估，也别让换一家之后估算突然变得不靠谱。"""
    bare = {"name": "X", "price_in": 1.0, "price_out": 2.0}
    est = estimate_generation_cost(bare, chars=0, image_count=5, use_vision=True)
    assert est["tokens_in"] == 5 * TOKENS_PER_IMAGE + 1500


def test_output_driven_by_pages_not_chars():
    """输出量由**页数**决定，与输入字数基本无关。

    这是 2026-09-11 用 14 份真实课件（Ceph 课程）标定后推翻的旧假设：
    旧公式按输入字数推输出，把一份实际 15,374 token 的文档估成 1,387（低估 11 倍）。
    """
    from app.services.cost_estimate import OUT_PER_PAGE

    common = dict(image_count=0, use_vision=False, style="详细", thinking=False)
    few = estimate_generation_cost(DEEPSEEK, chars=100, pages=20, **common)
    many = estimate_generation_cost(DEEPSEEK, chars=99_999, pages=20, **common)
    assert few["tokens_out"] == many["tokens_out"] == 20 * OUT_PER_PAGE["详细"]

    double = estimate_generation_cost(DEEPSEEK, chars=100, pages=40, **common)
    assert double["tokens_out"] == 2 * few["tokens_out"]


def test_calibrated_rate_is_not_below_measured():
    """标定口径必须 ≥ 实测均值（562 token/页），符合模块「宁高勿低」的约定。"""
    est = estimate_generation_cost(
        DEEPSEEK, chars=0, image_count=0, use_vision=False,
        style="详细", thinking=False, pages=23,
    )
    assert est["tokens_out"] >= 23 * 562


def test_missing_pages_falls_back_to_char_estimate():
    """漏传 pages 时退回旧的按字数估算：明确更小（已知偏低），仅防调用方漏传。"""
    common = dict(chars=30000, image_count=0, use_vision=False, style="详细", thinking=False)
    without = estimate_generation_cost(DEEPSEEK, **common)
    with_pages = estimate_generation_cost(DEEPSEEK, pages=23, **common)
    assert without["tokens_out"] < with_pages["tokens_out"]


def test_m3_aggregate_is_no_longer_underestimated():
    """回归锚点：M3 实跑的真实课程（14 份 / 325 页 / 273 图 / 详细 / 关思考）。

    实测 ¥1.9517；旧公式估 ¥0.77（低估 2.5 倍）。新公式应落在实测之上、
    且不夸张（≤ 实测 ×1.5）。
    """
    est = estimate_generation_cost(
        DEEPSEEK, chars=77_829, image_count=273, use_vision=True,
        style="详细", thinking=False, pages=325,
    )
    assert 1.95 <= est["cost_yuan"] <= 1.95 * 1.5


def test_output_ratio_by_style():
    """详细风格输出最长，精简最短（页数口径下同样成立）。"""
    args = dict(chars=8000, image_count=0, use_vision=False, thinking=False, pages=20)
    detailed = estimate_generation_cost(DEEPSEEK, style="详细", **args)
    concise = estimate_generation_cost(DEEPSEEK, style="精简", **args)
    assert detailed["tokens_out"] > concise["tokens_out"]
    assert detailed["cost_yuan"] > concise["cost_yuan"]


def test_exam_chars_inflate_cost():
    """带往年试题时输入变长、输出变长，费用更高。"""
    base = dict(chars=8000, image_count=0, use_vision=False, style="精简")
    plain = estimate_generation_cost(DEEPSEEK, **base)
    with_exam = estimate_generation_cost(DEEPSEEK, exam_chars=4000, **base)
    assert with_exam["tokens_in"] > plain["tokens_in"]
    assert with_exam["cost_yuan"] > plain["cost_yuan"]


def test_format_cost_boundaries():
    assert format_cost(0.0) == "约 0 元"
    assert format_cost(0.0001) == "不到 1 分钱"
    assert format_cost(0.137) == "约 ¥0.14"
    assert format_cost(2.0) == "约 ¥2.00"


def test_friendly_balance_error_has_topup_url(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    msg = _friendly_ai_error(RuntimeError("Error code: 402 - Insufficient Balance"))
    assert "余额不足" in msg
    assert "充值入口" in msg
    assert DEEPSEEK["topup_url"] in msg
    assert "换一家" in msg


def test_friendly_rate_limit_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    msg = _friendly_ai_error(RuntimeError("429 rate limit exceeded"))
    assert "限流" in msg and "换一家" in msg


def test_friendly_auth_error_guides_to_settings(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    msg = _friendly_ai_error(RuntimeError("AuthenticationError: invalid api key"))
    assert "Key 无效" in msg and "设置" in msg


def test_friendly_unknown_error_falls_back_to_raw(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    msg = _friendly_ai_error(RuntimeError("some weird failure"))
    assert "some weird failure" in msg
