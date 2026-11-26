"""Service-only generation settings; reuse the desktop parser and document generator."""

import logging
from pathlib import Path

from pypdf import PdfReader

from app.config import Settings
from app.infrastructure.service_config import ServiceSettings
from app.models import GenerateRequest
from app.services.ai_generator import EXAM_MAX_CHARS, run_single
from app.services.extractor import extract_any
from app.worker.runner import Generated

logger = logging.getLogger(__name__)


def read_exam(path: Path | None) -> str:
    if path is None:
        return ""
    if path.suffix == ".pdf":
        parts, length = [], 0
        for page in PdfReader(path).pages:
            text = page.extract_text() or ""
            parts.append(text)
            length += len(text)
            if length >= EXAM_MAX_CHARS:
                break
        result = "\n".join(parts)
    else:
        raw = path.read_bytes()
        for charset in ("utf-8-sig", "gb18030"):
            try:
                result = raw.decode(charset)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("Unsupported exam text encoding")
    if not result.strip():
        raise ValueError("No extractable exam text")
    return result[:EXAM_MAX_CHARS]


def make_generator(config: ServiceSettings):
    if config.generator == "demo":
        def demo(source, exam, options, workdir):
            result = extract_any(source, image_dir=workdir / "images")
            exam_note = "有" if read_exam(exam) else "无"
            return Generated(
                f"# 预览章节\n\n已提取 {result.total_pages} 页课件；试题：{exam_note}。"
                "\n\n这是无模型调用的部署演示。", pages=result.total_pages,
            )
        return demo

    if not config.model_api_key or not config.model_name:
        raise ValueError("Model generation requires PPT_SERVICE_MODEL_API_KEY and PPT_SERVICE_MODEL_NAME")
    settings = Settings(
        _env_file=None, ai_api_key=config.model_api_key.get_secret_value(), ai_base_url=config.model_base_url,
        ai_model=config.model_name, ai_vision_model=config.model_vision_name, ai_thinking_off={},
        ai_max_tokens=config.model_max_tokens, ai_timeout_seconds=config.model_timeout, ai_max_retries=1,
    )

    def generate(source, exam, options, workdir):
        parsed = extract_any(source, image_dir=workdir / "images")
        request = GenerateRequest(filename=source.name, **options)
        outcome = run_single(parsed, request, read_exam(exam), settings_override=settings)
        return Generated(outcome.markdown, pages=parsed.total_pages, truncated=outcome.truncated)

    return generate
