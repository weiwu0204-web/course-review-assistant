"""max_tokens 降级重试：服务商嫌输出上限太大时自动退回保守值，而不是整个失败。

背景（2026-09-10 实测）：一份 18 页课件的复习文档写「易错点清单」写到第 15 条
就被 16384 的输出上限切断（finish_reason=length）。故默认值抬到 32768。
但各家输出上限不一且会随版本变化，与其硬编码一份会过期的上限表，不如让系统
自愈：400 且错误文本指向 max_tokens 过大 → 退回保守值重试一次。
"""
from types import SimpleNamespace

import pytest

from app.services.ai_generator import MAX_TOKENS_FALLBACK, _create_completion


class _ApiError(Exception):
    """模拟 openai SDK 的 BadRequestError：带 status_code 的异常。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _FakeClient:
    def __init__(self, behaviour):
        self.calls: list[dict] = []
        outer = self

        def create(**kwargs):
            outer.calls.append(kwargs)
            return behaviour(kwargs)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def test_fallback_when_provider_rejects_large_max_tokens():
    """400 + 提到 max_tokens 过大 → 降级到保守值重试，并返回重试结果。"""

    def behaviour(kwargs):
        if kwargs["max_tokens"] > MAX_TOKENS_FALLBACK:
            raise _ApiError("Invalid request: max_tokens is too large, maximum is 16384")
        return {"ok": kwargs["max_tokens"]}

    client = _FakeClient(behaviour)
    out = _create_completion(client, {"model": "m", "max_tokens": 32768})

    assert out == {"ok": MAX_TOKENS_FALLBACK}
    assert [c["max_tokens"] for c in client.calls] == [32768, MAX_TOKENS_FALLBACK]


def test_unrelated_400_is_not_retried():
    """400 但错误与 max_tokens 无关（如模型不存在）→ 直接抛出，不重试。"""

    def behaviour(kwargs):
        raise _ApiError("Invalid request: model 'foo' does not exist")

    client = _FakeClient(behaviour)
    with pytest.raises(_ApiError):
        _create_completion(client, {"model": "foo", "max_tokens": 32768})
    assert len(client.calls) == 1


def test_non_400_status_is_not_retried():
    """非 400（如 402 余额不足 / 429 限流）不该走降级——重试也没用且会掩盖真因。"""

    def behaviour(kwargs):
        raise _ApiError("Insufficient balance", status_code=402)

    client = _FakeClient(behaviour)
    with pytest.raises(_ApiError):
        _create_completion(client, {"model": "m", "max_tokens": 32768})
    assert len(client.calls) == 1


def test_no_retry_when_already_at_conservative_value():
    """已经是保守值还被拒 → 说明不是「太大」，直接抛出。"""

    def behaviour(kwargs):
        raise _ApiError("max_tokens too large")

    client = _FakeClient(behaviour)
    with pytest.raises(_ApiError):
        _create_completion(client, {"model": "m", "max_tokens": MAX_TOKENS_FALLBACK})
    assert len(client.calls) == 1


def test_success_path_single_call():
    """正常情况只发一次请求，不引入额外开销。"""
    client = _FakeClient(lambda k: {"ok": k["max_tokens"]})
    assert _create_completion(client, {"model": "m", "max_tokens": 32768}) == {"ok": 32768}
    assert len(client.calls) == 1
