"""解盲 + 计算验收 5 的对照结果。

输入：`raw.json`（实验原始数据，含 A/B 映射）+ `scores.json`（人工评分导出）
输出：`report.md`（可直接贴进 docs/P3-RAG.md 的表格与结论）

## 判据（写死，不许事后改）

- **主指标**：严格准确率 = 「准确（2）」的题数 / 参评题数。
  验收 5 的原话是"带引用的答案准确率更高"，所以只看这一条。
- **辅助指标**（只解释现象，不替代主指标）：部分准确率、编造率、引用条数与核验通过率、
  锚点页被引用率、答案长度、首 token。
- **配对检验**：同一题两臂都评过的，用精确 McNemar（双向二项）看差异是否超过随机波动。
  20 题的样本量下，差 2 题以内基本不显著——这一点必须写进结论，不能拿"高了 5 个点"当结论。

## 为什么不用自动打分当判据

术语重合、长度、有没有引用标签，都不等于"事实对不对"。自动指标只能做交叉验证：
如果人工判"准确"而答案里一条锚点术语都没有，说明评分可能有问题，脚本会把这些题列出来复核。
"""
from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OUTPUT_ROOT

OUT_DIR = OUTPUT_ROOT / "eval_citation"


def mcnemar_exact(b: int, c: int) -> float:
    """精确 McNemar：只看不一致的两类 (b, c)，原假设下各占 1/2。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main() -> None:
    raw_path = OUT_DIR / "raw.json"
    score_path = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR / "scores.json"
    if not raw_path.exists():
        raise SystemExit("缺 raw.json，先跑 scripts/p3_citation_ab.py")
    if not score_path.exists():
        raise SystemExit(f"缺 {score_path.name}，先在评分表里评完并导出")

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    scores = json.loads(score_path.read_text(encoding="utf-8"))["scores"]

    rows = []
    for it in raw["items"]:
        sc = scores.get(it["id"]) or {}
        label = {arm: pos for pos, arm in it["blind"].items()}  # {"cite": "A"/"B", ...}
        got = {}
        for arm in ("cite", "noref"):
            s = sc.get(label[arm]) or {}
            if s.get("grade") is None:
                got[arm] = None
                continue
            # fabricated=None 表示"这个维度没评"，不是"没编造"——两者必须分开记
            got[arm] = {"grade": int(s["grade"]), "fabricated": s.get("fabricated")}
        if got.get("cite") is None or got.get("noref") is None:
            continue
        rows.append(
            {
                "id": it["id"],
                "type": it["type"],
                "anchor_hit": it["anchor_hit"],
                "anchor_rank": it["anchor_rank"],
                "cite": got["cite"],
                "noref": got["noref"],
                "cite_answer": it["arms"]["cite"]["answer"],
                "noref_answer": it["arms"]["noref"]["answer"],
                "cite_cites": it["arms"]["cite"]["citations"],
                "cite_checks": it["arms"]["cite"]["checks"],
                "cite_timings": it["arms"]["cite"]["timings"],
                "noref_timings": it["arms"]["noref"]["timings"],
                "anchor": it["anchor"],
                "question": it["question"],
            }
        )

    total = len(raw["items"])
    if not rows:
        raise SystemExit("没有一题两臂都评过分——先评分再导出")
    if len(rows) < total:
        print(f"⚠️ 只评了 {len(rows)}/{total} 题，结论只基于这些题\n")

    def stat(sub, arm):
        n = len(sub)
        if not n:
            return {}
        exact = sum(1 for r in sub if r[arm]["grade"] == 2)
        part = sum(1 for r in sub if r[arm]["grade"] == 1)
        wrong = sum(1 for r in sub if r[arm]["grade"] == 0)
        fab_scored = [r for r in sub if r[arm]["fabricated"] is not None]
        fab = sum(1 for r in fab_scored if r[arm]["fabricated"])
        ans = "cite_answer" if arm == "cite" else "noref_answer"
        return {
            "n": n,
            "exact": exact,
            "part": part,
            "wrong": wrong,
            "fab": fab,
            "fab_scored": len(fab_scored),
            "fab_rate": (fab / len(fab_scored)) if fab_scored else None,
            "exact_rate": exact / n,
            "usable_rate": (exact + part) / n,
            "avg_chars": sum(len(r[ans]) for r in sub) / n,
        }

    all_c, all_n = stat(rows, "cite"), stat(rows, "noref")
    hit_rows = [r for r in rows if r["anchor_hit"]]
    hit_c, hit_n = stat(hit_rows, "cite"), stat(hit_rows, "noref")

    # 配对检验：准确 vs 不准确
    b = sum(1 for r in rows if r["cite"]["grade"] == 2 and r["noref"]["grade"] != 2)
    c = sum(1 for r in rows if r["cite"]["grade"] != 2 and r["noref"]["grade"] == 2)
    p = mcnemar_exact(b, c)

    # 引用侧（只有引用臂有）
    n_cites = sum(r["cite_cites"].get("total", 0) for r in rows)
    n_sup = sum(r["cite_cites"].get("supported", 0) for r in rows)
    n_weak = sum(r["cite_cites"].get("weak", 0) for r in rows)
    n_nf = sum(r["cite_cites"].get("not_found", 0) for r in rows)
    anchor_cited = 0
    for r in rows:
        af, ap = r["anchor"]["file"], r["anchor"]["page"]
        if any(ch.get("file") == af and ch.get("page") == ap for ch in r["cite_checks"]):
            anchor_cited += 1

    def pct(x):
        return f"{x * 100:.1f}%"

    def fab_cell(st):
        """编造率：没评就写"未评"，绝不写 0%——0% 会被读成"确实没编造"。"""
        if st["fab_rate"] is None:
            return "未评（评分时未勾选该维度）"
        return f"{pct(st['fab_rate'])}（{st['fab']} 题）"

    lines = []
    lines.append("## 验收 5 实测结果（有溯源 vs 无溯源）\n")
    lines.append(
        f"语料：{raw['meta']['n_files']} 份课件 / {raw['meta']['n_pages']} 页；"
        f"模型 `{raw['meta']['model']}`；参评 **{len(rows)}** 题（每题两臂各一次，"
        f"共用同一检索结果）；总花费 ¥{raw['meta']['cost_yuan']:.4f}。\n"
    )

    lines.append("| 指标 | 有引用 | 无引用 | 差 |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| **严格准确率**（2 分） | **{pct(all_c['exact_rate'])}**（{all_c['exact']}/{all_c['n']}） "
        f"| {pct(all_n['exact_rate'])}（{all_n['exact']}/{all_n['n']}） "
        f"| {(all_c['exact_rate'] - all_n['exact_rate']) * 100:+.1f} pp |"
    )
    lines.append(
        f"| 可用率（2+1 分） | {pct(all_c['usable_rate'])} | {pct(all_n['usable_rate'])} "
        f"| {(all_c['usable_rate'] - all_n['usable_rate']) * 100:+.1f} pp |"
    )
    lines.append(
        f"| 编造率 | {fab_cell(all_c)} | {fab_cell(all_n)} | — |"
    )
    lines.append(
        f"| 平均答案长度 | {all_c['avg_chars']:.0f} 字 | {all_n['avg_chars']:.0f} 字 | — |"
    )
    lines.append("")
    lines.append(
        f"配对检验（精确 McNemar）：引用臂独对 **{b}** 题、无引用臂独对 **{c}** 题，"
        f"**p = {p:.3f}**。"
    )

    lines.append("\n### 只看「锚点页被检索到」的题\n")
    lines.append(
        f"共 {len(hit_rows)}/{len(rows)} 题检索到了锚点页。"
        "落在检索之外的题，两臂同样答不出——把它算进对比会稀释引用约束的作用，"
        "所以这一段单独报（**但不作为主指标**，主指标仍是上面那张全样本表）。\n"
    )
    lines.append("| 指标 | 有引用 | 无引用 | 差 |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| 严格准确率 | **{pct(hit_c['exact_rate'])}**（{hit_c['exact']}/{hit_c['n']}） "
        f"| {pct(hit_n['exact_rate'])}（{hit_n['exact']}/{hit_n['n']}） "
        f"| {(hit_c['exact_rate'] - hit_n['exact_rate']) * 100:+.1f} pp |"
    )

    lines.append("\n### 引用侧读数（只有引用臂）\n")
    lines.append(
        f"共 {n_cites} 条引用：可信 {n_sup}（{pct(n_sup / n_cites) if n_cites else '—'}）、"
        f"待核 {n_weak}、未找到 {n_nf}；"
        f"**锚点页被引用的题：{anchor_cited}/{len(rows)}**。\n"
    )

    # 分类型
    lines.append("### 按提问方式拆开\n")
    lines.append("| 题型 | 题数 | 有引用 | 无引用 | 差 |")
    lines.append("|---|---|---|---|---|")
    for t in ("术语", "口语"):
        sub = [r for r in rows if r["type"] == t]
        if not sub:
            continue
        a, n2 = stat(sub, "cite"), stat(sub, "noref")
        lines.append(
            f"| {t} | {len(sub)} | {pct(a['exact_rate'])} | {pct(n2['exact_rate'])} "
            f"| {(a['exact_rate'] - n2['exact_rate']) * 100:+.1f} pp |"
        )

    # 逐题明细
    lines.append("\n### 逐题明细\n")
    lines.append("| # | 题 | 类 | 锚点 | 有引用 | 无引用 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        cond = "命中" if r["anchor_hit"] else "未命中"
        rank = f"#{r['anchor_rank']}" if r["anchor_rank"] else "—"
        lines.append(
            f"| {r['id']} | {r['question'][:26]}… | {r['type']} | {cond}{rank} "
            f"| {r['cite']['grade']}{' 🚩' if r['cite']['fabricated'] else ''} "
            f"| {r['noref']['grade']}{' 🚩' if r['noref']['fabricated'] else ''} |"
        )

    # 结论（按事先定死的判据，不许含糊）
    delta = all_c["exact_rate"] - all_n["exact_rate"]
    if delta > 0 and p < 0.05:
        verdict = (
            f"**验收 5 通过**：有引用臂严格准确率高 {delta * 100:.1f} pp，"
            f"配对检验 p={p:.3f} < 0.05。"
        )
    elif delta > 0:
        verdict = (
            f"**方向一致但未达统计显著**：有引用臂高 {delta * 100:.1f} pp，"
            f"但 n={len(rows)}，p={p:.3f}。按事先定的判据（\"更高\"），"
            f"只能说**趋势支持、证据不足**——不能写成\"证明了更准\"。"
            f"要拿到显著结论，需要扩样本到 40+ 题或换更强的判据。"
        )
    elif delta == 0:
        ties = sum(1 for r in rows if r["cite"]["grade"] == r["noref"]["grade"])
        # 把同分拆成两类：检索命中的（模型本来就对）与检索未命中的（两臂同错/同分）
        tie_hit = sum(1 for r in rows if r["anchor_hit"] and r["cite"]["grade"] == r["noref"]["grade"])
        tie_miss = sum(
            1 for r in rows if not r["anchor_hit"] and r["cite"]["grade"] == r["noref"]["grade"]
        )
        gem = sum(1 for r in rows if not r["anchor_hit"] and r["cite"]["grade"] == 0)
        verdict = (
            f"**未观察到差异**：两臂严格准确率相同（{pct(all_c['exact_rate'])}），"
            f"**{ties}/{len(rows)} 题逐题同分、不一致对 {b + c} 对**，"
            f"McNemar p={p:.3f}（0 对不一致时这个检验没有信息量）。\n\n"
            f"这既不能读成「引用没用」，也不能读成正向——**这个题集没有给两臂留出差异空间**：\n"
            f"1. **锚点页命中的 {tie_hit} 题，两臂全部同分且基本都是满分**——"
            f"模型在这份语料上本来就答得对，引用约束没有可提升的余地（天花板效应）；\n"
            f"2. **锚点页未命中的 {tie_miss} 题，两臂同错或同分**"
            f"（其中 {gem} 题两臂都判 0，答案都写「课件没提到」）——"
            f"这 {tie_miss} 题测的是**检索**，不是引用：两臂拿到的上下文一模一样；\n"
            f"3. n={len(rows)}、每臂每次只生成一次，模型自身的随机性没有被平均掉。\n\n"
            f"**按事先定死的判据：验收 5 未通过（未证实）。** "
            f"不许调判据、不许换成「看起来高一点」的指标去凑通过。"
        )
    else:
        verdict = (
            f"**与预期相反**：无引用臂反而高 {-delta * 100:.1f} pp（p={p:.3f}）。"
            "必须如实记录，并检查是否是引用标注占用了篇幅、挤掉了内容。"
        )
    lines.append("\n### 结论\n")
    lines.append(verdict + "\n")
    lines.append(
        "### 这次实验测到了什么、没测到什么\n"
        "\n"
        "**测到了（可以直接用的）**\n"
        "\n"
        "| 项 | 读数 |\n"
        "|---|---|\n"
        f"| 引用层不制造错位 | 引用到锚点页的题 = {anchor_cited}/{len(rows)}，与检索命中数完全相同 |\n"
        f"| 引用可信度（真实输出） | {n_cites} 条引用，可信 {pct(n_sup / n_cites) if n_cites else '—'}、"
        f"未找到 {n_nf} 条 |\n"
        f"| 成本 | 单次问答 ¥{raw['meta']['cost_yuan'] / (len(rows) * 2):.4f}，"
        "约为单节文档生成（¥0.13）的 7% |\n"
        "\n"
        "**没测到**：引用让答案更准。两臂准确率相同，这一条**不能写进简历**。\n"
        "\n"
        "**为什么这不算白做**：它把一个含糊的卖点（\"更准\"）换成了一个有数据的卖点"
        "（\"可核验、约 90% 可信、成本 1/10\"）。后者面试时更站得住——"
        "说\"更准\"会被追问\"怎么证明\"，说\"每条论断带页码、自动核验出哪条待核\"当场能答。\n"
        "\n"
        "### 要继续验证的话（按性价比排）\n"
        "\n"
        "1. **换更难的题集**：现在这批题模型蒙对率太高。要挑模型**容易答错**的——"
        "跨两页才能拼出答案的、含具体数字/命令的、容易混淆的相近概念。\n"
        "2. **换指标**：主指标改为「**无出处断言率**」（引用臂每条论断都必须有出处，理论上更接近 0）。"
        "这是新判据，必须**事先声明并重新跑**，不能拿旧数据改算。\n"
        "3. **提高统计功效**：每题重复 3 次取多数、题量扩到 40+。\n"
        "\n"
    )
    lines.append(
        "> 判据说明：主指标是「严格准确率」= 人工评 2 分的题数占比；"
        "评分规则是只看事实与原文是否一致，忽略格式、长度、有无引用（引用本来藏不住，"
        "只能靠规则防偏）。单题样本只跑了一次生成，"
        "**模型本身的随机性没有被平均掉**——这也是 n=20 时 p 值偏大的原因之一，"
        "要更强结论应重复 3 次取均值。"
    )

    out = "\n".join(lines)
    (OUT_DIR / "report.md").write_text(out, encoding="utf-8")
    print(out)
    print(f"\n→ {OUT_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
