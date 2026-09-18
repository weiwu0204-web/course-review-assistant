"""设置路由：/api/settings/* —— 启动状态检查、服务商列表、Key 验证与保存。

为什么单独一组路由：API Key 现在是「首次使用必须配置」的东西，
主流程（上传/生成）不应该再承担「教用户配 key」的职责。
"""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import (
    AI_PROVIDERS,
    Settings,
    current_provider,
    effective_provider,
    get_settings,
    reload_settings,
)
from app.services.cost_estimate import estimate_generation_cost, format_cost
from app.services.user_config import (
    clear_user_settings,
    load_user_settings,
    save_user_settings,
)

router = APIRouter(prefix="/settings", tags=["settings"])


class ProviderInfo(BaseModel):
    id: str
    name: str
    model: str
    vision_model: str = ""
    vision_supported: bool = False
    note: str = ""
    price_note: str = ""
    topup_url: str = ""
    # 档位（仅有多档服务商如智谱付费/免费档才有；单档家为空列表）
    tiers: list[dict] = []
    default_tier: str = ""


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]
    default: str


class StatusResponse(BaseModel):
    configured: bool = False
    provider_id: str = ""
    provider_name: str = ""
    source: Literal["user", "env"] = "user"
    api_key_tail: str = ""
    # 当前生效档位（智谱 paid/free；单档服务商为空字符串）
    tier: str = ""
    tier_label: str = ""


class SaveRequest(BaseModel):
    provider_id: str
    api_key: str
    tier: str = ""


class VerifyRequest(BaseModel):
    provider_id: str
    api_key: str
    tier: str = ""


class VerifyResponse(BaseModel):
    ok: bool = False
    kind: str = "error"
    message: str = ""


class SaveResponse(BaseModel):
    ok: bool = True
    message: str = ""


class EstimateRequest(BaseModel):
    chars: int = 0
    image_count: int = 0
    use_vision: bool = True
    exam_chars: int = 0
    style: str = "详细"
    # 课件页数：输出 token 的主要自变量（实测约 562 token/页）。
    # 漏传会退回按字数的老估算，已知低估约 11 倍——前端务必带上 total_pages。
    pages: int = 0
    # 本次是否开启思考模式（四家服务商思考默认开启，会多烧推理 token）。
    # 前端把思考开关状态传进来，估算才与实际费用一致。
    thinking: bool = True


class EstimateResponse(BaseModel):
    provider_name: str = ""
    billed_model: str = "text"
    price_in: float = 0.0
    price_out: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_yuan: float = 0.0
    cost_text: str = ""
    caveat: str = ""


_TOPUP_CAVEAT = "按当前服务商官方单价粗估（思考模式/峰谷价会让实际账单略高或略低），以平台账单为准。"


@router.get("/status", response_model=StatusResponse)
def settings_status() -> StatusResponse:
    """当前是否已配置 key，来自用户配置还是 .env（开发态）。"""
    user = load_user_settings()
    provider, tier_id = effective_provider(
        user.get("provider_id", ""), user.get("tier", "")
    )
    if provider and user.get("api_key", "").strip():
        return StatusResponse(
            configured=True,
            provider_id=user["provider_id"],
            provider_name=provider["name"],
            source="user",
            api_key_tail=_tail(user["api_key"]),
            tier=tier_id,
            tier_label=provider.get("tier_label", ""),
        )
    env_key = Settings().ai_api_key
    if env_key:
        return StatusResponse(
            configured=True,
            provider_name=".env",
            source="env",
            api_key_tail=_tail(env_key),
        )
    return StatusResponse(configured=False)


@router.get("/providers", response_model=ProvidersResponse)
def list_providers() -> ProvidersResponse:
    """设置页下拉可用的服务商（model/base_url 以后端为准）。

    有 tiers 的服务商（智谱）带出档位列表，前端渲染「付费/免费」切换控件；
    model/note 等展示字段取默认档（付费档），切档后由前端重新读 tier 信息。
    """
    items = []
    for pid, cfg in AI_PROVIDERS.items():
        tiers_raw = cfg.get("tiers") or {}
        items.append(
            ProviderInfo(
                id=pid,
                name=cfg["name"],
                model=cfg["model"],
                vision_model=cfg.get("vision_model", ""),
                vision_supported=bool(cfg.get("vision_model")),
                note=cfg.get("note", ""),
                price_note=cfg.get("price_note", ""),
                topup_url=cfg.get("topup_url", ""),
                tiers=[
                    _tier_meta(cfg, tid, t)
                    for tid, t in tiers_raw.items()
                ],
                default_tier=cfg.get("default_tier", ""),
            )
        )
    return ProvidersResponse(providers=items, default="deepseek")


