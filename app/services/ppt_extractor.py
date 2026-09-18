"""PPT 内容提取服务：解析 .pptx 中的文本、备注、公式、图表与图片。

按成本从低到高分三层提取，越靠前越该优先用满：
  1. 纯解析（零成本）  ：文本框、表格、备注页、OMML 公式、图表数据
  2. 嵌入图片（需视觉模型）：截图、示意图、以图片形式存在的公式
  3. 整页渲染（尚未实现）：扫描版幻灯片，需要 PowerPoint / LibreOffice

注：.ppt 是二进制旧格式，python-pptx 无法解析，已在入口处明确拒绝。
"""
import hashlib
import io
from pathlib import Path

from lxml import etree
from PIL import Image, ImageStat
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.models import ExtractionResult, SlideContent

SUPPORTED_EXTENSIONS = {".pptx"}
MAX_FILE_SIZE_MB = 50

# 小于这个面积的图当作装饰（图标、logo、背景纹理），不值得花 token 去识别
MIN_IMAGE_PIXELS = 150 * 150

# 灰阶标准差低于此值，说明整张图接近纯色（白底占位框、纯色遮罩），没有信息
FLAT_STDDEV = 30.0


class PptExtractionError(Exception):
    """PPT 解析失败时抛出。"""


def _extract_math(shape) -> list[str]:
    """提取 OMML 公式的文字内容。

    PowerPoint 2007 以后的公式以 OMML 存在 XML 里而不是图片，
    python-pptx 不暴露这一层，需要直接读 XML——这样公式不用花钱也能拿到。
    """
    try:
        root = shape._element
    except Exception:  # noqa: BLE001
        return []

    found: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el).localname == "oMath":
            text = "".join(el.itertext()).strip()
            if text:
                found.append(text)
    return found


def _extract_chart_text(shape) -> list[str]:
    """提取图表里的真实数据（分类、系列名、数值）。

    图表不是图片，数据就存在 XML 里，直接读比让视觉模型去猜准得多。
    """
    if not getattr(shape, "has_chart", False):
        return []

    lines: list[str] = []
    try:
        chart = shape.chart
        if chart.has_title and chart.chart_title is not None:
            title = chart.chart_title.text_frame.text.strip()
            if title:
                lines.append(f"[图表] {title}")
        for plot in chart.plots:
            try:
                categories = [str(c) for c in plot.categories]
            except Exception:  # noqa: BLE001  某些图表类型没有分类轴
                categories = []
            for series in plot.series:
                try:
                    name = str(series.name)
                except Exception:  # noqa: BLE001
                    name = "数据系列"
                try:
                    values = [str(v) for v in series.values]
                except Exception:  # noqa: BLE001
                    continue
                if categories:
                    body = "、".join(f"{c}: {v}" for c, v in zip(categories, values))
                else:
                    body = "、".join(values)
                lines.append(f"[数据系列] {name} — {body}")
    except Exception:  # noqa: BLE001
        pass
    return lines


def _extract_text_from_shape(shape) -> str:
    """递归提取单个形状内的文本，含表格、公式与图表数据。"""
    texts: list[str] = []

    if shape.has_text_frame:
        for para in shape.text_frame.paragraphs:
            line = "".join(run.text for run in para.runs).strip()
            if line:
                texts.append(line)

    if shape.has_table:
        for row in shape.table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                texts.append(" | ".join(cells))

    texts.extend(_extract_math(shape))
    texts.extend(_extract_chart_text(shape))

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for sub in shape.shapes:
            texts.append(_extract_text_from_shape(sub))

    return "\n".join(t for t in texts if t)


