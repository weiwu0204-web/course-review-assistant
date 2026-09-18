"""Pydantic 数据模型：接口请求 / 响应的数据结构。"""
from pydantic import BaseModel, Field


class SlideContent(BaseModel):
    """单张幻灯片的提取内容。"""

    page: int = Field(description="页码，从 1 开始")
    text: str = Field(description="该页的文本内容，含表格、图表数据、公式")
    notes: str = Field(default="", description="备注内容，常包含老师口述的重点")
    images: list[str] = Field(default_factory=list, description="该页提取出的图片文件路径")


class ExtractionResult(BaseModel):
    """PPT 提取结果。"""

    filename: str
    total_pages: int
    slides: list[SlideContent]
    title: str = ""
    image_count: int = Field(default=0, description="提取出的图片总数")
    notes_chars: int = Field(default=0, description="备注总字数")


class GenerateRequest(BaseModel):
    """生成复习文档的请求。"""

    filename: str
    template: str = Field(default="复习指南", description="文档类型: 复习指南/重点总结/习题生成")
    style: str = Field(default="详细", description="输出风格: 精简/详细/表格化")
    use_vision: bool = Field(default=True, description="是否把幻灯片里的图片一并交给视觉模型")
    thinking: bool = Field(
        default=False,
        description=(
            "是否开启思考模式（深度推理）。2026-09-04 核实四家服务商思考默认全开，"
            "关思考省 30-50% 费用且更快，故默认 False；开启时走各家默认思考行为"
        ),
    )
    exam_id: str = Field(
        default="",
        description="往年试题 id（可选）：先 POST /api/exam 上传解析，再在生成时引用",
    )
    exam_mode: str = Field(
        default="direct",
        description="有往年试题时的处理方式: direct=原样搬运进文档 / predict=参考其风格出预测题",
    )
    attach_coverage: bool = Field(
        default=False,
        description="是否在文档末尾附「覆盖率自查」一节。默认 False——总册是拿来复习和"
        "分享的，塞一段自查报告会让同学以为这份文档不完整；需要时自己勾选",
    )
    enable_repair: bool = Field(
        default=False,
        description="覆盖率低于阈值时是否自动补漏（P2 的 verify→repair 闭环）。"
        "默认 False——它要额外调用模型（实测增量成本约为生成的 40% 以内），"
        "且只对「复习指南 / 详细」档且覆盖率 <94% 的文档触发；"
        "对本来就良好的文档零触发零成本",
    )


class ExamParseResponse(BaseModel):
    """往年试题解析上传的响应。"""

    exam_id: str
    filename: str
    chars: int = Field(description="解析出的字符数")
    preview: str = Field(default="", description="前 200 字预览，供前端确认解析正确")
    note: str = Field(default="", description="提示信息（如编码猜测、内容截断）")


class GenerateResponse(BaseModel):
    """生成复习文档的响应。"""

    markdown: str
    success: bool = True
    used_vision: bool = Field(default=False, description="本次是否真的向模型发送了图片")
    image_count: int = Field(default=0, description="本次发送给模型的图片数")
    truncated: bool = Field(
        default=False, description="模型输出是否因超过 max_tokens 被截断（尾部可能缺失）"
    )
    exam_used: bool = Field(default=False, description="本次是否携带了往年试题")
    coverage: dict | None = Field(
        default=None,
        description="覆盖率回检结果（term/missing_detail/pages）；None 表示未算出或"
        "当前档位不适用，前端应按「无数据」处理而不是 0%",
    )
    coverage_note: str = Field(
        default="",
        description="不做覆盖率回检时的一句解释（如档位不是复习指南/详细）——"
        "沉默会让用户以为功能坏了",
    )
    repair: "RepairRecord | None" = Field(
        default=None,
        description="自动补漏记录（触发档位/轮次/每轮覆盖率与花费/是否回滚）。"
        "None 表示未触发或未开启，前端不应显示「已补漏」",
    )


class RepairRound(BaseModel):
    """repair 循环里的一轮（= 一次模型调用 + 它发起的工具调用）。"""

    round: int = Field(description="轮次，从 1 开始")
    tool_calls: int = Field(default=0, description="本轮模型发起的工具调用数")
    patches_applied: int = Field(default=0, description="本轮成功落地的补丁数")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_yuan: float = 0.0
    percent_before: int = -1
    percent_after: int = -1


class RepairRecord(BaseModel):
    """一次 verify→repair 的完整记录。

    这份记录是**给用户看的**（诚实性约束，见 docs/P2-REPAIR.md §6）：
    用户有权知道自己的文档被改过、补了几轮、花了多少钱、从多少提升到多少。
    """

    triggered: bool = False
    mode: str = Field(
        default="",
        description="autonomous=模型自主决定调什么工具 / orchestrated=程序编排"
        "（provider 不支持 Function Calling 时的降级，能力等价但失去自主性）",
    )
    trigger_percent: int = Field(default=-1, description="触发时的覆盖率")
    rounds: list[RepairRound] = Field(default_factory=list)
    total_cost_yuan: float = 0.0
    final_percent: int = Field(default=-1, description="补漏后的覆盖率；-1 表示未算出")
    rolled_back: bool = Field(
        default=False, description="补漏后没有变好，已回退到原文档（用户拿到的仍是原版）"
    )
    stop_reason: str = Field(
        default="",
        description="model_stopped=模型自己认为补完 / max_rounds / cost_limit / stalled / error",
    )
    note: str = Field(default="", description="降级或失败原因；空表示一切正常")
    document: str = Field(
        default="", exclude=True, description="修好的文档正文，只回传调用方，不写进 meta.json"
    )
