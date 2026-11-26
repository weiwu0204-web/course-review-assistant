"""API 路由：文件上传、内容提取、复习文档生成。"""
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile

from app.config import get_settings
from app.models import (
    ExamParseResponse,
    ExtractionResult,
    GenerateRequest,
    GenerateResponse,
)
from app.services.ai_generator import run_single
from app.services.errors import friendly_ai_error as _friendly_ai_error
from app.services.coverage import render_report_section
from app.services.extractor import (
    SUPPORTED_EXTENSIONS,
    PptExtractionError,
    extract_any,
)

router = APIRouter()

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
EXAM_DIR = UPLOAD_DIR / "exams"

EXAM_EXTS = {".txt", ".md", ".markdown", ".pdf"}

PREVIEW_CHARS = 200
EXAM_TEXT_MAX = 200_000  # 拒绝明显异常的超长粘贴，保护后续请求


def _cleanup_stale_uploads() -> None:
    """清理超过保留期（upload_ttl_hours）的旧上传，防止 uploads/ 长期积压。

    桌面软件反复使用会不断累积上传副本；文档生成完成后旧文件已无引用价值。
    每次上传时顺带清扫（低成本），只删 mtime 超过 TTL 的条目：
      - UPLOAD_DIR 下的课件目录（exams/ 除外，目录 mtime 随上传写入更新）
      - EXAM_DIR 下的试题文件
    刚上传的文件 mtime 为当前时间，绝不可能被本次清扫误删。
    """
    try:
        _cleanup_stale_uploads_inner()
    except (OSError, SystemExit):
        # 清理是**纯副作用**：磁盘满、权限不足、文件被占用、沙箱拒绝删除
        # （本机 shim 就把"拒绝删除"实现成 SystemExit，而它不是 Exception 子类）
        # 都可能抛——任何一种都不该让上传 / 建库这条主流程失败。
        pass


def _cleanup_stale_uploads_inner() -> None:
    settings = get_settings()
    ttl_seconds = timedelta(hours=settings.upload_ttl_hours).total_seconds()
    now = time.time()

    if UPLOAD_DIR.exists():
        for entry in UPLOAD_DIR.iterdir():
            if entry.name == "exams" or entry.name.startswith("."):
                continue
            try:
                stale = now - entry.stat().st_mtime > ttl_seconds
            except OSError:  # noqa: BLE001 文件被占用/正在写入等瞬时错误，跳过不误删
                continue
            if stale:
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                except (OSError, SystemExit):
                    # ⚠️ **SystemExit 也必须接**：某些安全/沙箱环境把"拒绝删除"
                    # 实现成 SystemExit 而不是异常（本机的 shim 就是），
                    # 而它不是 Exception 的子类 —— 只写 `except OSError` 会漏掉，
                    # 表现是**上传/建库直接 500**（2026-10-17 真跑抓到）。
                    # 清理是**纯副作用**：磁盘满、权限不足、文件被占用都会触发同类失败，
                    # 任何一种都不该拖垮主流程。
                    continue

    if EXAM_DIR.exists():
        for f in EXAM_DIR.glob("*.txt"):
            try:
                stale = now - f.stat().st_mtime > ttl_seconds
            except OSError:  # noqa: BLE001
                continue
            if stale:
                try:
                    f.unlink(missing_ok=True)
                except (OSError, SystemExit):
                    # 同上：清理失败是纯副作用，不该让主流程失败
                    continue


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/extract", response_model=ExtractionResult)
async def extract(file: UploadFile, with_images: bool = True) -> ExtractionResult:
    """上传课件（.pptx / .pdf）并提取文本内容；PPT 默认同时导出嵌入图片。"""
    settings = get_settings()

    original = Path(file.filename or "unknown.pptx").name
    suffix = Path(original).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        if suffix == ".ppt":
            raise HTTPException(
                status_code=400,
                detail="不支持 .ppt 旧格式：请用 PowerPoint 打开后「文件 → 另存为 → .pptx」再上传",
            )
        raise HTTPException(
            status_code=400,
            detail="只支持 .pptx / .pdf 课件（旧版 .ppt 请先另存为 .pptx）",
        )

    content = await file.read()
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {settings.max_upload_size_mb}MB 限制",
        )

    # 上传新文件前先清掉超期旧文件（只增不减会让 uploads/ 长期积压）
    _cleanup_stale_uploads()

    # 每次上传单独一个目录，避免同名文件互相覆盖
    upload_id = uuid.uuid4().hex[:8]
    dest_dir = UPLOAD_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / original
    dest.write_bytes(content)

    image_dir = dest_dir / "images" if with_images else None

    try:
        result = extract_any(dest, image_dir=image_dir)
    except PptExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # filename 带上 upload_id，/generate 才能找回文件；前端显示时取最后一段即可
    result.filename = f"{upload_id}/{original}"
    return result