def _extract_notes(slide) -> str:
    """提取备注页内容。

    老师常把真正的考点写在备注里（「这题必考」「去年考过」），
    这是整个提取器里投入产出比最高的一处。
    """
    try:
        if not slide.has_notes_slide:
            return ""
        frame = slide.notes_slide.notes_text_frame
        return (frame.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _iter_pictures(shapes):
    """遍历形状树里的所有图片，含组合内的。"""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            yield shape
        elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_pictures(shape.shapes)


def _to_vision_png(blob: bytes) -> bytes | None:
    """把嵌入图片统一转成 PNG；无信息量的图返回 None。

    过滤三类不值得花 token 的图：
      - 过小的（图标、logo、背景纹理）
      - 接近纯色的（白底占位框、纯色遮罩）
      - 无法渲染的（公式编辑器的 WMF/EMF 在 Windows 上靠 GDI 解码，失败就跳过）
    """
    try:
        with Image.open(io.BytesIO(blob)) as im:
            im.load()

            if im.width * im.height < MIN_IMAGE_PIXELS:
                return None

            # 接近纯色的图（白底占位框、纯色遮罩）没有任何信息，不值得花 token。
            # 用标准差而不是极值差：单根细边线会让极值差很大但标准差仍然很低。
            stat = ImageStat.Stat(im.convert("L"))
            if stat.stddev[0] < FLAT_STDDEV:
                return None

            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def extract_slide_images(slide, page: int, out_dir: Path) -> list[str]:
    """把某一页里的嵌入图片存成 PNG，返回文件路径列表。同一张图只存一次。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[str] = []
    seen: set[str] = set()

    for shape in _iter_pictures(slide.shapes):
        try:
            blob = shape.image.blob
        except Exception:  # noqa: BLE001  链接到外部的图片没有 blob
            continue

        digest = hashlib.md5(blob).hexdigest()[:12]
        if digest in seen:
            continue
        seen.add(digest)

        png = _to_vision_png(blob)
        if png is None:
            continue

        dest = out_dir / f"p{page:03d}_{digest}.png"
        dest.write_bytes(png)
        paths.append(str(dest))

    return paths


def extract_pptx(path: str | Path, image_dir: str | Path | None = None) -> ExtractionResult:
    """从 .pptx 中提取全部文本内容，可选同时导出嵌入图片。

    image_dir 为 None 时不导出图片（纯文本提取，最快）。
    """
    path = Path(path)
    if not path.exists():
        raise PptExtractionError(f"文件不存在: {path}")

    suffix = path.suffix.lower()
    if suffix == ".ppt":
        raise PptExtractionError(
            "不支持 .ppt 旧格式：请用 PowerPoint 打开后「文件 → 另存为 → .pptx」再上传"
        )
    if suffix not in SUPPORTED_EXTENSIONS:
        raise PptExtractionError(f"不支持的文件格式: {path.suffix}")

    try:
        prs = Presentation(path)
    except Exception as exc:  # noqa: BLE001
        raise PptExtractionError(f"PPT 解析失败（文件可能已损坏）: {exc}") from exc

    if image_dir is not None:
        image_dir = Path(image_dir)
        image_dir.mkdir(parents=True, exist_ok=True)

    slides: list[SlideContent] = []
    notes_total = 0
    image_total = 0

    for idx, slide in enumerate(prs.slides, start=1):
        parts = [t for t in (_extract_text_from_shape(s) for s in slide.shapes) if t]
        notes = _extract_notes(slide)
        notes_total += len(notes)
        images = extract_slide_images(slide, idx, image_dir) if image_dir is not None else []
        image_total += len(images)

        slides.append(
            SlideContent(page=idx, text="\n".join(parts), notes=notes, images=images)
        )

    title = ""
    for slide in slides:
        first_line = next((line for line in slide.text.splitlines() if line.strip()), "")
        if first_line:
            title = first_line[:50]
            break

    return ExtractionResult(
        filename=path.name,
        total_pages=len(slides),
        slides=slides,
        title=title,
        image_count=image_total,
        notes_chars=notes_total,
    )


def format_for_ai(result: ExtractionResult, max_chars: int = 120_000) -> str:
    """把提取结果组装成适合发给 AI 的文本块，备注会单独标注。"""
    chunks: list[str] = []
    used = 0

    for slide in result.slides:
        block = f"--- 第 {slide.page} 页 ---"
        if slide.text.strip():
            block += f"\n{slide.text}"
        else:
            block += "\n（本页无可提取文字"
            block += "，内容见附图）" if slide.images else "，且无嵌入图片）"
        if slide.notes:
            block += f"\n【教师备注·重点】{slide.notes}"

        used += len(block) + 1
        if used > max_chars:
            break
        chunks.append(block)

    header = f"《{result.title or result.filename}》共 {result.total_pages} 页"
    if result.image_count:
        header += f"，另附 {result.image_count} 张图"
    return header + ":\n\n" + "\n".join(chunks)
