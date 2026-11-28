"""P3 引用核验的阈值标定：用真实课件构造三组样本，看判据能不能把它们分开。

## 为什么必须标定

`citation.py` 里那个 `SUPPORTED_THRESHOLD` 如果拍脑袋定，会出现两种灾难：
定太高 → 真引用被大面积标成"待核"，用户不再信任这个功能（P1 踩过同样的坑：
字面判据把好文档读成 29.6%）；
定太低 → 编造的引用也能过关，这个功能就成了摆设。

## 三组样本（全部程序化构造，可复现）

| 组 | 构造方式 | 期望 |
|---|---|---|
| **真引用** | 论断 = 某一页的原文片段，引用 = 该页 | 全部「引用可信」 |
| **页码错** | 论断 = 甲页原文，引用 = **同一课件的另一页** | 应落到「引用待核」 |
| **内容编造** | 论断 = **另一份课件**的原文，引用 = 本页 | 应落到「引用待核」 |

后两组是两种不同的失败：一种是"内容对但页码标错"，一种是"页码对但内容不在那页"。
**判据必须同时抓住这两种**，只抓一种是不够的。

## 一个必须承认的局限

标定用的论断是**原样截取**的原文，而真实场景里模型是**用自己的话复述**——
复述会换词，coverage 会系统性偏低。所以：
  · 标定出的分界是**乐观上界**；
  · 实际阈值应取在分界**之下**留安全边际；
  · 真实复述场景的表现**未被本次标定验证**，已如实写进 docs/P3-RAG.md 的限制清单。

用法：
    .venv/Scripts/python.exe scripts/p3_citation_calibrate.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.citation import CitationVerifier  # noqa: E402
from app.services.extractor import SUPPORTED_EXTENSIONS, extract_any  # noqa: E402
from app.services.retriever import build_chunks  # noqa: E402

DEFAULT_COURSE_DIR = ROOT / "课件"
_arg = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
# 语料目录可由命令行指定，用来验证判据在**别的学科**上是否仍成立：
#     .venv/Scripts/python.exe scripts/p3_citation_calibrate.py 计组/
COURSE_DIR = Path(_arg) if _arg else DEFAULT_COURSE_DIR
if not COURSE_DIR.is_absolute():
    COURSE_DIR = ROOT / COURSE_DIR
N_PER_GROUP = 120
CLAIM_CHARS = 200
SEED = 20261015  # 固定种子：标定结果必须可复现


def build_groups(chunks, rng: random.Random):
    """构造三组 (answer, 期望关键词)。"""
    by_file: dict[str, list] = {}
    for c in chunks:
        by_file.setdefault(c.file, []).append(c)

    usable = [c for c in chunks if len(c.text) >= 40]

    truth, wrong_page, fabricated = [], [], []
    for c in rng.sample(usable, min(N_PER_GROUP, len(usable))):
        claim = c.text[:CLAIM_CHARS].replace("\n", " ")
        truth.append(f"{claim}（源：{c.file} 第 {c.page} 页）")

    for c in rng.sample(usable, min(N_PER_GROUP, len(usable))):
        same = [x for x in by_file[c.file] if x.page != c.page]
        if not same:
            continue
        other = rng.choice(same)
        claim = c.text[:CLAIM_CHARS].replace("\n", " ")
        wrong_page.append(f"{claim}（源：{c.file} 第 {other.page} 页）")

    for c in rng.sample(usable, min(N_PER_GROUP, len(usable))):
        others = [x for x in usable if x.file != c.file and len(x.text) >= 40]
        if not others:
            continue
        alien = rng.choice(others)
        claim = alien.text[:CLAIM_CHARS].replace("\n", " ")
        fabricated.append(f"{claim}（源：{c.file} 第 {c.page} 页）")

    return {"真引用": truth, "页码错": wrong_page, "内容编造": fabricated}


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def report(name: str, samples: list[tuple[float, int]]) -> None:
    """samples = [(coverage, rank)]"""
    if not samples:
        print(f"  {name}: 无样本")
        return
    covs = [c for c, _ in samples]
    ranks = [r for _, r in samples if r]
    print(f"  {name:<6} n={len(samples):<4}")
    print(
        f"         coverage  min {min(covs):.2f}  p25 {_pct(covs, .25):.2f}  "
        f"p50 {_pct(covs, .50):.2f}  p75 {_pct(covs, .75):.2f}  max {max(covs):.2f}"
    )
    if ranks:
        print(
            f"         rank      p25 {_pct(ranks, .25):.0f}  p50 {_pct(ranks, .50):.0f}  "
            f"p75 {_pct(ranks, .75):.0f}  max {max(ranks)}"
        )


def main() -> None:
    rng = random.Random(SEED)
    print("=" * 74)
    print("P3 引用核验 · 阈值标定（真实课件，程序化构造样本）")
    print("=" * 74)

    results = [
        extract_any(p)
        for p in sorted(
            p for p in COURSE_DIR.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    ]
    chunks = build_chunks(results)
    print(f"\n知识库：{len({c.file for c in chunks})} 份课件 / {len(chunks)} 个 chunk")

    verifier = CitationVerifier(chunks)
    print("（术语表与索引构建完成，下面跑三组样本）\n")

    groups = build_groups(chunks, rng)

    print("-" * 74)
    print("两个信号的分布")
    print("-" * 74)
    samples: dict[str, list[tuple[float, int]]] = {}
    missed: dict[str, int] = {}
    for name, answers in groups.items():
        got: list[tuple[float, int]] = []
        miss = 0
        sample_miss = ""
        for ans in answers:
            checks = verifier.verify(ans)
            if not checks:
                miss += 1
                if not sample_miss:
                    sample_miss = ans[-70:]
                continue
            got.append((checks[0].coverage, checks[0].rank))
        samples[name] = got
        missed[name] = miss
        report(name, got)
        if miss:
            print(f"         ⚠️ {miss} 条没能抽出引用，样例尾部：{sample_miss!r}")

    # -------------------------------------------------------------- 信号对比
    print("\n" + "-" * 74)
    print("两个信号各自的区分能力（真引用组应尽量通过，两组假引用应尽量不通过）")
    print("-" * 74)

    truth = samples.get("真引用", [])
    decoy = samples.get("页码错", []) + samples.get("内容编造", [])

    candidates: list[tuple[str, object]] = [
        ("rank ≤ 1", lambda c, r: 0 < r <= 1),
        ("rank ≤ 3", lambda c, r: 0 < r <= 3),
        ("coverage ≥ 0.60", lambda c, r: c >= 0.60),
        ("coverage ≥ 0.80", lambda c, r: c >= 0.80),
        # ↓ 组合规则：两个信号各管一类失败，见 main 上方的说明。
        #   初版只测单一信号，结果在两个语料上各有一个赢家（Ceph 偏爱 rank、
        #   计组偏爱 coverage）——**单一语料标定会过拟合**，必须联合标定。
        ("rank≤1 且 cov≥0.30", lambda c, r: 0 < r <= 1 and c >= 0.30),
        ("rank≤1 或 cov≥0.80", lambda c, r: (0 < r <= 1) or c >= 0.80),
        ("rank≤3 且 cov≥0.40", lambda c, r: 0 < r <= 3 and c >= 0.40),
        ("rank≤3 且 cov≥0.60", lambda c, r: 0 < r <= 3 and c >= 0.60),
        ("rank≤5 且 cov≥0.50", lambda c, r: 0 < r <= 5 and c >= 0.50),
        ("rank≤3 或 cov≥0.90", lambda c, r: (0 < r <= 3) or c >= 0.90),
    ]

    print(f"  {'规则':<26} {'真引用通过':>10} {'假引用通过':>10} {'净区分度':>10}")
    best = None
    for label, fn in candidates:
        tp = sum(1 for c, r in truth if fn(c, r)) / len(truth) if truth else 0
        fp = sum(1 for c, r in decoy if fn(c, r)) / len(decoy) if decoy else 0
        gap = tp - fp
        mark = ""
        if best is None or gap > best[1]:
            best, mark = (label, gap, tp, fp), "  ←"
        print(f"  {label:<26} {tp*100:>9.1f}% {fp*100:>9.1f}% {gap*100:>9.1f}pp{mark}")

    print("\n" + "-" * 74)
    if best and best[1] > 0:
        print(f"  净区分度最好的规则：{best[0]}（真引用 {best[2]*100:.1f}% / 假引用 {best[3]*100:.1f}%）")
    else:
        print("  ⚠️ 所有候选规则的净区分度都 ≤ 0 —— 现有信号**无法区分**真引用与假引用")
    print("-" * 74)

    # -------------------------------------------------------------- 编造页码
    print("\n附加检查：编造页码（指向不存在的页）")
    for chk in verifier.verify("这是一条编造的引用（源：不存在的课件.pptx 第 999 页）"):
        print(f"  found={chk.found}  verdict={chk.verdict}  →  {chk.verdict_label}")


if __name__ == "__main__":
    main()
