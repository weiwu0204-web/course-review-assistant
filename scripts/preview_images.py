"""把 PPT 里的嵌入图片拼成一张联系表，用来肉眼判断哪些值得交给视觉模型。

用法：python scripts/preview_images.py <file.pptx> [--out 输出.png]

page 列标在每张图下方；重复出现在多页的图通常是校徽/图标，已在终端里标出来。
"""
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai_generator import collect_images  # noqa: E402
from app.services.ppt_extractor import PptExtractionError, extract_pptx  # noqa: E402

CELL = 220
COLS = 7
PAD = 10


def build_sheet(entries: list[tuple[int, Path]], out_path: Path) -> None:
    rows = (len(entries) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * CELL, rows * (CELL + 20)), "white")
    draw = ImageDraw.Draw(sheet)

    for i, (page, path) in enumerate(entries):
        row, col = divmod(i, COLS)
        x, y = col * CELL, row * (CELL + 20)
        try:
            with Image.open(path) as raw:
                raw.load()
                im = raw.convert("RGB")
                im.thumbnail((CELL - 2 * PAD, CELL - 2 * PAD))
                w, h = im.size
                sheet.paste(im, (x + PAD, y + PAD))
        except Exception:  # noqa: BLE001
            w = h = 0
        draw.text((x + PAD, y + CELL + 2), f"p{page}  {w}x{h}", fill="#333333")

    sheet.save(out_path)


def main(path: Path, out_path: Path, sendable_only: bool = True) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ppt_prev_"))
    try:
        try:
            result = extract_pptx(path, image_dir=tmp)
        except PptExtractionError as exc:
            print(f"无法解析：{exc}")
            return

        entries = [(s.page, Path(p)) for s in result.slides for p in s.images]

        if sendable_only:
            keep = set(collect_images(result, True)[0])
            total = len(entries)
            entries = [(pg, p) for pg, p in entries if str(p) in keep]
            print(f"\n只看真正会发出的图：{len(entries)} / {total} 张")
            print("（想看全部加 --all）")

        # 同一张图出现在多页 → 大概率是母版 logo / 图标
        by_digest: dict[str, set[int]] = defaultdict(set)
        sizes: dict[str, tuple[int, int, int]] = {}
        for page, fp in entries:
            digest = fp.stem.split("_")[-1]
            by_digest[digest].add(page)
            try:
                with Image.open(fp) as im:
                    sizes[digest] = (*im.size, fp.stat().st_size)
            except Exception:  # noqa: BLE001
                sizes[digest] = (0, 0, 0)

        print(f"\n共 {len(entries)} 张图，去重后 {len(by_digest)} 张")
        print("-" * 60)
        repeated = {d: ps for d, ps in by_digest.items() if len(ps) >= 3}
        if repeated:
            print("重复出现 ≥3 页的图（几乎可以确定是装饰）:")
            for d, pages in sorted(repeated.items(), key=lambda kv: -len(kv[1])):
                w, h, size = sizes[d]
                print(f"  {w}x{h}  {size/1024:6.1f} KB  出现在 {len(pages)} 页: {sorted(pages)}")
        else:
            print("没有重复出现 3 次以上的图")
        print("-" * 60)

        if entries:
            build_sheet(entries, out_path)
            print(f"联系表已生成: {out_path}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _pptx_utils import resolve_pptx  # noqa: E402

    src = resolve_pptx("python scripts/preview_images.py <file.pptx> [--out 输出.png]")
    out = Path("uploads/preview.png")
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    main(src, out, sendable_only="--all" not in sys.argv)
