"""AI 生成复习文档服务：调用 OpenAI 兼容接口生成复习资料。

按本次是否附带图片自动选择模型：
  - 无图 / 服务商未配视觉模型：走纯文本模型（设置页默认文本模型）
  - 有图且服务商支持：走视觉模型，把幻灯片里的图片一起发过去

两者共用同一个 base_url 和 API Key，只换 model 字符串。
"""
import base64
from collections import defaultdict
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from app.config import OUTPUT_ROOT, Settings, current_provider, get_settings
from app.models import ExtractionResult, GenerateRequest
from app.services.agent_repair import (
    RepairRecord,
    needs_repair,
    repair_document,
    token_cost,
)
from app.services.coverage import (
    CoverageReport,
    applies_to,
    measure,
    not_applicable_note,
)
from app.services.ppt_extractor import format_for_ai

SYSTEM_PROMPT = "你是擅长课程复习的 AI 助手，输出规范的 Markdown。"

# DeepSeek 视觉接口的硬限制，见 https://api-docs.deepseek.com/zh-cn/guides/vision/
MAX_IMAGES_PER_REQUEST = 600
MAX_IMAGE_BYTES_TOTAL = 32 * 1024 * 1024

# 同一张图出现在这么多页以上，就当作母版背景/水印/logo 直接丢弃
DECOR_REPEAT_PAGES = 3

# max_tokens 被服务商拒绝时的降级值。不去硬编码各家输出上限（会随版本过期，
# 且多数家我们没核实过），改为「被 400 拒绝就退回这个保守值重试一次」，
# 这样抬高的默认值只对真正支持的家生效，其余家自动退化、不会整个失败。
MAX_TOKENS_FALLBACK = 16384

PROMPT_TEMPLATES: dict[str, str] = {
    "复习指南": """你是大学生的复习助手。请基于下面的 PPT 内容，生成一份结构化的期末复习指南。
要求：
1. 按章节/主题组织，提炼核心概念与定义
2. 列出重点公式、模型或结论
3. 标注易错点 / 易混淆概念（如 ARP 该归哪一层、表示层 vs 应用层这类考试高频辨析点）
4. 给出建议的复习顺序
5. 使用 Markdown 格式输出，结构清晰
6. 不要生成「深度思考题 / 开放讨论题」等非考试型内容——期末考卷考的是考点，不是开放式讨论。PPT 里若出现思考题页，只提炼其背后考察的知识点即可
7. 禁止编造 PPT 里没有的数据、年份、题目或"老师强调过"之类的内容""",
    "重点总结": """你是大学生的复习助手。请基于下面的 PPT 内容，提炼出最可能考的重点。
要求：
1. 只保留关键知识点，删除废话
2. 用简短的要点列出每个知识点
3. 对重点内容做加粗或标记
4. 不要生成开放式的"思考题"章节，只做考点提炼
5. 使用 Markdown 格式输出""",
    "习题生成": """你是出题老师。请基于下面的 PPT 内容，生成一份模拟练习题卷（题型贴近该课程常规期末考：选择/判断/填空/简答/综合）。
要求：
1. 包含 10 道题：5 道选择题、3 道简答题、2 道综合题
2. 选择题附带答案和解析
3. 难度覆盖基础、中等、提高
4. 若本提示词后面提供了「往年试题」，则一律按往年试题规则处理（直搬或参考风格），不再自行编题型
5. 除非引用的是提供的往年试题原文，否则不得声称题目"来自某校/某年真题"——没有真实出处的题只能标为模拟练习题
6. 使用 Markdown 格式输出""",
}

