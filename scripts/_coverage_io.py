"""覆盖率相关脚本共用的读取逻辑（回填 / 体检都用它）。

为什么单独抽出来：`load_job_extraction` 里有一条**容易写歪的规则**——
优先用 job 目录里当时落盘的 `extracted.json`，而不是重新提取源课件。
用当时那份是为了**与当时生成的结果保持一致**：重新提取可能因库版本不同
得到略有差异的结果，那算出来的覆盖率就不再是「当时那份文档的覆盖率」了。
这条规则只放一处，两个脚本才不会有朝一日各写一套。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import ExtractionResult  # noqa: E402
from app.services import batch_runner as br  # noqa: E402

SOURCE_SUFFIXES = (".pptx", ".pdf")


def load_job_extraction(job_dir: Path) -> ExtractionResult | None:
    """取该份课件当时的提取结果：优先 extracted.json，兜底现场提取。取不到返回 None。"""
    cached = job_dir / br.EXTRACTED_JSON
    if cached.exists():
        return ExtractionResult.model_validate_json(br.read_text_retry(cached))
    src = next((p for p in job_dir.iterdir() if p.suffix.lower() in SOURCE_SUFFIXES), None)
    if src is None:
        return None
    return br.extract_any(src, image_dir=job_dir / "images")


def halves_summary(percents: list[int], split: int = 7) -> str:
    """把逐节覆盖率分成前半/后半两段看——**通用 agent 的掉队特征就是这么看出来的**。"""
    if not percents:
        return "（无数据）"
    if len(percents) <= split:
        return f"平均 {sum(percents) / len(percents):.1f}%（{len(percents)} 节）"
    front = percents[:split]
    back = percents[split:]
    return (f"平均 {sum(percents) / len(percents):.1f}%"
            f"｜前 {len(front)} 节 {sum(front) / len(front):.1f}%"
            f"｜后 {len(back)} 节 {sum(back) / len(back):.1f}%")