@router.post("/exam", response_model=ExamParseResponse)
async def upload_exam(
    file: UploadFile | None = None,
    text: str | None = Form(default=None),
) -> ExamParseResponse:
    """解析往年试题并暂存，返回 exam_id 供 /generate 引用。

    两种输入方式（file 优先）：
      - file: .txt / .md / .pdf（文字版 PDF；扫描版 PDF 无文本层，无法解析）
      - text: 直接把题目文本粘贴进表单字段
    """
    if file is None and text is None:
        raise HTTPException(status_code=400, detail="请上传文件或粘贴题目文本")

    source_name = ""
    content = b""

    if file is not None:
        source_name = Path(file.filename or "exam.txt").name
        suffix = Path(source_name).suffix.lower()
        if suffix not in EXAM_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持 {suffix} 格式的往年试题，支持：.txt / .md / .pdf",
            )
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="上传的文件是空的")

        if suffix == ".pdf":
            parsed = _parse_pdf(content)
            if not parsed.strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "这份 PDF 提取不到任何文字（可能是扫描版/图片版）。"
                        "请改用文字版 PDF，或把题目复制粘贴到文本框。"
                    ),
                )
            raw_text = parsed
        else:
            raw_text = _decode_text(content)
    else:
        source_name = "粘贴文本.txt"
        raw_text = text or ""

    raw_text = raw_text.strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="解析后没有内容，请检查输入")

    if len(raw_text) > EXAM_TEXT_MAX:
        raise HTTPException(status_code=400, detail="题目文本过长，请分批上传")

    # 与课件上传同理：先清扫超期旧文件
    _cleanup_stale_uploads()

    exam_id = uuid.uuid4().hex[:8]
    EXAM_DIR.mkdir(parents=True, exist_ok=True)
    (EXAM_DIR / f"{exam_id}.txt").write_text(raw_text, encoding="utf-8")

    return ExamParseResponse(
        exam_id=exam_id,
        filename=source_name,
        chars=len(raw_text),
        preview=raw_text[:PREVIEW_CHARS],
        note="PDF 无文本层时可能提取为空" if source_name.lower().endswith(".pdf") else "",
    )


def _decode_text(content: bytes) -> str:
    """按 UTF-8 优先、GBK 兜底的顺序解码文本文件。"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_pdf(content: bytes) -> str:
    """用 pypdf 提取 PDF 文本；扫描版/图片版 PDF 会返回空串。"""
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 单页损坏不致命
            pages.append("")
    return "\n".join(pages)


@router.post("/generate", response_model=GenerateResponse)
def generate(request: GenerateRequest) -> GenerateResponse:
    """基于已上传的课件生成复习文档；有图片时自动切到视觉模型。

    可选携带往年试题（exam_id），作为文档里题目的最高优先级来源。
    """
    src_file = UPLOAD_DIR / request.filename
    if not src_file.exists():
        raise HTTPException(status_code=404, detail="未找到该课件，请重新上传")

    image_dir = src_file.parent / "images"
    result = extract_any(src_file, image_dir=image_dir)

    has_content = any(s.text.strip() or s.notes.strip() for s in result.slides)
    if not has_content and result.image_count == 0:
        raise HTTPException(
            status_code=400,
            detail="这份课件里没有提取到任何文字和图片，无法生成复习文档",
        )

    exam_text = ""
    if request.exam_id:
        exam_file = EXAM_DIR / f"{request.exam_id}.txt"
        if not exam_file.exists():
            raise HTTPException(status_code=404, detail="未找到该往年试题，请重新上传")
        exam_text = exam_file.read_text(encoding="utf-8")

    try:
        # 与批任务共用 run_single（单一实现，避免两套逻辑漂移），
        # 并且只有它带得回覆盖率回检结果。
        outcome = run_single(result, request, exam_text)
    except RuntimeError as exc:
        # RuntimeError 由 ai_generator 主动抛出（如未配置 key），文案已对用户友好
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=_friendly_ai_error(exc)) from exc

    # 「覆盖率自查」是否附进文档：默认关（总册/文档是拿来复习与分享的，
    # 自查报告会让同学以为这份不完整）。勾了才附，且只附在返回给用户的正文里，
    # 不改动 outcome.markdown 本身。
    markdown = outcome.markdown
    if request.attach_coverage and outcome.coverage is not None:
        markdown += render_report_section([{
            "name": Path(request.filename).name,
            "percent": outcome.coverage.percent,
            "reliable": outcome.coverage.reliable,
            "missing": outcome.coverage.missing_detail,
        }])

    return GenerateResponse(
        markdown=markdown,
        used_vision=outcome.used_vision,
        image_count=outcome.image_count,
        truncated=outcome.truncated,
        exam_used=outcome.exam_used,
        coverage=outcome.coverage_dict(),
        coverage_note=outcome.coverage_note,
    )
