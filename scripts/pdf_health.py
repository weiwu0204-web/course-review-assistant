"""批量检查 PDF 课件的文字层健康度。

## 为什么需要这个

`pdf_extractor` 的模块文档里记着一个反直觉的事实：**「文字版 PDF」不等于「能提取出文字」**。
某些生成方式（Mac 打印成 PDF / pdftopdf）会把字体的 ToUnicode 映射表写坏——
字形轮廓在、肉眼能看见、阅读器正常显示，但程序读出来是 `ºÜÔć` 这样的乱码。
实测 6 份 PDF 里 4 份中文课件中招（乱码占比 17.9%–42.1%）。

**一次只测一份**时 `extract_pdf` 会直接抛错，够用；但拿到一批新课件时
（比如新的一门课几十份 PDF），需要先知道**整体可用率**再决定怎么处理：
是全部可用、要挑着用、还是得先走 OCR/整页渲染那条路。

## 用法

    .venv/Scripts/python.exe scripts/pdf_health.py 计组/
    .venv/Scripts/python.exe scripts/pdf_health.py 计组/ --detail

`--detail` 会逐份打印，否则只在有问题的文件上展开。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.pdf_extractor import PdfExtractionError, extract_pdf  # noqa: E402


def check(folder: Path, detail: bool = False) -> int:
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"目录下没有 PDF：{folder}")
        return 2

    ok: list[tuple[str, int, int]] = []
    bad: list[tuple[str, str]] = []

    for path in pdfs:
        try:
            result = extract_pdf(path)
            chars = sum(len(s.text) for s in result.slides)
            ok.append((path.name, result.total_pages, chars))
        except PdfExtractionError as exc:
            bad.append((path.name, str(exc).split("。")[0]))
        except Exception as exc:  # noqa: BLE001
            bad.append((path.name, f"{type(exc).__name__}: {exc}"))

    print("=" * 74)
    print(f"PDF 文字层健康检查 · {folder}")
    print("=" * 74)
    print(f"共 {len(pdfs)} 份：可用 {len(ok)} · 不可用 {len(bad)}\n")

    if detail and ok:
        print("-" * 74)
        print("可用清单")
        print("-" * 74)
        for name, pages, chars in ok:
            print(f"  {name:<40} {pages:>3} 页  {chars:>7,} 字符")

    if ok:
        total_pages = sum(p for _, p, _ in ok)
        total_chars = sum(c for _, _, c in ok)
        print("-" * 74)
        print(f"合计 {total_pages} 页 / {total_chars:,} 字符"
              f"（平均每页 {total_chars / total_pages:.0f} 字符）")

    if bad:
        print("-" * 74)
        print("不可用清单（这些不能用来建知识库，会把乱码喂给模型）")
        print("-" * 74)
        for name, reason in bad:
            print(f"  ✗ {name}")
            print(f"      {reason}")

    ratio = len(ok) / len(pdfs)
    print("\n" + "=" * 74)
    if ratio == 1.0:
        print("结论：全部可用，可直接建库。")
    elif ratio >= 0.8:
        print(f"结论：{ratio:.0%} 可用，用可用的那部分即可；"
              "不可用的需要整页渲染 + 视觉模型（另一条路线）。")
    else:
        print(f"结论：仅 {ratio:.0%} 可用 —— 这批 PDF 大概率是「打印/导出」生成的，"
              "要先走 OCR / 整页渲染路线。")
    print("=" * 74)
    return 0 if ok else 1


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    detail = "--detail" in sys.argv
    folder = Path(args[0]) if args else Path.cwd()
    sys.exit(check(folder, detail=detail))


if __name__ == "__main__":
    main()
