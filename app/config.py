"""应用配置：从环境变量 / .env 文件加载设置，并支持用户级配置覆盖。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.user_config import load_user_settings

# 产物根目录（批任务产物、单份 repair 过程文件都在它下面）。
# 放在 config 里，是为了让 batch_runner / ai_generator / agent_repair
# 共用同一个定义，而不是各自算一遍路径。
OUTPUT_ROOT: Path = Path(__file__).resolve().parent.parent / "output"

# 设置页可选的 AI 服务商（模型名/单价/思考行为于 2026-09-04 第四次核实，依据各家官方定价页/API 文档）。
# 若某家模型名/价格随官方调整，只需改这里，无需动业务代码。
# 注意：deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用。
#
# 价格字段约定：单位 = 元 / 百万 tokens（缓存未命中），peak=高峰/常规价。
#   price_in / price_out        —— 文本模型的输入、输出单价（0 = 免费档）
#   vision_price_in/out         —— 视觉模型单价（缺省回退文本价，即原生多模态模型）
#   vision_tokens_per_image     —— 单张图片折算的输入 token。各家计费口径不同：
#                                  缺省用 cost_estimate.TOKENS_PER_IMAGE（384，保守）；
#                                  2026-09-11 用 14 份真实课件（273 张图）实测 DeepSeek ≈ 110。
#   topup_url                   —— 控制台/充值入口（文案层面给「去充值」跳转）
#   thinking_off                —— 「关闭思考模式」的请求参数。2026-09-04 核实：四家思考默认全开，
#                                  复习文档是长文档结构化任务，关思考省 30-50% 费用且更快。
#                                  OpenAI SDK 的非标准参数须走 extra_body，各家格式不同：
#                                  deepseek/zhipu/moonshot 用 {"thinking": {"type": "disabled"}}，
#                                  qwen 用 {"enable_thinking": False}。
# 各家 note 附阶梯价与免费额度，供「成本预估」提示与设置面板展示。
#
# ⚠️ 改动本表前先看 docs/PROVIDER-MODELS.md —— 那里有带核实日期与来源的型号/价格基准。
#    项目里模型名被写错/写旧过多次（glm-4.5v→4.6v、kimi-k2→k2.6、glm-4.6→4.7），
#    每次都是实测才发现；根因就是没有这份基准表。
AI_PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "vision_model": "deepseek-v4-flash-vision-exp",
        "note": "新用户送免费额度；价格低，中文与文档理解强",
        "price_in": 3.0,
        "price_out": 9.0,
        "vision_price_in": 3.0,
        "vision_price_out": 9.0,
        # 2026-09-11 实测：14 份 Ceph 课件 / 273 张图，单张实际约 110 token（旧口径 384 高估 3.5 倍）
        "vision_tokens_per_image": 110,
        "price_note": "输入 3 / 输出 9 元每百万 tokens（高峰价；空闲时段半价）",
        "topup_url": "https://platform.deepseek.com/top_up",
        "thinking_off": {"thinking": {"type": "disabled"}},
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # 默认档 = 付费档（顶层字段即付费档，兼容旧引用）。免费档经 tiers.free 覆盖。
        "model": "glm-4.7",
        "vision_model": "glm-4.6v",
        "note": "付费档：文本 glm-4.7（200K）、看图 glm-4.6v；新注册送 tokens 可先抵扣",
        "price_in": 2.0,
        "price_out": 8.0,
        "vision_price_in": 2.0,
        "vision_price_out": 6.0,
        "price_note": "付费档：文本 2/8、看图 glm-4.6v 2/6 元每百万 tokens（输入≤32K）",
        "topup_url": "https://open.bigmodel.cn/console",
        "thinking_off": {"thinking": {"type": "disabled"}},
        # 档位：设置面板可选付费/免费。default_tier 必须存在于 tiers 中。
        # effective_provider() 把选中档位覆盖层合并到顶层字段；paid 层留空即可
        # （顶层即付费档），free 层只写与付费档不同的部分。
        "default_tier": "paid",
        "tiers": {
            "paid": {"tier_label": "付费档（稳定）"},
            "free": {
                "tier_label": "免费档（0 元）",
                "model": "glm-4.7-flash",
                "vision_model": "glm-4.6v-flash",
                "price_in": 0.0,
                "price_out": 0.0,
                "vision_price_in": 0.0,
                "vision_price_out": 0.0,
                "note": "免费档：文本/看图均标价 0 元，但需实名认证；免费模型是共享资源池，"
                "高峰期经常超载（HTTP 429 code 1305「访问量过大」），赶时间慎用",
                "price_note": "免费档（0 元）；高峰易超载、限流较严，实名认证后可用",
            },
        },
    },
    "moonshot": {
        "name": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "vision_model": "kimi-k2.6",
        "note": "K2.6 原生多模态、256K，文本与视觉同模型；kimi-k2.5 及更早已于 2026-08-31 全平台下线",
        "price_in": 6.5,
        "price_out": 27.0,
        "price_note": "输入 6.5 / 输出 27 元每百万 tokens（四家里最贵；思考默认开启，关思考可大幅省钱）",
        "topup_url": "https://platform.moonshot.cn/console",
        "thinking_off": {"thinking": {"type": "disabled"}},
    },
    "qwen": {
        "name": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-plus",
        "vision_model": "qwen3.7-plus",
        "note": "原生多模态，1M 上下文，图文理解均衡",
        "price_in": 2.0,
        "price_out": 8.0,
        "price_note": "输入 2 / 输出 8 元每百万 tokens（输入≤256K）；开通后 90 天内送 100 万免费 tokens",
        "topup_url": "https://bailian.console.aliyun.com",
        "thinking_off": {"enable_thinking": False},
    },
}


def effective_provider(provider_id: str, tier: str = "") -> tuple[dict | None, str]:
    """按档位解析出最终生效的服务商配置 + 档位 id。

    - 无 tiers 的服务商（deepseek / kimi / qwen 单档）：tier 被忽略，返回原配置与 ""。
    - 有 tiers 的服务商（智谱付费/免费档）：tier 为空或非法时回退 default_tier；
      选中的档位覆盖层合并到顶层字段后返回（顶层即默认档，paid 层可为空）。
    """
    cfg = AI_PROVIDERS.get(provider_id)
    if cfg is None:
        return None, ""
    tiers = cfg.get("tiers") or {}
    if not tiers:
        return cfg, ""
    tid = tier if tier in tiers else cfg.get("default_tier", "")
    if tid not in tiers:
        tid = next(iter(tiers))
    eff = dict(cfg)
    eff.update(tiers[tid])
    eff["tier_id"] = tid
    return eff, tid


class Settings(BaseSettings):
    """全局配置项。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # AI 服务（OpenAI 兼容接口）
    ai_api_key: str = ""
    ai_base_url: str = "https://api.deepseek.com"
    # 纯文本模型。注意：deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用
    ai_model: str = "deepseek-v4-flash"
    # 视觉模型，仅在本次请求附带图片时使用
    ai_vision_model: str = "deepseek-v4-flash-vision-exp"
    # 关闭思考模式的请求参数（由 AI_PROVIDERS 的 thinking_off 带出，用户无需关心）。
    # 空 dict = 不关闭思考（默认思考开启）；非空 dict 经 extra_body 传给接口。
    ai_thinking_off: dict = {}
    # 单次生成的最大输出 token。DeepSeek V4 输出上限 384K，本地限制只为兜底。
    # 复习指南 + 真题章节容易上万 token，8192 会把文末真题截断（实测踩过）。
    # 2026-09-10 实测：16384 仍会截断——一份 18 页「计算机网络」课件因辨析点密集，
    # 写「易错点清单」写到第 15 条就被切断（finish_reason=length）。故翻倍到 32768，
    # 宁可留余量也不中途断章（只有真写满才会多花钱，正常结束的请求不受影响）。
    ai_max_tokens: int = 32768

    # SDK 自动重试次数：对 429 限流 / 5xx 服务端错误做指数退避重试（默认 2 次）。
    # 失败/被拒的请求不会计费，重试是安全的；402 余额不足、400 参数类错误不重试，
    # 直接走上层文案（充值/换服务商指引）。
    ai_max_retries: int = 3
    # 单次请求总超时（秒）。视觉请求要把几十 MB 图片 base64 后一次性上传，
    # 长文档生成也可能耗时数分钟，600s 给足余量；连不上服务商时通常是连接阶段
    # 立即失败，不会真的干等满 10 分钟。
    ai_timeout_seconds: int = 600

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000

    # 上传限制
    max_upload_size_mb: int = 50
    # 上传文件保留时长（小时）：生成完文档后旧上传就没用了，定时清掉避免长期积压。
    # 桌面软件反复使用会不断累积上传副本，.env 可调（测试期可设小值观察清理效果）
    upload_ttl_hours: int = 24


