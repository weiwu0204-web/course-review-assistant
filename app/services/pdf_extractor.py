"""PDF 课件提取：从文字版 PDF 中提取文本。

⚠️ 关键前提：「文字版 PDF」不等于「能提取出文字」。

    某些生成方式（Mac 打印成 PDF / pdftopdf 过滤器）会把字体的 ToUnicode
    映射表写坏——字形轮廓在、肉眼能看见、阅读器能正常显示，但 CID 无法还原成
    Unicode。实测 6 份 PDF：4 份中文课件的乱码字符占比 17.9%–42.1%（提取出来
    是 ºÜÔć 这种字符），而健康的中文 PDF（简历）和英文 PDF（LLM 课件）都是 0.0%。

    这类文件对程序而言等价于扫描版，只能走 OCR / 视觉模型。所以提取之后
    必须做「文本健康检测」并抛错——绝不能把乱码喂给模型，否则会生成一份
    看起来完整、内容全是噪声的复习文档，比直接失败更糟。

依赖：pypdf（已在 requirements，无需新增）。
"""
import logging
import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from app.models import ExtractionResult, SlideContent
from app.services.ppt_extractor import PptExtractionError

SUPPORTED_EXTENSIONS = {".pdf"}

# 全文字符数低于此值不做判断（封面、纯公式页等本来就没多少字）
MIN_TEXT_FOR_CHECK = 200
# 乱码字符占比高于此值 → 文字层损坏。实测健康 PDF 为 0.0%，
# 损坏 PDF 为 17.9%–42.1%，两者完全不重叠，故阈值取 5% 足够安全。
JUNK_RATIO = 0.05

#: 乱码特征字符：C1 控制区 + Latin-1 补充 + Latin Extended-A/B。
#: 正常的中文或英文文本几乎不出现这些码位。
_JUNK = re.compile(r"[\u0080-\u02ff]")

# 跨页重复行过滤：短行在多数页面反复出现，基本是水印 / 页眉页脚 / 章节指示器
REPEAT_MIN_PAGES = 3       # 少于这么多页则不做过滤（避免误删真实标题）
REPEAT_RATIO = 0.6         # 出现在 60% 以上页面才算重复
REPEAT_MAX_LEN = 40        # 只过滤短行；长段落重复更可能是真实内容


class PdfExtractionError(PptExtractionError):
    """PDF 解析失败，或文字层不可用（继承 PPT 异常，上层统一捕获即可）。"""


def assess_text_health(text: str) -> tuple[bool, str]:
    """判断提取出的文本是否可用。

    返回 (是否可用, 原因)。原因仅在不可用时非空，用于给用户可操作的提示。
    """
    stripped = "".join(text.split())
    n = len(stripped)
    if n < MIN_TEXT_FOR_CHECK:
        return True, ""  # 内容太少，不足以判断，交给上层的「无内容」检查处理

    if "(cid:" in text:
        return False, "文字层没有映射表（提取结果是字体内部编号，不是文字）"

    if len(_JUNK.findall(stripped)) / n > JUNK_RATIO:
        return False, "文字层已损坏（中文字符无法还原成可读文本）"

    # 刻意不用「中文占比」作判据：英文课件本来就没有中文（实测 LLM.pdf
    # 中文仅 0.6%，但内容完全正常），那样会把正常文件误杀。
    # 乱码字符占比才是文字层损坏的可靠信号。
    return True, ""


def _strip_repeated_lines(pages: list[str]) -> list[str]:
    """去掉跨页重复出现的短行（水印、页眉页脚、章节指示器）。

    PDF 没有 PPT 的「形状」概念，水印会实打实混进文本里。实测某些课件的
    水印行每页重复 3 次，130 页的课件光水印就占数千字符的 token。
    """
    if len(pages) < REPEAT_MIN_PAGES:
        return pages

    counter: Counter[str] = Counter()
    for text in pages:
        for line in {ln.strip() for ln in text.splitlines() if ln.strip()}:
            counter[line] += 1

    threshold = max(REPEAT_MIN_PAGES, int(len(pages) * REPEAT_RATIO))
    noise = {
        line for line, count in counter.items()
        if count >= threshold and len(line) <= REPEAT_MAX_LEN
    }
    if not noise:
        return pages

    cleaned = []
    for text in pages:
        kept = [ln for ln in text.splitlines() if ln.strip() not in noise]
        cleaned.append("\n".join(kept))
    return cleaned


def extract_pdf(
    path: str | Path, image_dir: str | Path | None = None
) -> ExtractionResult:
    """从文字版 PDF 提取文本。

    返回结构与 extract_pptx 完全一致，因此下游（提示词组装、AI 生成、
    批量总册）无需任何改动。

    image_dir 为接口兼容而保留：PDF 的图片提取需要整页渲染，属于 OCR 路线
    （P1）的范围，当前版本不导出图片。
    """
    path = Path(path)
    if not path.exists():
        raise PdfExtractionError(f"文件不存在: {path}")
    if path.suffix.lower() != ".pdf":
        raise PdfExtractionError(f"不支持的文件格式: {path.suffix}")

    # pypdf 对交叉引用表受损的 PDF 会打印大量 "Ignoring wrong pointing object"，
    # 批量处理时刷屏。临时抬高它的日志级别（子日志器未设级别，会继承本级）。
    py_logger = logging.getLogger("pypdf")
    prev_level = py_logger.level
    py_logger.setLevel(logging.ERROR)
    try:
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                # 仅限制权限（如禁用编辑）的 PDF 用空口令即可解开
                try:
                    reader.decrypt("")
                except Exception as exc:  # noqa: BLE001
                    raise PdfExtractionError(
                        "这份 PDF 有密码保护，无法解析：请先解除密码再上传"
                    ) from exc

            pages_text: list[str] = []
            for page in reader.pages:
                try:
                    pages_text.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 单页损坏不影响整份
                    pages_text.append("")
        except PdfExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PdfExtractionError(f"PDF 解析失败（文件可能已损坏）: {exc}") from exc
    finally:
        py_logger.setLevel(prev_level)

    if not pages_text:
        raise PdfExtractionError("这份 PDF 没有任何页面")

    ok, reason = assess_text_health("\n".join(pages_text))
    if not ok:
        raise PdfExtractionError(
            f"{reason}。这类 PDF 多是「打印 / 导出」生成的，字形能显示但无法被程序读取，"
            "需要走 OCR 识别。可以先自己验证：在阅读器里试着复制一段中文，"
            "若粘贴出来是乱码或复制不出来，即可确认。"
        )

    pages_text = _strip_repeated_lines(pages_text)

    slides = [
        SlideContent(page=idx, text=text, notes="", images=[])
        for idx, text in enumerate(pages_text, start=1)
    ]

    title = ""
    for slide in slides:
        first_line = next(
            (ln.strip() for ln in slide.text.splitlines() if ln.strip()), ""
        )
        if first_line:
            title = first_line[:50]
            break
    if not title:
        title = path.stem

    return ExtractionResult(
        filename=path.name,
        total_pages=len(slides),
        slides=slides,
        title=title,
        image_count=0,
        notes_chars=0,  # PDF 没有备注页这个概念
    )