def _tier_meta(cfg: dict, tid: str, t: dict) -> dict:
    """档位的展示元数据：把档位覆盖层合并到顶层后取模型/说明（paid 层可为空）。"""
    merged = dict(cfg)
    merged.update(t)
    return {
        "id": tid,
        "label": t.get("tier_label") or tid,
        "free": _is_free_tier(merged),  # 免费档：前端切换时弹提醒，避免又遇「超载/未实名」
        "model": merged["model"],
        "vision_model": merged.get("vision_model", ""),
        "note": merged.get("note", ""),
        "price_note": merged.get("price_note", ""),
    }


@router.post("/estimate", response_model=EstimateResponse)
def estimate_cost(req: EstimateRequest) -> EstimateResponse:
    """生成前粗估：按当前生效服务商与单价，估算本次生成会消耗多少 token / 多少钱。

    前端在「解析完课件、尚未真正调用模型」时调用，把费用数字展示给用户——
    余额不足的第一道防线是让用户先知道「这一次大概花多少」。
    """
    provider = current_provider()
    est = estimate_generation_cost(
        provider,
        chars=req.chars,
        image_count=req.image_count,
        use_vision=req.use_vision,
        exam_chars=req.exam_chars,
        style=req.style,
        thinking=req.thinking,
        pages=req.pages,
    )
    return EstimateResponse(
        provider_name=est["provider_name"],
        billed_model=est["billed_model"],
        price_in=est["price_in"],
        price_out=est["price_out"],
        tokens_in=est["tokens_in"],
        tokens_out=est["tokens_out"],
        cost_yuan=est["cost_yuan"],
        cost_text=format_cost(est["cost_yuan"]),
        caveat=_TOPUP_CAVEAT,
    )


@router.post("/verify", response_model=VerifyResponse)
def verify_key(req: VerifyRequest) -> VerifyResponse:
    """发一个 max_tokens=1 的微型请求验证 key 是否可用，并区分失败原因。

    只验证文本模型：视觉模型通常更贵，且各家图像模型名变动频繁，
    验证时多打一次只为了确认「图片能不能读」不划算——文本通了即可放行。
    """
    provider, tier_id = effective_provider(req.provider_id.strip(), req.tier)
    key = req.api_key.strip()
    if provider is None:
        raise HTTPException(status_code=400, detail="未知的服务商")
    if not key:
        return VerifyResponse(ok=False, kind="empty", message="请先粘贴 API Key")
    return _probe(provider, key)


def _topup_hint(provider: dict, kind: str) -> str:
    """余额/限流类错误的兜底指引：充值入口 + 换一家有免费额度的服务商。

    只对「付费档」生效——免费档模型标价 0 元，提示充值会自相矛盾（见 _is_free_tier）。
    出现在设置面板的验证结果里（此时用户正停在面板上），充值 URL 会以纯文本
    显示在面板帮助/上方链接中，可点可复制。
    """
    url = provider.get("topup_url") or ""
    name = provider.get("name", "")
    pay = f"充值入口：{url}" if url else f"请到 {name} 官网控制台充值"
    if kind == "insufficient_balance":
        return f"{pay}；若不想充值，可换一家有免费额度的服务商再验证。"
    return "可稍后重试；若持续出现多为额度或并发限制，可换一家服务商试试。"


def _is_free_tier(provider: dict) -> bool:
    """该服务商当前配置的文本/视觉模型单价全为 0（如智谱免费档 glm-4.7-flash）。

    免费档的失败原因与付费档完全不同（无「充值」概念），错误指引要分叉。
    """
    return (
        provider.get("price_in", 0) == 0
        and provider.get("price_out", 0) == 0
        and provider.get("vision_price_in", 0) == 0
        and provider.get("vision_price_out", 0) == 0
    )


def _raw_hint(exc) -> str:
    """把服务商原始错误（code + message）透出到文案末尾。

    教训：智谱免费档真实报错是 429 code 1305「访问量过大」，曾被我们误归因为
    「余额不足」。与其二次猜测平台语义，不如把原文给用户（也便于日后排查）。
    """
    body = _err_body(exc).strip()
    if not body:
        return ""
    if len(body) > 160:
        body = body[:160] + "…"
    return f"（服务商原文：{body}）"


def _rate_limit_message(provider: dict, exc) -> str:
    """HTTP 429 的文案：免费档与付费档原因完全不同，不能共用一句归因。"""
    raw = _raw_hint(exc)
    if _is_free_tier(provider):
        return (
            "免费模型当前繁忙（HTTP 429）：免费档是共享资源池，高峰期经常超载"
            "（智谱常见 code 1305「访问量过大」），并非你的 key 或余额问题。"
            "建议稍后再试；赶时间可临时换用该服务商的付费模型，或换一家有免费额度的服务商。"
            + raw
        )
    return "请求被限流（HTTP 429）：多为并发超限或额度用尽，可稍后重试。" + raw


