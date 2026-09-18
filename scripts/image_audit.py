"""图像审计：统计 PPT 里图片的真实构成，决定 CV 该从哪里介入。

用法：
    python scripts/image_audit.py <file.pptx | 目录> [...]
    python scripts/image_audit.py                 # 不给参数则扫当前目录

可以一次给多个文件或目录（目录会递归找 .pptx）。给 2 份以上会额外输出
一张跨文件汇总表——判断「文字/表格截图为主」这个分布稳不稳，靠的就是它。

回答三个问题：
  1. 跨页重复的图有多少？当前去重只在单页内做（seen 是函数局部变量），
     跨页重复出现的示意图/背景会重复计费。
  2. 哪些是装饰、哪些有信息？当前只用「尺寸 < 150x150」和「灰阶标准差 < 30」
     两道物理启发式，有纹理的装饰图（渐变背景、边框）会漏过去。
  3. 有多少是表格/文字型截图？这类图让多模态模型「看图描述」既贵又不准，
     适合用 CV 做结构化提取（直线检测 + OCR），离线、零 token、更准。

只依赖 Pillow，不新增任何依赖。分类阈值为初始启发式——它的用途是
产出分布数据供判断，不是给出最终结论。
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageFilter  # noqa: E402

from app.services.ppt_extractor import PptExtractionError, extract_pptx  # noqa: E402

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    RESAMPLE = Image.LANCZOS

# 所有特征都在缩略图上算：既快，又不影响统计性质
N = 128
DHASH_SIZE = 8
DUP_DIST = 5
VISION_TOKENS_PER_IMAGE = 384  # 视觉模型单张图的粗略 token 量（用于量级估算）
VISION_PRICE_PER_M = 3.0       # 输入高峰价 ¥/百万，仅用于量级对比


def _pixels(im: Image.Image) -> list:
    """取像素序列，兼容 Pillow 新旧 API（getdata 将于 Pillow 14 移除）。"""
    fn = getattr(im, "get_flattened_data", None)
    return list(fn()) if fn else list(im.getdata())


def dhash(im: Image.Image, size: int = DHASH_SIZE) -> int:
    """差值哈希：抗缩放与轻微压缩，用来找跨页重复的图。"""
    small = im.convert("L").resize((size + 1, size), RESAMPLE)
    px = _pixels(small)
    bits = 0
    for r in range(size):
        base = r * (size + 1)
        for c in range(size):
            bits = (bits << 1) | (1 if px[base + c] > px[base + c + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def features(im: Image.Image) -> dict:
    """抽取用于分级判断的低层视觉特征。"""
    small = im.convert("L").resize((N, N), RESAMPLE)
    px = _pixels(small)

    epx = _pixels(small.filter(ImageFilter.FIND_EDGES))
    edge = sum(1 for v in epx if v > 40) / len(epx)

    binary = [1 if v < 128 else 0 for v in px]

    # 接近全暗的整行 / 整列 —— 表格线与流程框线的信号
    hline = sum(1 for r in range(N) if sum(binary[r * N:(r + 1) * N]) > N * 0.8) / N
    vline = sum(
        1 for c in range(N) if sum(binary[r * N + c] for r in range(N)) > N * 0.8
    ) / N
    # 接近全亮的整行 —— 文字行之间的空隙，行数多说明是文字排版
    rowgap = sum(1 for r in range(N) if sum(binary[r * N:(r + 1) * N]) < N * 0.02) / N

    colors = im.convert("RGB").resize((64, 64), RESAMPLE).getcolors(maxcolors=4096)
    unique = len(colors) if colors else 4096

    return {
        "w": im.size[0],
        "h": im.size[1],
        "unique": unique,
        "edge": edge,
        "hline": hline,
        "vline": vline,
        "rowgap": rowgap,
    }


def classify(f: dict) -> str:
    """粗分类：装饰 / 表格框图 / 文字截图 / 照片 / 示意图。"""
    if f["unique"] < 24 or f["edge"] < 0.02:
        return "装饰"
    if f["hline"] > 0.30 or f["vline"] > 0.30:
        return "表格/框图"
    if f["rowgap"] > 0.25 and f["edge"] > 0.05:
        return "文字截图"
    if f["edge"] > 0.15:
        return "照片"
    return "示意图"


def audit(path: Path) -> dict | None:
    """审计单份课件，打印明细并返回汇总用的统计字典（不可解析时返回 None）。"""
    tmp = Path(tempfile.mkdtemp(prefix="ppt_audit_"))
    try:
        try:
            result = extract_pptx(path, image_dir=tmp)
        except PptExtractionError as exc:
            print(f"\n无法解析 {path.name}：{exc}")
            return None

        entries = []  # (page, file, features, dhash)
        for slide in result.slides:
            for raw in slide.images:
                fp = Path(raw)
                try:
                    with Image.open(fp) as im:
                        im.load()
                        f = features(im)
                        d = dhash(im)
                except Exception:  # noqa: BLE001 单张图分析失败不影响整体
                    continue
                entries.append((slide.page, fp, f, d))

        if not entries:
            print(f"\n文件: {path.name} —— 没有任何通过初筛的图片")
            return None

        print(f"\n文件: {path.name}   共 {result.total_pages} 页")
        print("=" * 92)
        print(
            f"{'页':<5}{'尺寸':<13}{'KB':<7}{'色数':<7}"
            f"{'边缘':<9}{'横线':<9}{'竖线':<8}分类"
        )
        print("-" * 92)
        for page, fp, f, _ in entries:
            kb = fp.stat().st_size / 1024
            print(
                f"{page:<6}{f['w']}x{f['h']:<9}{kb:<8.1f}{f['unique']:<8}"
                f"{f['edge']:<10.3f}{f['hline']:<10.3f}{f['vline']:<9.3f}"
                f"{classify(f)}"
            )

        dup_pairs = []
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                dist = hamming(entries[i][3], entries[j][3])
                if dist <= DUP_DIST:
                    dup_pairs.append((entries[i][0], entries[j][0], dist))

        counts: dict[str, int] = {}
        for _, _, f, _ in entries:
            label = classify(f)
            counts[label] = counts.get(label, 0) + 1

        print("=" * 92)
        print(f"图片总数         : {len(entries)}")
        print("分类分布         : " + " / ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        if dup_pairs:
            print(f"跨页重复         : {len(dup_pairs)} 组（当前只在单页内去重，这些会重复计费）")
            for a, b, dist in dup_pairs[:10]:
                print(f"                   p{a} ↔ p{b}   距离 {dist}")
        else:
            print("跨页重复         : 无")
        tokens = len(entries) * VISION_TOKENS_PER_IMAGE
        print(f"当前会发视觉模型 : {len(entries)} 张，约 {tokens} tokens")
        print(f"                  按输入高峰价 ¥{VISION_PRICE_PER_M:g}/百万 估 "
              f"≈ ¥{tokens * VISION_PRICE_PER_M / 1_000_000:.4f}")

        return {
            "name": path.name,
            "pages": result.total_pages,
            "images": len(entries),
            "counts": counts,
            "dups": len(dup_pairs),
            "tokens": tokens,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def print_summary(rows: list) -> None:
    """多份课件的跨文件汇总——回答「文字/表格截图为主」这个分布稳不稳。"""
    total_pages = sum(r["pages"] for r in rows)
    total_images = sum(r["images"] for r in rows)
    total_dups = sum(r["dups"] for r in rows)
    total_tokens = sum(r["tokens"] for r in rows)

    agg: dict[str, int] = {}
    for r in rows:
        for k, v in r["counts"].items():
            agg[k] = agg.get(k, 0) + v

    n = len(rows)
    print("\n" + "#" * 92)
    print(f"跨文件汇总：{n} 份课件")
    print("#" * 92)
    for r in rows:
        main = max(r["counts"].items(), key=lambda kv: kv[1])[0] if r["counts"] else "-"
        print(f"  {r['name']}   {r['pages']}页 / {r['images']}图 / "
              f"重复{r['dups']}组 / 主要类型: {main}")
    print("-" * 92)
    print(f"合计: {total_pages} 页、{total_images} 张通过初筛的图")
    print("分类分布: " + " / ".join(
        f"{k} {v} ({v * 100 // max(total_images, 1)}%)"
        for k, v in sorted(agg.items(), key=lambda kv: -kv[1])
    ))
    print(f"跨页重复: {total_dups} 组，平均 {total_dups / n:.1f} 组/份"
          "（这些当前会重复计费）")
    print(f"平均每份: {total_images / n:.1f} 张图 / 约 {total_tokens / n:.0f} tokens")
    print(f"全量发视觉模型: 约 {total_tokens} tokens ≈ "
          f"¥{total_tokens * VISION_PRICE_PER_M / 1_000_000:.4f}（输入高峰价，仅供量级对比）")


def resolve_targets(args: list) -> list:
    """把命令行参数（文件/目录混合）展开成去重后的 pptx 列表。"""
    found: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            found += sorted(p.rglob("*.pptx"))
        elif p.suffix.lower() in (".pptx", ".ppt"):
            found.append(p)
        else:
            print(f"跳过（不是目录也不是 pptx）: {a}")

    seen, uniq = set(), []
    for t in found:
        key = t.resolve()
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    args = sys.argv[1:]
    targets = resolve_targets(args) if args else sorted(Path(".").glob("*.pptx"))
    if not targets:
        print("用法: python scripts/image_audit.py <file.pptx | 目录> [...]")
        sys.exit(1)

    print(f"待审计 {len(targets)} 份课件")
    rows = [r for t in targets if (r := audit(t))]
    if len(rows) > 1:
        print_summary(rows)
