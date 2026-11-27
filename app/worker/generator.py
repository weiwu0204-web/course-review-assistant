"""Service-only generation settings; reuse the desktop parser and document generator."""

from pathlib import Path

from pypdf import PdfReader

from app.infrastructure.service_config import ServiceSettings
from app.models import GenerateRequest
from app.services.ai_generator import EXAM_MAX_CHARS, run_single
from app.services.coverage import applies_to, measure
from app.services.extractor import extract_any
from app.worker.runner import Generated

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
            markdown = (
                f"# 预览章节\n\n已提取 {result.total_pages} 页课件；试题：{exam_note}。"
                "\n\n这是无模型调用的部署演示。"
            )
            coverage = measure(result, markdown).to_dict() if applies_to(options["template"], options["style"]) else None
            return Generated(
                markdown, pages=result.total_pages, coverage=coverage,
            )
        return demo

    settings = config.model_settings()
    if settings is None:
        raise ValueError("Model generation requires PPT_SERVICE_MODEL_API_KEY and PPT_SERVICE_MODEL_NAME")
    prices = config.repair_pricing()

    def generate(source, exam, options, workdir):
        parsed = extract_any(source, image_dir=workdir / "images")
        if options.get("enable_repair") and prices is None:
            raise ValueError("Auto repair requires service model unit prices")
        request = GenerateRequest(filename=source.name, **options)
        outcome = run_single(parsed, request, read_exam(exam), repair_workdir=workdir / "repair",
                             settings_override=settings, pricing_override=prices)
        repair = outcome.repair.model_dump() if outcome.repair else None
        if repair:
            # Error notes from a provider may contain request detail.
            repair.pop("note", None)
        return Generated(outcome.markdown, pages=parsed.total_pages, truncated=outcome.truncated,
                         coverage=outcome.coverage_dict(), repair=repair)

    return generate