def _balance_message(provider: dict, exc) -> str:
    """疑似余额/额度类错误（402 或错误文本含 quota/额度/余额等）的文案。

    免费档不存在「余额不足」，此时提示充值就是自相矛盾——免费模型被拒通常是
    未实名认证 / 未在控制台开通该模型 / 当日额度用尽，指引去控制台而非充值。
    """
    raw = _raw_hint(exc)
    if _is_free_tier(provider):
        return (
            "你选的是免费模型（标价 0 元，不需要充值），但平台拒绝了请求，通常是："
            "① 智谱账户未完成实名认证；② 免费模型未在控制台开通；③ 当日免费额度用尽。"
            "请到服务商控制台完成实名认证并检查模型开通状态，再回来验证。"
            + raw
        )
    return (
        "Key 有效但余额不足（或未开通该模型）。" + _topup_hint(provider, "insufficient_balance") + raw
    )


def _probe(provider: dict, api_key: str) -> VerifyResponse:
    from openai import (
        APIConnectionError,
        APIStatusError,
        AuthenticationError,
        NotFoundError,
        OpenAI,
        RateLimitError,
    )

    base_url = provider["base_url"]
    model = provider["model"]
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=15)
    try:
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
    except AuthenticationError as exc:
        return VerifyResponse(
            ok=False, kind="invalid_key",
            message="Key 无效（401/403）：请检查是否复制完整、有没有多余空格，或是否已删除停用。"
            + _raw_hint(exc),
        )
    except NotFoundError as exc:
        return VerifyResponse(
            ok=False, kind="model_not_found",
            message=f"模型名 {model} 在该平台不可用（可能已下架改名），请联系开发者更新内置配置。"
            + _raw_hint(exc),
        )
    except RateLimitError as exc:
        return VerifyResponse(
            ok=False, kind="rate_limit",
            message=_rate_limit_message(provider, exc),
        )
    except APIConnectionError:
        return VerifyResponse(
            ok=False, kind="network",
            message="连不上服务商服务器：请检查网络（能否正常上网），稍后重试。",
        )
    except APIStatusError as exc:
        if _looks_like_balance(exc):
            return VerifyResponse(
                ok=False, kind="insufficient_balance",
                message=_balance_message(provider, exc),
            )
        if exc.status_code in (401, 403):
            return VerifyResponse(
                ok=False, kind="invalid_key",
                message="Key 无效（401/403）：请检查是否复制完整、有没有多余空格。"
                + _raw_hint(exc),
            )
        return VerifyResponse(
            ok=False, kind="error",
            message=f"服务商返回错误（HTTP {exc.status_code}）：{_err_body(exc)}",
        )
    except Exception as exc:  # noqa: BLE001 尽量给出可读信息而不是裸异常
        return VerifyResponse(
            ok=False, kind="error", message=f"验证失败：{type(exc).__name__}: {exc}"
        )
    return VerifyResponse(ok=True, kind="ok", message="Key 有效，已保存。可以开始使用了。")


def _looks_like_balance(exc) -> bool:
    """按状态码或错误文本判断是否「余额/额度」问题（各家文案不一，多兜几层）。"""
    if exc.status_code == 402:
        return True
    body = _err_body(exc).lower()
    return any(k in body for k in ("balance", "insufficient", "quota", "额度", "余额", "欠费"))


def _err_body(exc) -> str:
    raw = getattr(exc, "body", None)
    if isinstance(raw, dict):
        # 智谱等平台错误体带 code（如 1305 访问量过大），拼进文案便于定位
        code = raw.get("code")
        msg = raw.get("message") or raw.get("error", {})
        if isinstance(msg, dict):
            msg = msg.get("message", "")
        text = str(msg).strip()
        prefix = f"code {code}: " if code not in (None, "") else ""
        return prefix + text
    if raw:
        return str(raw)[:300]
    return str(exc)[:300]


@router.post("/save", response_model=SaveResponse)
def save_key(req: SaveRequest) -> SaveResponse:
    """保存 key 与档位到用户配置并热重载，使运行中的服务立即用上新值。"""
    provider, _ = effective_provider(req.provider_id.strip(), req.tier)
    if provider is None:
        raise HTTPException(status_code=400, detail="未知的服务商")
    if not req.api_key.strip():
        raise HTTPException(status_code=400, detail="API Key 不能为空")
    save_user_settings(req.provider_id, req.api_key, req.tier)
    reload_settings()
    label = provider.get("tier_label", "")
    suffix = f"（{label}）" if label else ""
    return SaveResponse(ok=True, message=f"已保存 {provider['name']}{suffix} 的配置")


@router.post("/clear", response_model=SaveResponse)
def clear_key() -> SaveResponse:
    """清除用户配置（回退到 .env / 未配置状态）。"""
    clear_user_settings()
    reload_settings()
    return SaveResponse(ok=True, message="已清除本地配置")


def _tail(key: str, visible: int = 4) -> str:
    """掩码显示 key 尾部，避免前端整串回显。"""
    return f"…{key[-visible:]}" if len(key) > visible else "…"


def current_settings_snapshot() -> dict:
    """给设置页/调试用：当前生效的 provider 与模型（不含 key）。"""
    s: Settings = get_settings()
    return {
        "base_url": s.ai_base_url,
        "model": s.ai_model,
        "vision_model": s.ai_vision_model,
    }
