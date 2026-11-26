"""验收 5：有溯源 vs 无溯源 的对照实验。

## 为什么需要这个脚本

P3 的主张是「**强制引用**能让答案更准」。这个主张如果只是"我觉得"，那它就是修辞；
验收 5 要求的是：同一批 20 个问题、同一份语料、同一个模型，只改**引用约束**，
看答案准确率是否不同。所以这是一个受控对照，不是演示。

## 受控的三件事

1. **同一个检索结果**：改写只做一次，两臂共用 `RewriteResult`，检索结果完全一样。
   （两臂各改一次的话，差异里会混进"改写随机性 → 检索运气"，那就测不出引用的作用。）
2. **同一条代码路径**：两臂都走 `qa.answer_from_rewrite`，只有 system prompt 不同
   （`ANSWER_SYSTEM` vs `ANSWER_SYSTEM_NO_CITATION`）。
3. **同一次生成参数**：model / max_tokens / 是否关思考，全部一致。

## 盲评

每题的 A/B 顺序用**固定种子**随机决定，映射单独存进 `blind` 字段。
评分表（`p3_ab_sheet.py`）只显示 甲/乙，不显示哪条是引用臂——
**知道哪条是"我们的方案"之后，人会不自觉地给它打高分**，这是这类实验最容易坏的地方。

## 不做的事

- **不写问答历史**：40 条实验记录混进用户的 `output/qa/` 里，会把"薄弱点清单"污染掉。
  所以脚本里把 `qa.append_qa_log` 替换成 no-op。
- **不自动判定"准确"**：判据要求人工评分。脚本只产出可复现的原始数据与盲评表。

用法：
    .venv/Scripts/python.exe scripts/p3_citation_ab.py            # 20 题 × 2 臂
    .venv/Scripts/python.exe scripts/p3_citation_ab.py --limit 2  # 先冒烟
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OUTPUT_ROOT, get_settings
from app.services import kb as kbmod
from app.services import qa

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "eval" / "citation_ab" / "questions.json"
OUT_DIR = OUTPUT_ROOT / "eval_citation"

#: 盲评随机种子：写死，保证"同一份原始数据 → 同一张评分表"，换机器也复现。
SEED = 20261019


def _load_questions(limit: int | None, ids: list[str] | None) -> dict:
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    items = data["items"]
    if ids:
        items = [it for it in items if it["id"] in ids]
    if limit:
        items = items[:limit]
    data["items"] = items
    return data


def _collect(gen) -> dict:
    """把事件流收成一条结构化结果。"""
    answer, hits, checks, summary = [], [], [], {}
    timings: dict = {}
    cost = 0.0
    error = ""
    for ev in gen:
        t = ev.get("type")
        if t == "delta":
            answer.append(ev["text"])
        elif t == "sources":
            hits = ev.get("hits") or []
            timings.update(ev.get("timings") or {})
        elif t == "citations":
            checks = ev.get("checks") or []
            summary = ev.get("summary") or {}
        elif t == "done":
            cost = float(ev.get("cost_yuan") or 0.0)
            timings.update(ev.get("timings") or {})
        elif t == "error":
            error = ev.get("message") or "未知错误"
    return {
        "answer": "".join(answer),
        "hits": [{"file": h["file"], "page": h["page"], "rank": h["rank"]} for h in hits],
        "citations": summary,
        "checks": checks,
        "timings": timings,
        "cost_yuan": round(cost, 6),
        "error": error,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题（冒烟用）")
    ap.add_argument("--ids", nargs="*", default=None, help="只跑指定 id")
    args = ap.parse_args()

    spec = _load_questions(args.limit, args.ids)
    corpus = ROOT / spec.get("corpus", "课件")
    paths = sorted(p for p in corpus.iterdir() if p.suffix.lower() in {".pdf", ".pptx"})

    # 实验不写问答历史：历史是用户的"薄弱点清单"，不该被实验数据污染
    qa.append_qa_log = lambda *a, **k: None  # type: ignore[assignment]

    t0 = time.perf_counter()
    kb = kbmod.build(paths, course_name=spec.get("course_name", corpus.name))
    print(f"知识库 {kb.kb_id}：{kb.n_files} 份 / {kb.n_pages} 页 / {len(kb.chunks)} chunk"
          f"（{time.perf_counter() - t0:.1f}s）")
    print(f"模型 {get_settings().ai_model}｜题数 {len(spec['items'])}｜两臂 × 2\n")

    rng = random.Random(SEED)
    results: list[dict] = []
    total_cost = 0.0
    t_all = time.perf_counter()

    for n, item in enumerate(spec["items"], 1):
        q = item["question"]
        anchor = item["anchor"]

        # 改写只做一次，两臂共用（见模块 docstring「受控的三件事」）
        t_rw_start = time.perf_counter()
        rw = qa.rewrite_query(q, kb, enabled=True)
        rw_seconds = time.perf_counter() - t_rw_start

        arms = {}
        for arm, require in (("cite", True), ("noref", False)):
            t_start = time.perf_counter()
            got = _collect(
                qa.answer_from_rewrite(
                    q,
                    kb,
                    rw,
                    topk=5,
                    scope=None,
                    require_citation=require,
                    t_start=t_start,
                    t_rewrite=rw_seconds,
                )
            )
            arms[arm] = got
            total_cost += got["cost_yuan"]

        # 锚点页是否被检索到：**两臂共用同一份检索结果**，所以这个判断只有一份
        hit = next(
            (
                h
                for h in arms["cite"]["hits"]
                if h["file"] == anchor["file"] and h["page"] == anchor["page"]
            ),
            None,
        )

        # 盲评顺序：固定种子打乱，映射只留在数据里
        order = ["cite", "noref"]
        rng.shuffle(order)
        blind = {"A": order[0], "B": order[1]}

        results.append(
            {
                **{k: item[k] for k in ("id", "type", "question", "anchor", "key_points")},
                "rewrite": {"text": rw.rewritten, "applied": rw.applied, "note": rw.note},
                "anchor_hit": hit is not None,
                "anchor_rank": None if hit is None else hit["rank"],
                "arms": arms,
                "blind": blind,
            }
        )

        a = arms["cite"]
        b = arms["noref"]
        print(
            f"[{n:>2}/{len(spec['items'])}] {item['id']} {item['type']} "
            f"锚点={'命中#' + str(hit['rank']) if hit else '未命中'}"
            f"｜引用 {len(a['answer'])}字 {a['timings'].get('first_token_ms')}ms"
            f"｜无引用 {len(b['answer'])}字 {b['timings'].get('first_token_ms')}ms"
        )
        if a["error"] or b["error"]:
            print(f"    ⚠ 错误：{a['error'] or b['error']}")

        # 每题落一次盘：中断也只丢当前这一题
        payload = {
            "meta": {
                "kb_id": kb.kb_id,
                "n_files": kb.n_files,
                "n_pages": kb.n_pages,
                "model": get_settings().ai_model,
                "seed": SEED,
                "corpus": str(corpus),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "done": n,
                "total": len(spec["items"]),
                "cost_yuan": round(total_cost, 6),
                "elapsed_s": round(time.perf_counter() - t_all, 1),
            },
            "items": results,
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "raw.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    print(f"\n完成：{len(results)} 题 × 2 臂，花费 ¥{total_cost:.4f}，"
          f"用时 {time.perf_counter() - t_all:.0f}s")
    print(f"→ {OUT_DIR / 'raw.json'}")
    print("下一步：python scripts/p3_ab_sheet.py  生成盲评表")


if __name__ == "__main__":
    main()
