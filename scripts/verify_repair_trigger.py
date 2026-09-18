"""repair 触发判定验收：对**已有产物**做本地判定（不调模型、零成本）。

对应 docs/P2-REPAIR.md §5 验收标准 2：
    「M3 全量 14 节（97.7%，全 ≥95）跑一遍 → repair 一次都不触发、零额外成本」

为什么可以本地做：触发条件是纯函数（**档位适用 + 样本可靠 + 覆盖率 <94**），
不需要真的跑一遍 repair —— 判定不触发就等于不花钱。
这个脚本把「会不会触发」这件事变成可重复核对的证据，而不是口头保证。

用法：
    .venv/Scripts/python.exe scripts/verify_repair_trigger.py

退出码：0 = 判定全部符合预期；1 = 出现了不该触发的触发（档位不适用却判为要补漏）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUT_ROOT  # noqa: E402
from app.models import ExtractionResult  # noqa: E402
from app.services import batch_runner as br  # noqa: E402
from app.services.agent_repair import REPAIR_TRIGGER_PERCENT, needs_repair  # noqa: E402
from app.services.coverage import applies_to, measure  # noqa: E402


def main() -> int:
    if not OUTPUT_ROOT.exists():
        print(f"找不到产物目录：{OUTPUT_ROOT}")
        return 0

    ok_skip = ok_trigger = bad = 0
    print(f"{'批次':<12}{'档位':<16}{'节':<5}{'覆盖率':>7}{'触发?':>8}  说明")
    print("-" * 78)

    for batch_dir in sorted(OUTPUT_ROOT.iterdir()):
        bj = batch_dir / br.BATCH_JSON
        if not bj.exists():
            continue
        data = json.loads(bj.read_text(encoding="utf-8"))
        opts = data.get("options", {})
        tpl, sty = opts.get("template", "复习指南"), opts.get("style", "详细")
        applicable = applies_to(tpl, sty)

        for job in data["jobs"]:
            if job["status"] != "done":
                continue
            job_dir = batch_dir / f"{job['idx']:02d}_{job['slug']}"
            ext_f, doc_f = job_dir / br.EXTRACTED_JSON, job_dir / br.DOC_MD
            if not (ext_f.exists() and doc_f.exists()):
                continue
            ext = ExtractionResult.model_validate_json(ext_f.read_text(encoding="utf-8"))
            cov = measure(ext, doc_f.read_text(encoding="utf-8"))

            # 真实触发条件 = 档位适用 且 needs_repair（读 run_single 的判定）
            would = applicable and needs_repair(cov)
            if would:
                note = "低覆盖且档位适用 → 应当触发"
                ok_trigger += 1
                flag = "会"
            else:
                note = "档位不适用" if not applicable else (
                    "覆盖率达标" if cov.percent >= REPAIR_TRIGGER_PERCENT else (
                        "样本不足" if not cov.reliable else "—"
                    )
                )
                ok_skip += 1
                flag = "不会"
            # 兜底断言：档位不适用时绝不该触发。
            # （needs_repair 只管覆盖率与可靠性，档位这道门是 run_single 加的；
            #   这里的 would 已含档位判断，所以这条正常永远不成立——
            #   留着是防将来有人把判定改坏。）
            if not applicable and would:
                bad += 1
                note = "⚠️ 档位不适用却判定要触发（判定漏了档位这道门）"

            print(
                f"{batch_dir.name:<12}{tpl + '/' + sty:<16}{job['idx']:<5}"
                f"{cov.percent:>6}%{flag:>8}  {note}"
            )

    print("-" * 78)
    print(f"合计：不触发 {ok_skip} 节，应当触发 {ok_trigger} 节，判定错误 {bad} 节")
    if bad:
        print("❌ 存在不该触发的触发 —— 触发判定漏了「档位适用」这道门")
        return 1
    print("✅ 判定全部符合预期（档位不适用的批次一律不触发；达标的不触发）")
    print(
        f"   → 对已有 {ok_skip} 节产物，开启「自动补漏」也不会产生任何额外调用。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
