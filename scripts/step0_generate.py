"""Step 0 验证：用真实课件跑一次完整「提取 → 视觉读图 → 生成复习文档」。

输出 Markdown 到 eval/，并在终端报告统计。用法：
    uv run python scripts/step0_generate.py <课件.pptx> [模板] [风格]
默认模板=复习指南，风格=详细，开启视觉。
"""
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import GenerateRequest  # noqa: E402
from app.services.ai_generator import generate_review_document  # noqa: E402
from app.services.ppt_extractor import PptExtractionError, extract_pptx  # noqa: E402


def main() -> None:
    src = Path(sys.argv[1])
    template = sys.argv[2] if len(sys.argv) > 2 else "复习指南"
    style = sys.argv[3] if len(sys.argv) > 3 else "详细"

    tmp = Path(tempfile.mkdtemp(prefix="ppt_step0_"))
    print(f"[1/3] 提取 {src.name} ...")
    try:
        result = extract_pptx(src, image_dir=tmp)
    except PptExtractionError as exc:
        print(f"无法解析：{exc}")
        sys.exit(1)

    print(f"      共 {result.total_pages} 页 | 文字合计约 {sum(len(s.text) for s in result.slides)} 字符"
          f" | 备注 {result.notes_chars} 字符 | 原始图片 {result.image_count} 张")

    req = GenerateRequest(
        filename=src.name, template=template, style=style, use_vision=True
    )
    print(f"[2/3] 调用模型（模板={template}，风格={style}，use_vision=True）...")
    t0 = time.time()
    try:
        markdown, sent, truncated = generate_review_document(result, req)
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {type(exc).__name__}: {exc}")
        sys.exit(1)
    elapsed = time.time() - t0

    out_dir = Path("eval")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = out_dir / f"step0_{src.stem}_{template}.md"
    out.write_text(markdown, encoding="utf-8")

    print(f"[3/3] 完成。实际发送图片 {sent} 张 | 耗时 {elapsed:.0f}s | 输出 {len(markdown)} 字符")
    print(f"      已保存：{out}")
    print("-" * 60)
    print(markdown[:400])
    print("...")


if __name__ == "__main__":
    main()