# 往年试题作为「最高优先级题源」时的处理规则：直搬 / 参考风格出预测题
EXAM_INSTRUCTIONS: dict[str, str] = {
    "direct": """往年试题使用规则（直搬原题）：
1. 在文档合适位置增加「历年真题」章节，把上面往年试题中与本 PPT 知识点相关的题目【原样】整理进去，按题型分组（选择/判断/填空/简答/综合）。
2. 题目一个字都不要改：不改写、不合并、不"优化"表述、不增删选项。
3. 题目自带答案/解析的保留；没有答案的绝不编造，标「（无答案，需自行核对）」。
4. 与本 PPT 知识点无关的题目直接删掉，不要硬凑。
5. 章节标题写明来源，例如「历年真题（依据上传的往年试题整理）」。
""",
    "predict": """往年试题使用规则（参考风格出预测题）：
1. 在文档合适位置增加「预测题」章节。
2. 先分析上面往年试题的题型结构、题量、难度与出题风格，再结合本 PPT 的核心考点，仿照该风格生成 6~10 道预测题，题型比例贴近原卷。
3. 预测题必须真正考察 PPT 里的考点，不超纲、不编造 PPT 外的新知识。往年试题里与 PPT 无关的知识点，只可参考其题型与问法，不得把题目本身搬进预测题。
4. 每题必须新拟：题干与选项不得照抄、也不得近似复述往年试题中任何一道原题（同考点可以保留，但要换场景、换问法、换选项重新出题）。
5. 每题附参考答案与简短解析。
6. 章节标题必须写明「预测题（依据往年试题风格改编，非原题）」，不得冒充真实真题。
""",
}

# 往年试题文本太长就截断，避免挤占输入窗口
EXAM_MAX_CHARS = 20000

STYLE_INSTRUCTIONS: dict[str, str] = {
    "精简": "语言精炼，直达要点，篇幅控制在 500 字以内。",
    "详细": "内容详尽，覆盖所有知识点，可结合例子说明。",
    "表格化": "尽量使用表格对比、汇总关键信息，便于快速浏览。",
}

# 习题生成专用的风格说明：通用风格里的「篇幅 500 字以内」「表格汇总关键信息」
# 是复习文档语境，套到试卷上会自相矛盾——模拟卷要保证题量，题目也不能装进表格。
EXAM_STYLE_INSTRUCTIONS: dict[str, str] = {
    "详细": "题目要求：每题附完整参考答案与解析，讲清考点与易错点。",
    "精简": "题目要求：每题附参考答案与一句话要点即可，不展开长篇解析。",
}


def _digest_of(path: str) -> str:
    """从导出文件名里取回图片指纹（命名规则见 extract_slide_images）。"""
    return Path(path).stem.split("_")[-1]


def collect_images(
    result: ExtractionResult, use_vision: bool
) -> tuple[list[str], list[int], int]:
    """挑出要发给视觉模型的图片，返回 (路径, 页码, 被当作装饰丢弃的张数)。

    先剔除装饰图：同一张图出现在 DECOR_REPEAT_PAGES 页以上，基本可以断定是
    母版背景、水印或 logo。实测一份 19 页课件里有一张 1280x720 的背景图占了
    18 页，不过滤就白烧 18×384 tokens，还会稀释模型对真正内容的注意力。

    剩下的再按单请求 600 张 / 32MiB 的上限截断，超出直接丢弃而不是让请求失败。
    """
    if not use_vision:
        return [], [], 0

    occurrences: dict[str, set[int]] = defaultdict(set)
    for slide in result.slides:
        for raw in slide.images:
            occurrences[_digest_of(raw)].add(slide.page)

    decoration = {d for d, seen_on in occurrences.items() if len(seen_on) >= DECOR_REPEAT_PAGES}

    paths: list[str] = []
    pages: list[int] = []
    total = 0
    skipped = 0

    for slide in result.slides:
        for raw in slide.images:
            if _digest_of(raw) in decoration:
                skipped += 1
                continue
            fp = Path(raw)
            if not fp.exists():
                continue
            size = fp.stat().st_size
            if len(paths) >= MAX_IMAGES_PER_REQUEST or total + size > MAX_IMAGE_BYTES_TOTAL:
                return paths, pages, skipped
            total += size
            paths.append(raw)
            if slide.page not in pages:
                pages.append(slide.page)

    return paths, pages, skipped


