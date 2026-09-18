"""统一提取入口：按文件后缀分发到对应的提取器。

下游（提示词组装、AI 生成、批量总册）只消费 ExtractionResult，不关心
来源格式——所以新增一种格式，只需要在这里加一条分支。

异常约定：所有提取器抛出的异常都继承 PptExtractionError，上层统一捕获
这一个类型即可（PdfExtractionError 是它的子类）。
"""
from pathlib import Path

from app.models import ExtractionResult
from app.services.pdf_extractor import PdfExtractionError, extract_pdf
from app.services.ppt_extractor import PptExtractionError, extract_pptx

#: 支持的课件格式。上传校验、前端 accept、脚本发现文件都以此为准。
SUPPORTED_EXTENSIONS = {".pptx", ".pdf"}

#: 统一异常别名 —— 新代码用它，语义比 PptExtractionError 更准确
ExtractionError = PptExtractionError

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "ExtractionError",
    "PdfExtractionError",
    "PptExtractionError",
    "extract_any",
    "extract_pdf",
    "extract_pptx",
]


def extract_any(
    path: str | Path, image_dir: str | Path | None = None
) -> ExtractionResult:
    """按后缀分发提取：.pptx → python-pptx；.pdf → pypdf。"""
    suffix = Path(path).suffix.lower()

    if suffix == ".pptx":
        return extract_pptx(path, image_dir=image_dir)
    if suffix == ".pdf":
        return extract_pdf(path, image_dir=image_dir)
    if suffix == ".ppt":
        raise PptExtractionError(
            "不支持 .ppt 旧格式：请用 PowerPoint 打开后「文件 → 另存为 → .pptx」再上传"
        )
    raise PptExtractionError(
        f"不支持的文件格式: {suffix}（当前支持 .pptx / .pdf）"
    )
