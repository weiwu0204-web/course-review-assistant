"""成本预估：在真正调用模型前按字数/图片数粗估本次生成的 token 与费用。

为什么要这个模块：余额不足的第一大成因是「不知道会花多少钱」，额度在反复生成中
悄悄烧完。生成前给一个「预计花费约 ¥X」的数字（按当前服务商的官方单价、宁高勿低），
把成本焦虑前置，也让「余额不足」报错出现时用户不意外。

估算粗糙度声明：真实账单受 token 化细节、思考模式推理 token、上下文缓存命中、
DeepSeek 峰谷价等因素影响，这里只保证「数量级正确 + 偏保守（偏高）」，展示文案
一律附注「以官方账单为准」。
"""
from __future__ import annotations

# 中文汉字 → token 换算：约 1 token ≈ 1.5 汉字（取保守值 1.5，token 数偏大即费用偏大）
CHARS_PER_TOKEN = 1.5
# 单张图片折算的输入 token。旧值 384 是照抄 OpenAI 的口径、并非实测。
# 2026-09-11 用 14 份真实课件（Ceph 课程 / 273 张图）拟合：DeepSeek 视觉约 **110/张**。
# 各家计费口径不同，所以优先取服务商的 vision_tokens_per_image 字段；
# 没有实测值的服务商沿用这里的 384（保守，宁可高估）。
TOKENS_PER_IMAGE = 384
# 系统提示词 + 模板等固定输入开销（每篇文档）
FIXED_INPUT_TOKENS = 1500

# 输出 token 与「页数」挂钩，**不与输入字数挂钩**——这是 2026-09-11 标定后推翻的旧假设。
# 依据：14 份真实课件实测 completion 平均 **562 token/页**（区间 417–817），
# 与输入字数弱相关，而与页数强相关。原因是复习文档本质是**扩写**：
# 课件是提纲式短句，文档要展开成完整段落，输出体量由信息量（≈页数）决定。
# 旧公式「输出 = 输入字数 ÷ 1.5 × 0.5」把 15,374 token 的一份估成 1,387，低估 11 倍。
# 详细档取 700（实测均值 +25%），符合本模块「宁高勿低」的口径。
# 表格化/精简按原风格比例缩放，**尚未标定**——用这两档时估算更偏保守。
OUT_PER_PAGE = {"详细": 700, "表格化": 490, "精简": 250}
# 拿不到页数时的兜底（旧的字数比例路径）。已知会明显低估，仅防调用方漏传 pages。
OUTPUT_RATIO = {"精简": 0.25, "表格化": 0.35, "详细": 0.5}
# 思考模式开启时模型会先产出推理 token（2026-09-04 核实四家思考默认全开），
# 输出量按此系数兜底（宁高勿低）。前端默认关闭思考，只有开思考时才乘。
THINKING_FACTOR = 1.5


def estimate_generation_cost(
    provider: dict,
    chars: int,
    image_count: int,
    use_vision: bool,
    exam_chars: int = 0,
    style: str = "详细",
    thinking: bool = True,
    pages: int = 0,
) -> dict:
    """按当前生效的服务商配置粗估一次生成的 token 量与花费。

    输入 token = 课件文字 + 备注 + 往年试题文字（汉字→token）+ 图片 token + 固定开销；
    输出 token = **页数 × 风格每页 token**（pages=0 时退回按字数的粗估，已知偏低）。
    走视觉模型时整个请求按视觉模型单价计（同请求只按一个模型收），否则按文本价。

    thinking=True 表示本次请求开启思考模式（推理 token 多，费用更高）——
    默认 True 保持与「服务商思考默认开启」的旧行为一致，调用方（前端估算）显式传入。

    pages 是标定输出的关键入参，**所有真实调用点都应传**（extract 结果里有 total_pages）。
    """
    text_chars = max(0, int(chars)) + max(0, int(exam_chars))
    per_img = provider.get("vision_tokens_per_image") or TOKENS_PER_IMAGE
    img_tokens = max(0, int(image_count)) * per_img if use_vision else 0

    text_in_tokens = text_chars / CHARS_PER_TOKEN + FIXED_INPUT_TOKENS
    tokens_in = int(text_in_tokens + img_tokens)

    if pages and int(pages) > 0:
        out_tokens = int(OUT_PER_PAGE.get(style, 700) * int(pages))
    else:
        out_tokens = int(text_chars / CHARS_PER_TOKEN * OUTPUT_RATIO.get(style, 0.5))
    if thinking:
        out_tokens = int(out_tokens * THINKING_FACTOR)

    vision_billed = use_vision and img_tokens > 0
    price_in = provider.get("vision_price_in" if vision_billed else "price_in")
    price_out = provider.get("vision_price_out" if vision_billed else "price_out")
    if price_in is None:
        price_in = provider.get("price_in", 0)
    if price_out is None:
        price_out = provider.get("price_out", 0)

    cost = (tokens_in * float(price_in) + out_tokens * float(price_out)) / 1_000_000

    return {
        "provider_name": provider.get("name", ""),
        "billed_model": "vision" if vision_billed else "text",
        "price_in": float(price_in),
        "price_out": float(price_out),
        "tokens_in": tokens_in,
        "tokens_out": out_tokens,
        "cost_yuan": round(cost, 3),
    }


def format_cost(cost_yuan: float) -> str:
    """把金额格式化成展示用文案：一分钱以下显示「不到 1 分钱」。"""
    if cost_yuan <= 0.0:
        return "约 0 元"
    if cost_yuan < 0.01:
        return "不到 1 分钱"
    return f"约 ¥{cost_yuan:.2f}"
