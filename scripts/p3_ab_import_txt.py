"""把人工评分记录（一个 txt/md 表格）导成 `scores.json`。

## 为什么需要这个

网页评分表适合"一题一题点"，但 40 条答案一次性读完更省事——
人工评分的实际做法常常是：把两段答案抄进一个 txt，逐题写 甲/乙 得分。
这个脚本把那种记录导成 `p3_ab_score.py` 能吃的格式，避免手抄出错。

## 认的格式（不严格，够用即可）

    每个 `**Q01**`（或 `Q01`）之后的两列数字就是 甲、乙 得分（0/1/2）。
    备注列里出现的数字不会被误当成得分——只认 `| 数字` / `| 数字` 结尾的表格单元。

用法：
    .venv/Scripts/python.exe scripts/p3_ab_import_txt.py <评分记录.txt>
    → output/eval_citation/scores.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OUTPUT_ROOT

OUT_DIR = OUTPUT_ROOT / "eval_citation"

#: 题号分隔（`**Q01**` / `Q1` / `q01` 都认）
_SPLIT = re.compile(r"\*{0,2}[Qq]0*(\d{1,2})\*{0,2}")
#: 表格单元里的 0/1/2 —— 行尾形式，避免把"满分 2 分"这类文字算进去
_CELL = re.compile(r"\|\s*([012])\s*$", re.M)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：p3_ab_import_txt.py <评分记录.txt>")
    src = Path(sys.argv[1])
    text = src.read_text(encoding="utf-8", errors="replace")

    marks = list(_SPLIT.finditer(text))
    if not marks:
        raise SystemExit("没找到题号（期待 `**Q01**` 这类标记）")

    raw = json.loads((OUT_DIR / "raw.json").read_text(encoding="utf-8"))
    want = [it["id"] for it in raw["items"]]

    grades: dict[str, tuple[int, int]] = {}
    for i, m in enumerate(marks):
        qid = f"q{int(m.group(1)):02d}"
        if qid in grades:
            continue  # 同一题号在后文"问题分析"里还会再出现一次，只认第一次（表格）
        block = text[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        nums = [int(x) for x in _CELL.findall(block)]
        if len(nums) < 2:
            continue  # 后文的分析段落没有得分单元格，不算错
        grades[qid] = (nums[0], nums[1])

    missing = [q for q in want if q not in grades]
    if missing:
        raise SystemExit(f"缺这些题的评分：{missing}")

    scores = {
        q: {
            "A": {"grade": grades[q][0], "fabricated": None},  # 甲 = blind.A
            "B": {"grade": grades[q][1], "fabricated": None},  # 乙 = blind.B
        }
        for q in want
    }
    payload = {
        "scored_at": None,
        "course": raw["meta"].get("corpus", ""),
        "source": str(src),
        "note": "fabricated=null 表示该维度未评分（不是 0 分）",
        "scores": scores,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scores.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    dist = {}
    for q, (a, b) in grades.items():
        dist[a] = dist.get(a, 0) + 1
        dist[b] = dist.get(b, 0) + 1
    print("逐题核对（甲/乙）：")
    print("  " + "  ".join(f"{q[1:]}={a}/{b}" for q, (a, b) in grades.items()))
    print(f"导入 {len(grades)} 题（{len(want)} 题全部齐）｜得分分布 {dist}")
    print(f"→ {OUT_DIR / 'scores.json'}")
    print("下一步：python scripts/p3_ab_score.py")


if __name__ == "__main__":
    main()
