"""首 token 延迟分项探针：回答「4.9 秒到底花在哪」。

## 为什么需要它

P3 的验收 3 写的是「首 token ≤ 3 秒」，而 §16.5 的实测是 **4.9 秒**。
两个反应都是错的：
- 直接把判据改成 5 秒（**改判据去迁就实现**，本项目最忌讳这个）；
- 直接去优化生成（可能那 4.9 秒里有一大半根本不在生成上）。

先量。链路里首 token 之前其实有两段：**改写那次 LLM 调用**（一次完整请求）
和检索（实测 0.2 ms，可忽略）。所以 `first_token_ms` 里藏着改写。

## 测法

同一批问题跑两遍：`use_rewrite=True` 和 `False`。
- 两遍的差值 ≈ 改写那一跳的净成本；
- `first_token_ms - rewrite_ms - retrieve_ms` ≈ 模型真正的首 token 时间。

用法：
    .venv/Scripts/python.exe scripts/qa_latency_probe.py            # 默认 5 题 × 2 组
    .venv/Scripts/python.exe scripts/qa_latency_probe.py --n 3
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OUTPUT_ROOT, get_settings
from app.services import kb as kbmod
from app.services import qa

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = OUTPUT_ROOT / "eval_citation"


def _one(question: str, kb, use_rewrite: bool) -> dict:
    t0 = time.perf_counter()
    res = {"rewrite_ms": 0.0, "retrieve_ms": 0.0, "first_token_ms": None, "total_ms": 0.0}
    answer_len = 0
    for ev in qa.answer_stream(question, kb, use_rewrite=use_rewrite):
        if ev["type"] == "sources":
            res.update(ev.get("timings") or {})
        elif ev["type"] == "delta":
            answer_len += len(ev["text"])
        elif ev["type"] == "done":
            res.update(ev.get("timings") or {})
        elif ev["type"] == "error":
            res["error"] = ev["message"]
    res["answer_chars"] = answer_len
    res["wall_s"] = round(time.perf_counter() - t0, 2)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    spec = json.loads((ROOT / "eval" / "citation_ab" / "questions.json").read_text("utf-8"))
    qs = [it["question"] for it in spec["items"][: args.n]]

    qa.append_qa_log = lambda *a, **k: None  # type: ignore[assignment]  # 探针不污染历史

    corpus = ROOT / spec.get("corpus", "课件")
    paths = sorted(p for p in corpus.iterdir() if p.suffix.lower() in {".pdf", ".pptx"})
    kb = kbmod.build(paths, course_name=spec.get("course_name", corpus.name))
    print(f"知识库 {kb.kb_id}（{kb.n_files} 份 / {kb.n_pages} 页）｜模型 {get_settings().ai_model}")
    print(f"{len(qs)} 题 × 2 组（改写开/关）\n")

    rows = []
    for i, q in enumerate(qs, 1):
        on = _one(q, kb, use_rewrite=True)
        off = _one(q, kb, use_rewrite=False)
        # 生成首 token = 首 token - 改写 - 检索
        gen_on = (on["first_token_ms"] or 0) - on["rewrite_ms"] - on["retrieve_ms"]
        gen_off = (off["first_token_ms"] or 0) - off["rewrite_ms"] - off["retrieve_ms"]
        rows.append({"q": q, "on": on, "off": off, "gen_on": gen_on, "gen_off": gen_off})
        print(f"[{i}/{len(qs)}] {q[:24]}…")
        print(f"   改写开：改写 {on['rewrite_ms']:>7.0f}ms | 检索 {on['retrieve_ms']:>5.0f}ms "
              f"| 首 token {on['first_token_ms'] or 0:>7.0f}ms | 全程 {on['total_ms']:>7.0f}ms")
        print(f"   改写关：改写 {off['rewrite_ms']:>7.0f}ms | 检索 {off['retrieve_ms']:>5.0f}ms "
              f"| 首 token {off['first_token_ms'] or 0:>7.0f}ms | 全程 {off['total_ms']:>7.0f}ms")
        print(f"   → 生成首 token（估算）：开 {gen_on:>6.0f}ms / 关 {gen_off:>6.0f}ms\n")

    def med(key, group="on"):
        vals = [r[group][key] for r in rows if r[group][key] is not None]
        return statistics.median(vals) if vals else float("nan")

    ft_on = [r["on"]["first_token_ms"] or 0 for r in rows]
    ft_off = [r["off"]["first_token_ms"] or 0 for r in rows]
    rw = [r["on"]["rewrite_ms"] for r in rows]
    gen_on = [r["gen_on"] for r in rows]
    gen_off = [r["gen_off"] for r in rows]

    print("=== 汇总（中位数） ===")
    print(f"改写耗时            {med('rewrite_ms'):>7.0f} ms")
    print(f"检索耗时            {med('retrieve_ms'):>7.0f} ms")
    print(f"首 token（改写开）  {statistics.median(ft_on):>7.0f} ms")
    print(f"首 token（改写关）  {statistics.median(ft_off):>7.0f} ms")
    print(f"生成首 token（开）  {statistics.median(gen_on):>7.0f} ms")
    print(f"生成首 token（关）  {statistics.median(gen_off):>7.0f} ms")
    print(f"\n判据是 ≤3000 ms。改写一关，首 token 中位数变化 "
          f"{statistics.median(ft_on) - statistics.median(ft_off):+.0f} ms。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latency.json").write_text(
        json.dumps(
            {
                "model": get_settings().ai_model,
                "kb_id": kb.kb_id,
                "n": len(rows),
                "rows": rows,
                "median": {
                    "rewrite_ms": med("rewrite_ms"),
                    "retrieve_ms": med("retrieve_ms"),
                    "first_token_on": statistics.median(ft_on),
                    "first_token_off": statistics.median(ft_off),
                    "gen_first_token_on": statistics.median(gen_on),
                    "gen_first_token_off": statistics.median(gen_off),
                },
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n→ {OUT_DIR / 'latency.json'}")


if __name__ == "__main__":
    main()
