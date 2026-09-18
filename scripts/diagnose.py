"""诊断工具：检查一份 PPT 实际能被提取到什么程度。

用法：python scripts/diagnose.py <file.pptx>

逐页报告文字 / 备注 / 图片的提取情况，并估算启用视觉模型的成本。
先跑这个，再决定要不要为了这份 PPT 调用视觉模型。
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai_generator import collect_images  # noqa: E402
from app.services.ppt_extractor import PptExtractionError, extract_pptx  # noqa: E402


def diagnose(path: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ppt_diag_"))
    try:
        try:
            result = extract_pptx(path, image_dir=tmp)
        except PptExtractionError as exc:
            print(f"\n无法解析：{exc}")
            return

        print(f"\n文件: {path.name}   共 {result.total_pages} 页")
        print("-" * 72)
        print(f"{'页':<6}{'文字':<9}{'备注':<9}{'图片':<8}状态")
        print("-" * 72)

        blank_pages: list[int] = []
        image_only_pages: list[int] = []

        for slide in result.slides:
            chars = len(slide.text.strip())
            notes = len(slide.notes)
            imgs = len(slide.images)

            if chars == 0 and imgs == 0:
                blank_pages.append(slide.page)
                status = "完全空页"
            elif chars == 0:
                image_only_pages.append(slide.page)
                status = "只能靠图片（需视觉模型）"
            else:
                status = "正常"
                if notes:
                    status += " · 含备注"

            print(f"{slide.page:<7}{chars:<10}{notes:<10}{imgs:<9}{status}")

        print("-" * 72)
        print(f"备注总字数  : {result.notes_chars}")
        print(f"可提取图片  : {result.image_count} 张")
        print(f"完全空页    : {blank_pages or '无'}")
        print(f"只靠图片的页: {image_only_pages or '无'}")

        sendable, send_pages, skipped = collect_images(result, True)
        if sendable:
            tokens = len(sendable) * 384
            print(
                f"\n实际会发给视觉模型: {len(sendable)} 张"
                f"（已过滤 {skipped} 张重复出现的装饰图）"
                f"\n图片部分约 {tokens} tokens，按输入高峰价 ¥3/百万 估 ≈ ¥{tokens * 3 / 1_000_000:.4f}"
                f"\n（价格随 V4 模型随时段浮动，空闲时段约半价）"
                f"\n涉及页码: {send_pages}"
            )
        else:
            print("\n没有值得发送给视觉模型的图片，用纯文本模型即可，不必开视觉。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _pptx_utils import resolve_pptx  # noqa: E402

    path = resolve_pptx("python scripts/diagnose.py <file.pptx>")
    diagnose(path)
