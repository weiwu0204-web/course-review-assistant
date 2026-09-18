"""P2b 真实冒烟：验证「MCP 子进程 + 真实模型 Function Calling」能协同工作。

为什么要单独一个脚本（而不是塞进 pytest）：
    单测用的是假 LLM（可复现、零成本），它验证不了「真实模型会不会按预期调工具」。
    这里用 M3 那次实跑留下的真实课件 + 真实文档，**人为删掉后半部分小节**制造低覆盖，
    再让真模型去补——花几分钱，换「这条链路在真实条件下能跑通」的证据。

注意它**不动任何已有产物**：削弱的文档与补漏过程都写在 output/_smoke/ 下。

用法：
    .venv/Scripts/python.exe scripts/smoke_repair.py            # 跑
    .venv/Scripts/python.exe scripts/smoke_repair.py --dry-run  # 只看削弱后的覆盖率，不调模型
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUT_ROOT, current_provider, get_settings  # noqa: E402
from app.models import ExtractionResult  # noqa: E402
from app.services import batch_runner as br  # noqa: E402
from app.services.agent_repair import (  # noqa: E402
    REPAIR_TRIGGER_PERCENT,
    needs_repair,
    repair_document,
)
from app.services.coverage import measure  # noqa: E402

SMOKE_DIR = OUTPUT_ROOT / "_smoke"


def _pick_job(batch: str = "", idx: int = 0):
    """找一份有 extracted.json + doc.md 的真实产物。

    不给参数就取找到的第一份；``--batch``/``--idx`` 可指定某一节的真实产物
    （真实低覆盖的节比人为削弱更有说服力）。
    """
    roots = [OUTPUT_ROOT / batch] if batch else sorted(OUTPUT_ROOT.glob("*/"))
    for batch_dir in roots:
        bj = batch_dir / br.BATCH_JSON
        if not bj.exists():
            continue
        data = json.loads(bj.read_text(encoding="utf-8"))
        for job in data.get("jobs", []):
            if idx and job.get("idx") != idx:
                continue
            if job.get("status") != "done":
                continue
            job_dir = batch_dir / f"{job['idx']:02d}_{job['slug']}"
            ext_file = job_dir / br.EXTRACTED_JSON
            doc_file = job_dir / br.DOC_MD
            if ext_file.exists() and doc_file.exists():
                ext = ExtractionResult.model_validate_json(
                    ext_file.read_text(encoding="utf-8")
                )
                return (
                    job_dir,
                    ext,
                    doc_file.read_text(encoding="utf-8"),
                    data.get("options", {}).get("template", ""),
                    data.get("options", {}).get("style", ""),
                    float(job.get("cost_yuan") or 0),
                )
    raise SystemExit("output/ 下找不到符合条件的产物（需要 extracted.json + doc.md）")


def _weaken(document: str, keep_ratio: float = 0.5) -> str:
    """删掉靠后的小节，模拟「这份文档漏讲了后半部分」。

    按 ``## 小节`` 切分删除（而不是截断文本），这样保留的文档结构仍然完整——
    否则测的就不是"补内容"而是"修破损的 markdown"了。
    """
    parts = re.split(r"(?m)^(?=## )", document)
    head, sections = parts[0], parts[1:]
    if len(sections) < 2:
        return document
    keep = max(1, int(len(sections) * keep_ratio))
    return head + "".join(sections[:keep])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只构造并报告，不调模型")
    ap.add_argument("--keep-ratio", type=float, default=0.5, help="削弱时保留多少小节")
    ap.add_argument("--batch", default="", help="指定批次 id（用真实产物）")
    ap.add_argument("--idx", type=int, default=0, help="指定节序号（1 起）")
    ap.add_argument(
        "--raw", action="store_true",
        help="用真实文档原样跑，不做削弱（测「真实低覆盖节」时用）",
    )
    args = ap.parse_args()

    job_dir, ext, original, tpl, sty, gen_cost = _pick_job(args.batch, args.idx)
    weakened = original if args.raw else _weaken(original, args.keep_ratio)

    cov_before = measure(ext, weakened)
    cov_original = measure(ext, original)
    print(f"素材：{job_dir}  [{tpl}/{sty}]")
    if args.raw:
        print(f"  真实文档 {len(original)} 字（未削弱）")
    else:
        print(f"  原文档 {len(original)} 字 → 削弱后 {len(weakened)} 字")
    print(f"  覆盖率：{'原版 ' if not args.raw else ''}{cov_original.percent}%"
          f"{'' if args.raw else '  →  削弱后 ' + str(cov_before.percent) + '%'}"
          f"（术语 {cov_before.total_terms}，缺 {cov_before.missing_terms}）")
    print(f"  该节当初的生成成本：¥{gen_cost:.4f}")
    print(f"  触发条件（<{REPAIR_TRIGGER_PERCENT}% 且样本可靠）: "
          f"{'满足' if needs_repair(cov_before) else '不满足'}")

    if args.dry_run:
        print("\n--dry-run：到此为止，未调用模型")
        return 0

    settings = get_settings()
    if not settings.ai_api_key:
        print("\n未配置 API Key，无法真实调用。请先在设置页配好。")
        return 2

    prov = current_provider() or {}
    print(f"\n服务商：{prov.get('name', '?')} / {settings.ai_model}"
          f"（输入 {prov.get('price_in')} 输出 {prov.get('price_out')} 元/百万 token）")
    print("开始补漏（真实调用）…\n")
    t0 = time.perf_counter()

    doc, rec = repair_document(
        ext, weakened, cov_before,
        workdir=SMOKE_DIR,
        base_cost_yuan=gen_cost or 0.05,   # 用该节真实的生成成本算 40% 上限
        provider=prov,
    )
    elapsed = time.perf_counter() - t0

    print("\n===== 结果 =====")
    print(f"模式      : {rec.mode}")
    print(f"覆盖率    : {rec.trigger_percent}% → {rec.final_percent}%"
          f"（原版是 {cov_original.percent}%）")
    print(f"是否回滚  : {rec.rolled_back}")
    print(f"停止原因  : {rec.stop_reason}")
    print(f"总成本    : ¥{rec.total_cost_yuan}"
          + (f"（占该节生成成本 ¥{gen_cost:.4f} 的 "
             f"{rec.total_cost_yuan / gen_cost * 100:.0f}%）" if gen_cost else ""))
    print(f"耗时      : {elapsed:.1f} 秒")
    if rec.note:
        print(f"备注      : {rec.note}")
    print("\n逐轮：")
    for r in rec.rounds:
        print(f"  第 {r.round} 轮：工具 {r.tool_calls} 次、落地补丁 {r.patches_applied} 个、"
              f"覆盖率 {r.percent_before}% → {r.percent_after}%、¥{r.cost_yuan}")

    if not rec.rolled_back and rec.final_percent > rec.trigger_percent:
        added = doc.replace(weakened, "")  # 补进去的部分（近似：取差集）
        print(f"\n补后文档 {len(doc)} 字（比削弱版多 {len(doc) - len(weakened)} 字）")
        print("补入内容片段（前 400 字）：")
        print("  " + (added.strip()[:400] or "（未能提取差异）"))
    print(f"\n过程文件在：{SMOKE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
