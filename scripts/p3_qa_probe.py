"""P3b 端到端探针：真实语料建库 → 真实模型问答 → 打印全链路事件。

## 为什么单独写一个探针而不是只在单测里验

单测用的是假模型（响应是我自己构造的），**它永远测不出真实模型会怎么表现**——
这在 P2 上吃过一次大亏：repair 循环用假 LLM 单测全绿，首次真实冒烟却 0 个补丁落地，
因为真实模型的节奏是"先侦察两轮再动手"，而假模型的剧本是我写的。

这个探针就是要看真实模型在这条链路上的行为：
  · 改写会不会听术语表的话？
  · 它会不会老老实实标来源？
  · 会不会编造材料里没有的内容？

用法：
    .venv/Scripts/python.exe scripts/p3_qa_probe.py 计组/
    .venv/Scripts/python.exe scripts/p3_qa_probe.py 计组/ --no-rewrite
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services import kb as kb_service  # noqa: E402
from app.services.extractor import SUPPORTED_EXTENSIONS  # noqa: E402
from app.services.qa import answer_stream  # noqa: E402

# 问题集：一半口语（测改写）、一半术语式（测直检）
#
# ⚠️ 单靠文件名匹配问题集很脆——换个语料目录名就匹配不上了。
# 所以留一个 `_DEFAULT`：**同一门课的不同课件版本可以共用同一组问题**，
# 这样"两个老师的课件"才可比（问题相同，只有语料不同）。
# 这正是本探针存在的意义：把"课件风格"对问答效果的影响量出来。
_DEFAULT_QUESTIONS = [
    "cache 是怎么工作的",          # 口语 → 期望改写成"高速缓冲存储器 …"
    "DMA 是什么",                  # 半术语
    "指令周期分为哪几个阶段",       # 术语式，基本不用改写
]

QUESTIONS: dict[str, list[str]] = {
    "_DEFAULT": _DEFAULT_QUESTIONS,
    "课件": [
        "集群满了怎么加机器",
        "纠删码是什么东西",
        "CRUSH 算法的作用",
    ],
}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_rewrite = "--no-rewrite" not in sys.argv
    folder = ROOT / (args[0] if args else "计组")

    settings = get_settings()
    if not settings.ai_api_key:
        print("✗ 未配置 API Key：请先在设置页或 .env 里填好再跑这个探针。")
        print("  （本探针会真实调用模型，产生少量费用）")
        return

    print("=" * 76)
    print(f"P3b 端到端探针 · {folder.name} · 改写={'开' if use_rewrite else '关'}")
    print(f"模型：{settings.ai_model}")
    print("=" * 76)

    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    print(f"\n[建库] {len(paths)} 份课件…")
    t0 = time.perf_counter()
    kb = kb_service.build(paths)
    print(f"  kb_id={kb.kb_id}  耗时 {time.perf_counter() - t0:.1f}s（命中缓存则更快）")
    print(f"  {kb.n_files} 份 / {kb.n_pages} 页 / {kb.n_chars:,} 字符 / 术语 {len(kb.course_terms)} 个")
    if kb.failures:
        print(f"  ⚠️ {len(kb.failures)} 份提取失败：{kb.failures[:2]}")

    questions = QUESTIONS.get(folder.name) or QUESTIONS["_DEFAULT"]
    if not questions:
        print(f"\n（{folder.name} 没有预置问题集，跳过问答）")
        return

    total_cost = 0.0
    for i, q in enumerate(questions, 1):
        print("\n" + "─" * 76)
        print(f"[Q{i}] {q}")
        print("─" * 76)

        answer = ""
        checks: list[dict] = []
        summary: dict = {}
        sources: list[dict] = []
        rewrite_info: dict = {}
        err = ""
        t0 = time.perf_counter()
        first_token_at = 0.0

        for event in answer_stream(q, kb, topk=5, use_rewrite=use_rewrite):
            kind = event["type"]
            if kind == "rewrite":
                rewrite_info = event
            elif kind == "sources":
                sources = event["hits"]
            elif kind == "delta":
                if not first_token_at:
                    first_token_at = time.perf_counter() - t0
                answer += event["text"]
            elif kind == "citations":
                checks, summary = event["checks"], event["summary"]
            elif kind == "done":
                total_cost += event.get("cost_yuan") or 0
            elif kind == "error":
                err = event["message"]

        elapsed = time.perf_counter() - t0

        if err:
            print(f"  ✗ 错误：{err}")
            continue

        if rewrite_info.get("applied"):
            print(f"  改写：{rewrite_info['original']}  →  {rewrite_info['rewritten']}")
        else:
            print(f"  改写：未生效（{rewrite_info.get('note', '')}）")

        print(f"  命中 {len(sources)} 页：")
        for h in sources[:5]:
            print(f"    · {h['file']} 第 {h['page']} 页  (score {h['score']})")

        if not answer:
            print("  （无答案——大概率是检索为空）")
            continue

        print(f"\n  答案（首 token {first_token_at:.1f}s，全程 {elapsed:.1f}s）：")
        for line in answer.splitlines():
            print(f"    {line}")

        print(f"\n  引用核验（{summary.get('note', '')}）：")
        if not checks:
            print("    （答案里没有出现任何引用标记）")
        for c in checks:
            mark = "✓" if c["verdict"] == "supported" else ("✗" if c["verdict"] == "not_found" else "?")
            print(
                f"    {mark} {c['file']} 第 {c['page']} 页  → {c['verdict_label']}"
                f"（rank {c['rank']}, coverage {c['coverage']}）"
            )
            print(f"       论断：{c['claim'][:60]}")

    print("\n" + "=" * 76)
    print(f"合计成本：¥{total_cost:.4f}")
    print("=" * 76)


if __name__ == "__main__":
    main()