@lru_cache
def get_settings() -> Settings:
    """返回全局配置；用户已在设置页保存 key 时，用其覆盖 .env / 默认值。

    覆盖逻辑：用户配置里的 provider_id 查 AI_PROVIDERS 表，把 base_url / 模型
    一并带出——用户只需要填 key 选服务商，模型名不让他们操心。
    """
    overrides: dict = {}
    user = load_user_settings()
    provider, _ = effective_provider(user.get("provider_id", ""), user.get("tier", ""))
    if provider and user.get("api_key", "").strip():
        overrides = {
            "ai_api_key": user["api_key"].strip(),
            "ai_base_url": provider["base_url"],
            "ai_model": provider["model"],
            "ai_vision_model": provider.get("vision_model", ""),
            "ai_thinking_off": provider.get("thinking_off") or {},
        }
    return Settings(**overrides)


def current_provider_id() -> str:
    """当前生效的服务商 id：优先用户配置，未配置（含 .env 开发态）回退 deepseek。"""
    user = load_user_settings()
    pid = user.get("provider_id", "")
    if pid in AI_PROVIDERS and user.get("api_key", "").strip():
        return pid
    return "deepseek"


def current_provider() -> dict:
    """当前生效的服务商配置项（含价格/充值入口，已按用户档位解析）。"""
    user = load_user_settings()
    provider, _ = effective_provider(user.get("provider_id", ""), user.get("tier", ""))
    if provider:
        return provider
    return AI_PROVIDERS["deepseek"]


def reload_settings() -> None:
    """清空配置缓存，使设置页保存的 key 立即生效（get_settings 带 lru_cache）。"""
    get_settings.cache_clear()