def build_prompt(
    result: ExtractionResult,
    request: GenerateRequest,
    image_pages: list[int],
    exam_text: str = "",
) -> str:
    """根据用户选择、本次附带的图片和往年试题组装完整提示词。"""
    template = PROMPT_TEMPLATES.get(request.template, PROMPT_TEMPLATES["复习指南"])
    # 习题生成用试卷语境的风格说明；其余文档类型走通用风格。收到字典外的值
    # （如习题生成 + "表格化"，前端已不提供该选项，此处纯防御）兜底到详细。
    if request.template == "习题生成":
        style = EXAM_STYLE_INSTRUCTIONS.get(
            request.style, EXAM_STYLE_INSTRUCTIONS["详细"]
        )
    else:
        style = STYLE_INSTRUCTIONS.get(request.style, STYLE_INSTRUCTIONS["详细"])
    body = format_for_ai(result)

    extra = ""
    if image_pages:
        shown = "、".join(str(p) for p in image_pages[:40])
        extra = (
            f"\n\n补充说明：除文字外，本次还附带了第 {shown} 页的幻灯片图片。"
            "请结合图片里的内容（示意图、表格、公式、截图）一起理解，这些同样可能考。"
            "图片中看不清或不存在的结论不要凭空编造。"
        )

    prompt = f"{template}\n\n{style}{extra}\n\n以下是 PPT 的内容：\n\n{body}"

    exam = exam_text.strip()
    if exam:
        rule = EXAM_INSTRUCTIONS.get(request.exam_mode, EXAM_INSTRUCTIONS["direct"])
        truncated = exam if len(exam) <= EXAM_MAX_CHARS else exam[:EXAM_MAX_CHARS]
        prompt += (
            f"\n\n===== 本校往年试题（最高优先级题源，生成题目时以此为准）=====\n"
            f"{truncated}\n\n{rule}"
            "\n\n长度规划：以上述试题为依据的题目章节是用户最看重的部分，"
            "必须完整输出到文档末尾。若总篇幅紧张，宁可在前面的知识点叙述上精简，"
            "也绝不允许省略或截断题目章节。"
        )

    return prompt


def _collect(result: ExtractionResult, request: GenerateRequest, settings) -> tuple[list[str], list[int], int]:
    """收集要发给视觉模型的图片；服务商没配视觉模型时自动降级为纯文本。

    设置页只有少数模型能读图（各家的 vision_model），用户换了一家不支持读图
    的服务商时，直接跳过图片而不是让生成失败——最坏结果只是图片内容不纳入。
    """
    if not request.use_vision:
        return [], [], 0
    if not settings.ai_vision_model:
        print("[生成] 当前服务商未配置视觉模型，图片已跳过，仅基于文字生成")
        return [], [], 0
    return collect_images(result, True)


def build_vision_content(prompt: str, image_paths: list[str]) -> list[dict]:
    """把文本提示词和图片拼成多模态 content 数组。"""
    content: list[dict] = [{"type": "text", "text": prompt}]

    for raw in image_paths:
        fp = Path(raw)
        try:
            b64 = base64.b64encode(fp.read_bytes()).decode("utf-8")
        except OSError:
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )

    return content


def build_client(settings) -> OpenAI:
    """按当前配置构造 OpenAI 兼容客户端。

    显式带上 max_retries（429/5xx 指数退避自动重试，失败请求不计费）与
    总超时（视觉请求要上传几十 MB base64、长文档生成可能耗时数分钟）。
    单独抽成函数便于单测断言重试/超时契约，不用发真实请求。
    """
    return OpenAI(
        api_key=settings.ai_api_key,
        base_url=settings.ai_base_url,
        max_retries=settings.ai_max_retries,
        timeout=settings.ai_timeout_seconds,
    )


def generate_review_document(
    result: ExtractionResult, request: GenerateRequest, exam_text: str = ""
) -> tuple[str, int, bool]:
    """兼容壳：等价于 run_single，但丢掉 usage 与 coverage，返回旧的 (markdown, 图片数, 截断) 三元组。

    仅给 scripts/ 下的离线评估脚本与既有测试用。
    **/api/generate 与批任务都已改走 run_single**——它们需要 usage（按实际 token 记账）
    和 coverage（覆盖率回检），这个壳取不到。
    """
    out = run_single(result, request, exam_text)
    return out.markdown, out.image_count, out.truncated


