"""一次性帮手：把课件切块后的索引导出来，供人工挑选评估题。

不属于生产链路，只服务「验收 5 的 20 题评估集」的作者工作：
    需要先看清每一页有什么内容，才能写出「答案确实在这页」的题。

用法：
    .venv/Scripts/python.exe scripts/p3_dump_chunks.py 课件/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OUTPUT_ROOT
from app.services import kb as kbmod


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "课件")
    paths = sorted([p for p in src.iterdir() if p.suffix.lower() in {".pptx", ".pdf"}])
    kb = kbmod.build(paths, course_name=src.name)
    print(f"kb_id={kb.kb_id}  {kb.n_files} 份 / {kb.n_pages} 页 / {len(kb.chunks)} chunk / {kb.n_chars} 字")

    out = OUTPUT_ROOT / "eval_citation" / "chunks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "i": i,
            "file": c.file,
            "page": c.page,
            "title": c.title,
            "chars": len(c.text),
            "head": c.text[:400],
        }
        for i, c in enumerate(kb.chunks)
    ]
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
