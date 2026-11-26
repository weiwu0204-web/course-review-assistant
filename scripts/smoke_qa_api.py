"""P3 问答链路的端到端烟雾测试（走真实 HTTP 接口 + 真实模型）。

## 为什么要有它

`tests/test_qa.py` 的 28 个用例全用假模型（响应是我自己构造的），
测的是**逻辑分支对不对**；这个脚本测的是**真链路通不通**——
multipart 上传、SSE 帧格式、事件顺序、引用核验结果，这些只有过一遍真 HTTP 才看得见。

（P2 的教训：假 LLM 单测全绿，首次真实冒烟 0 个补丁落地。
 假替身的剧本是测试自己写的，永远测不出真实现会怎么做。）

## 用法

先起服务（另开一个终端）：
    .venv/Scripts/python.exe -m uvicorn app.main:app --port 8011 --log-level warning

再跑：
    .venv/Scripts/python.exe scripts/smoke_qa_api.py 计算机组成原理/计组
    .venv/Scripts/python.exe scripts/smoke_qa_api.py 计算机组成原理/计组 --port 8011

**会真实调用模型，产生少量费用**（一次问答约 ¥0.02）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

#: 只挑几份，别把整门课都传上去——烟雾测试要快
SAMPLE_HINTS = ["DMA", "指令周期", "高速缓冲"]
QUESTIONS = ["DMA 是什么", "指令周期分为哪几个阶段"]


def pick_files(folder: Path) -> list[Path]:
    all_files = sorted(
        p for p in folder.iterdir() if p.suffix.lower() in {".pptx", ".pdf"}
    )
    picked = [p for p in all_files if any(h in p.name for h in SAMPLE_HINTS)]
    return picked[:4] or all_files[:3]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = ROOT / (args[0] if args else "计算机组成原理/计组")
    port = "8011"
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    base = f"http://127.0.0.1:{port}"

    files = pick_files(folder)
    if not files:
        print(f"✗ {folder} 下没有可用课件")
        return 2

    print("=" * 74)
    print(f"P3 问答烟雾测试 · {base}")
    print("=" * 74)

    # ---------------------------------------------------------- 1. 建库
    print(f"\n[1] 建库：上传 {len(files)} 份课件")
    for f in files:
        print(f"    · {f.name}")
    t0 = time.perf_counter()
    with httpx.Client(timeout=300) as client:
        resp = client.post(
            f"{base}/api/kb",
            files=[("files", (f.name, f.read_bytes())) for f in files],
        )
    if resp.status_code != 200:
        print(f"  ✗ 建库失败 {resp.status_code}：{resp.text[:200]}")
        return 1
    kb = resp.json()
    print(f"  ✓ kb_id={kb['kb_id']}  耗时 {time.perf_counter() - t0:.1f}s")
    print(f"    {kb['n_files']} 份 / {kb['n_pages']} 页 / {kb['n_chars']:,} 字符 "
          f"/ 术语 {kb['n_terms']} 个")
    if kb.get("failures"):
        print(f"    ⚠️ 未能提取：{kb['failures']}")

    # ---------------------------------------------------------- 2. 问答
    seen_types: list[str] = []
    for i, q in enumerate(QUESTIONS, 1):
        print("\n" + "─" * 74)
        print(f"[2.{i}] 提问：{q}")
        print("─" * 74)
        answer, sources, checks, summary = "", [], [], {}
        rewrite, error = {}, ""
        t0 = time.perf_counter()
        first_at = 0.0

        with httpx.Client(timeout=300) as client:
            with client.stream(
                "POST",
                f"{base}/api/ask",
                json={"kb_id": kb["kb_id"], "question": q, "use_rewrite": True},
            ) as r:
                if r.status_code != 200:
                    print(f"  ✗ {r.status_code}")
                    return 1
                buf = ""
                for chunk in r.iter_text():
                    buf += chunk
                    frames = buf.split("\n\n")
                    buf = frames.pop() or ""
                    for frame in frames:
                        if not frame.startswith("data: "):
                            continue
                        ev = json.loads(frame[6:])
                        seen_types.append(ev["type"])
                        if ev["type"] == "rewrite":
                            rewrite = ev
                        elif ev["type"] == "sources":
                            sources = ev["hits"]
                        elif ev["type"] == "delta":
                            if not first_at:
                                first_at = time.perf_counter() - t0
                            answer += ev["text"]
                        elif ev["type"] == "citations":
                            checks, summary = ev["checks"], ev["summary"]
                        elif ev["type"] == "error":
                            error = ev["message"]

        if error:
            print(f"  ✗ {error}")
            return 1
        print(f"  改写：{rewrite.get('rewritten') if rewrite.get('applied') else '未生效'}")
        print(f"  命中 {len(sources)} 页：" + "、".join(
            f"{h['file'][:16]} p{h['page']}" for h in sources[:3]
        ))
        print(f"  首 token {first_at:.1f}s / 全程 {time.perf_counter() - t0:.1f}s")
        print(f"  答案 {len(answer)} 字，前 150 字：")
        print("    " + answer[:150].replace("\n", "\n    "))
        print(f"  引用核验：{summary.get('note', '')}")
        for c in checks[:4]:
            print(f"    {c['verdict_label']:<8} {c['file'][:20]} 第 {c['page']} 页"
                  f"  (rank {c['rank']})")

    # ---------------------------------------------------------- 3. 断言
    print("\n" + "=" * 74)
    required = ["rewrite", "sources", "delta", "citations", "done"]
    missing = [t for t in required if t not in seen_types]
    if missing:
        print(f"✗ 事件序列缺：{missing}")
        return 1
    if seen_types.index("sources") > seen_types.index("delta"):
        print("✗ sources 应早于 delta（用户要先看到「找到了哪几页」）")
        return 1
    print("✓ 事件序列完整：rewrite → sources → delta* → citations → done")
    print("✓ sources 早于 delta")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
