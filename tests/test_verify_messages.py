"""verify 错误判定修正的单测：429/余额类错误按免费档/付费档分叉 + 透出服务商原文。

背景（2026-09-04 实测）：智谱免费档 glm-4.7-flash 的真实报错是
HTTP 429 code 1305「该模型当前访问量过大」（共享免费池超载），
旧文案却归因为「多为余额不足」，免费档用户看到「余额不足/充值」自相矛盾。
"""
from app.api.settings_routes import (
    _balance_message,
    _is_free_tier,
    _rate_limit_message,
    _raw_hint,
)
from app.config import AI_PROVIDERS, effective_provider

# 智谱默认档=付费；免费档须经 effective_provider 解析（顶层仍是付费档）
ZHIPU = AI_PROVIDERS["zhipu"]
ZHIPU_FREE, _ = effective_provider("zhipu", "free")  # glm-4.7-flash / glm-4.6v-flash，单价 0
DEEPSEEK = AI_PROVIDERS["deepseek"]  # 付费档


class _Zhipu429:
    """智谱免费档真实报错形态。"""

    status_code = 429
    body = {"code": 1305, "message": "该模型当前访问量过大，请您稍后再试"}


class _GenericBalance402:
    """OpenAI 风格余额错误体。"""

    status_code = 402
    body = {"error": {"message": "Insufficient Balance"}}


class _ZhipuBalance402:
    """智谱风格带 code 的余额类错误（免费档理论场景）。"""

    status_code = 402
    body = {"code": 1123, "message": "账户余额不足"}


def test_free_tier_detection():
    assert _is_free_tier(ZHIPU_FREE) is True
    assert _is_free_tier(ZHIPU) is False  # 智谱默认付费档（glm-4.7 价 2/8）
    assert _is_free_tier(DEEPSEEK) is False


def test_zhipu_429_free_tier_no_balance_words():
    """免费档 429 = 共享资源池超载，绝不能出现「余额不足/充值」引导。"""
    msg = _rate_limit_message(ZHIPU_FREE, _Zhipu429())
    assert "余额不足" not in msg
    assert "充值入口" not in msg
    assert "免费" in msg and "超载" in msg  # 归因到免费资源池超载
    assert "code 1305" in msg and "访问量过大" in msg  # 透出服务商原文


def test_paid_429_no_longer_blames_balance():
    """付费档 429 也不再武断归因为「余额不足」（可能只是并发/限流）。"""
    msg = _rate_limit_message(DEEPSEEK, _Zhipu429())
    assert "余额不足" not in msg
    assert "访问量过大" in msg  # 原文照实透出


def test_paid_balance_still_guides_topup():
    """付费档真正的 402 余额错误：保留充值入口指引。"""
    msg = _balance_message(DEEPSEEK, _GenericBalance402())
    assert "余额不足" in msg
    assert DEEPSEEK["topup_url"] in msg
    assert "Insufficient Balance" in msg  # 原文透出


def test_free_tier_balance_error_never_guides_topup():
    """免费档即便平台返回「账户余额不足」，也不引导充值，改指实名/开通/额度。"""
    msg = _balance_message(ZHIPU_FREE, _ZhipuBalance402())
    assert "充值入口" not in msg and "去充值" not in msg
    assert "不需要充值" in msg
    assert "实名认证" in msg and "控制台" in msg
    assert "code 1123" in msg


def test_raw_hint_truncation_and_empty():
    assert "code 1305" in _raw_hint(_Zhipu429())

    class NoBody(Exception):
        pass

    assert _raw_hint(NoBody()) == ""  # 无 body 时不产生空壳提示