@dataclass
class GenerateOutcome:
    """一次「单份课件 → 复习文档」调用的完整结果。

    批任务每完成一个 job 把它落盘成 meta.json 的核心；usage 为 None 表示
    服务商本次未返回用量（此时费用只能按估算记，见 batch_runner）。
    coverage 为覆盖率回检结果（见 services/coverage.py）；None 表示未算或算失败——
    它只是辅助信息，缺失不影响文档本身的有效性。
    """

    markdown: str
    image_count: int = 0
    truncated: bool = False
    used_vision: bool = False
    exam_used: bool = False
    usage: dict | None = None
    coverage: "CoverageReport | None" = None
    # 不做覆盖率回检时的一句解释（档位不适用）——沉默会让用户以为功能坏了
    coverage_note: str = ""
    # P2：低覆盖时的自动补漏记录（None = 未开启或未触发）。
    # 用户有权知道自己的文档被补过几轮、花了多少钱——见 docs/P2-REPAIR.md §6。
    repair: "RepairRecord | None" = None

    def coverage_dict(self) -> dict | None:
        return self.coverage.to_dict() if self.coverage else None


def _extract_usage(response) -> dict | None:
    """从 OpenAI 兼容响应里取 token 用量；响应没带 usage（部分服务商/降级场景）返回 None。

    usage 可能是对象（官方 SDK）也可能是 dict（个别兼容层），统一折成 dict。
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if isinstance(usage, dict):
        return {k: int(usage.get(k, 0) or 0) for k in keys}
    return {
        k: int(getattr(usage, k, 0) or 0) for k in keys
    }


def _generation_cost(usage: dict | None, provider: dict, vision: bool) -> float:
    """本次生成的费用（按该请求实际走的模型口径），给 repair 定成本上限用。

    生成可能走视觉模型（带图），repair 是纯文本，两者单价不同——上限要用生成的
    真实口径算，否则带图生成的 repair 预算会被严重高估。
    """
    if not usage:
        return 0.0
    pin = float(provider.get("vision_price_in" if vision else "price_in") or 0)
    pout = float(provider.get("vision_price_out" if vision else "price_out") or 0)
    return token_cost(usage, pin, pout)


def _create_completion(client, kwargs: dict):
    """发起生成请求；服务商因 max_tokens 过大拒绝时，降级重试一次。

    默认 max_tokens 抬到 32768 后，输出上限低于此的服务商（各家不一、且会随
    版本变化）可能直接 400。与其硬编码一份会过期的上限表，不如让系统自愈：
    按错误文本判断是「max_tokens 过大」就退回保守值重试一次，其余异常照常抛出。
    """
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 需按文本判断，不能只认某个异常类
        low = str(exc).lower()
        too_large = getattr(exc, "status_code", None) == 400 and any(
            k in low for k in ("max_tokens", "max output", "maximum", "too large")
        )
        if not too_large or kwargs.get("max_tokens", 0) <= MAX_TOKENS_FALLBACK:
            raise
        print(
            f"[生成] 服务商拒绝 max_tokens={kwargs['max_tokens']}，"
            f"降级为 {MAX_TOKENS_FALLBACK} 重试"
        )
        return client.chat.completions.create(
            **{**kwargs, "max_tokens": MAX_TOKENS_FALLBACK}
        )


def run_single(
    result: ExtractionResult,
    request: GenerateRequest,
    exam_text: str = "",
    repair_workdir: Path | None = None,
    *,
    settings_override: Settings | None = None,
) -> GenerateOutcome:
    """调用大模型生成单份课件的复习文档（map 阶段的原子单元）。

    与 /api/generate 的唯一区别是返回完整结果（含 usage），批任务、单份端点共用
    此实现，避免两套逻辑漂移。exam_text 为往年试题原文（可选）。

    ``repair_workdir``：P2 补漏的过程目录（会话上下文、原文备份、补后文档）；
    批任务传该 job 自己的目录，单份不传则落在 ``output/_repair/<随机名>``。
    """
    settings = settings_override if settings_override is not None else get_settings()
    if not settings.ai_api_key:
        raise RuntimeError(
            "尚未配置 AI API Key：请点击右上角「设置」选择服务商并粘贴你的 Key。"
        )

    image_paths, image_pages, skipped = _collect(result, request, settings)
    if skipped:
        print(f"[提取] 跳过 {skipped} 张重复出现的装饰图（背景/水印/logo）")
    prompt = build_prompt(result, request, image_pages, exam_text)

    client = build_client(settings)

    if image_paths:
        model = settings.ai_vision_model
        content: str | list[dict] = build_vision_content(prompt, image_paths)
    else:
        model = settings.ai_model
        content = prompt

    # 思考模式：2026-09-04 核实四家服务商思考默认全开，所以 request.thinking=True
    # 时不传任何参数即走默认思考；False 时显式关闭（省 30-50% 费用、更快）。
    # 各家关闭参数格式不同（qwen 是 enable_thinking，其余是 thinking.type），
    # 由设置面板所选服务商的 thinking_off 字段带出，经 extra_body 透传。
    create_kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
        "max_tokens": settings.ai_max_tokens,
    }
    if not request.thinking and settings.ai_thinking_off:
        create_kwargs["extra_body"] = settings.ai_thinking_off

    response = _create_completion(client, create_kwargs)

    choice = response.choices[0]
    markdown = (choice.message.content or "").strip()
    truncated = getattr(choice, "finish_reason", "") == "length"
    if truncated:
        print(f"[生成] 警告：输出达到 max_tokens={settings.ai_max_tokens} 被截断，"
              "文档尾部内容（通常是题目章节）可能缺失")

    # 覆盖率回检（免费、纯文本比对）。**必须兜住异常**：它只是给用户的辅助参考，
    # 任何计算问题都不该让已经生成好的文档作废。
    #
    # 只在「复习指南 / 详细」档算——只有这一档承诺全面覆盖知识点。其它档位
    # （习题生成/重点总结/精简）本就不追求全覆盖，硬打分等于用不成立的承诺去指责
    # 文档（实测会得出 58% 这种吓人且错误的结论，见 coverage.COVERAGE_TEMPLATES）。
    coverage: CoverageReport | None = None
    coverage_note = ""
    if applies_to(request.template, request.style):
        try:
            coverage = measure(result, markdown)
        except Exception as exc:  # noqa: BLE001
            print(f"[覆盖率] 计算失败（不影响文档）：{type(exc).__name__}: {exc}")
    else:
        coverage_note = not_applicable_note(request.template, request.style)

    usage = _extract_usage(response)

    # P2：低覆盖时自动补漏（verify → repair 闭环）。**默认关闭**——要额外花钱。
    # 触发同时要求「档位适用（复习指南/详细）」「样本可靠（术语够多）」「覆盖率 < 94%」，
    # 所以对本来就生成良好的文档是零触发、零成本。
    repair_rec: RepairRecord | None = None
    if request.enable_repair and needs_repair(coverage):
        workdir = repair_workdir or (OUTPUT_ROOT / "_repair" / uuid4().hex[:8])
        prov = current_provider() or {}
        print(
            f"[repair] 覆盖率 {coverage.percent}% 低于阈值，开始自动补漏"
            f"（工作目录 {workdir}）"
        )
        fixed, repair_rec = repair_document(
            result,
            markdown,
            coverage,
            workdir=workdir,
            base_cost_yuan=_generation_cost(usage, prov, bool(image_paths)),
            provider=prov,
        )
        if not repair_rec.rolled_back and repair_rec.final_percent > coverage.percent:
            markdown = fixed
            # 覆盖率必须跟着文档走——数字与正文不一致等于给用户错信息
            try:
                coverage = measure(result, markdown)
            except Exception as exc:  # noqa: BLE001
                print(f"[repair] 补漏后重算覆盖率失败（沿用旧值）：{exc}")
        print(
            f"[repair] 结束：{repair_rec.trigger_percent}% → {repair_rec.final_percent}%，"
            f"{len(repair_rec.rounds)} 轮，¥{repair_rec.total_cost_yuan}"
            f"{'（已回滚）' if repair_rec.rolled_back else ''}"
            f"{'｜' + repair_rec.note if repair_rec.note else ''}"
        )

    return GenerateOutcome(
        markdown=markdown,
        image_count=len(image_paths),
        truncated=truncated,
        used_vision=len(image_paths) > 0,
        exam_used=bool(exam_text.strip()),
        usage=usage,
        coverage=coverage,
        coverage_note=coverage_note,
    )
