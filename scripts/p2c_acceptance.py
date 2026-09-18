"""P2c 验收：边际成本曲线 + 补丁汇总（供防凑词人工抽查）。

对应 docs/P2-REPAIR.md §5 的验收标准 4（边际成本曲线）与 5（防凑词抽查）。

做法：拿一份**真实产物**，用几个不同的削弱档位制造不同的缺失程度，各跑一次真 repair，
记录「每提升 1 个百分点花多少钱」。同一份素材、只变缺失程度，曲线才有可比性。

注意削弱是在**内存里**做的，不动任何已有产物；过程文件写在 output/_p2c/ 下。

用法：
    .venv/Scripts/python.exe scripts/p2c_acceptance.py            # 跑（会真花钱，约 ¥0.1）
    .venv/Scripts/python.exe scripts/p2c_acceptance.py --dry-run  # 只看各档位构造结果
"""

from __future__ import annotations

import argparse
import difflib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OUTPUT_ROOT, current_provider, get_settings  # noqa: E402
from app.services.agent_repair import needs_repair, repair_document  # noqa: E402
from app.services.coverage import measure  # noqa: E402
from smoke_repair import _pick_job, _weaken  # noqa: E402

# 1.0 = 不削弱（真实文档）；数值越小缺失越多。
# 这份文档小节数不多，档位过密会得到相同的起点，所以取跨度大的三档。
RATIOS = [1.0, 0.50]
OUT_DIR = OUTPUT_ROOT / "_p2c"


def _added_text(before: str, after: str) -> str:
    """取补入的部分（差分里的新增行），供防凑词抽查。"""
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0
    )
    return "\n".join(l[1:] for l in diff if l.startswith("+") and not l.startswith("+++"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", default="398cceda")
    ap.add_argument("--idx", type=int, default=2)
    args = ap.parse_args()

    job_dir, ext, base_doc, tpl, sty, gen_cost = _pick_job(args.batch, args.idx)
    settings = get_settings()
    prov = current_provider() or {}
    print(f"素材：{job_dir}  [{tpl}/{sty}]  生成成本 ¥{gen_cost:.4f}")
    print(f"服务商：{prov.get('name', '?')} / {settings.ai_model}\n")

    if not settings.ai_api_key:
        print("未配置 API Key，无法真实调用。")
        return 2

    rows: list[dict] = []
    all_patches: list[str] = []

    for ratio in RATIOS:
        weakened = base_doc if ratio >= 1 else _weaken(base_doc, ratio)
        cov0 = measure(ext, weakened)
        label = "真实文档（未削弱）" if ratio >= 1 else f"削弱 {ratio:.2f}"
        line = f"{label} → 起点覆盖率 {cov0.percent}%（缺 {cov0.missing_terms}）"
        if args.dry_run:
            print(line)
            continue

        # 没到触发阈值就跳过——repair 本就不该在这种文档上跑（这本身也是一条验证）
        if not needs_repair(cov0):
            print(f"{line}  → 跳过（未达触发条件，repair 不该跑）")
            continue

        workdir = OUT_DIR / f"r{int(ratio * 100):03d}"
        t0 = time.perf_counter()
        doc, rec = repair_document(
            ext, weakened, cov0,
            workdir=workdir,
            base_cost_yuan=gen_cost,
            provider=prov,
        )
        secs = time.perf_counter() - t0
        gained = max(0, rec.final_percent - rec.trigger_percent)
        per_pp = (rec.total_cost_yuan / gained) if gained else None
        rows.append({
            "ratio": ratio, "start": rec.trigger_percent, "end": rec.final_percent,
            "gained": gained, "rounds": len(rec.rounds), "cost": rec.total_cost_yuan,
            "per_pp": per_pp, "secs": secs, "stop": rec.stop_reason,
            "rolled_back": rec.rolled_back,
        })
        print(f"{line}  →  {rec.final_percent}%  "
              f"（{len(rec.rounds)} 轮，¥{rec.total_cost_yuan}，{secs:.1f}s，{rec.stop_reason}）")
        if not rec.rolled_back:
            all_patches.append(
                f"\n## 档位 {ratio}（{rec.trigger_percent}% → {rec.final_percent}%）\n\n"
                + _added_text(weakened, doc)
            )
        # 每轮明细
        for r in rec.rounds:
            print(f"      第{r.round}轮：工具 {r.tool_calls} 次 / 补丁 {r.patches_applied} 个 / "
                  f"{r.percent_before}% → {r.percent_after}% / ¥{r.cost_yuan}")

    if args.dry_run:
        print("\n--dry-run：未调用模型")
        return 0

    print("\n===== 边际成本曲线 =====")
    print(f"{'起点':>6}{'终点':>7}{'提升':>6}{'轮次':>6}{'成本':>10}{'元/百分点':>11}{'耗时':>8}")
    print("-" * 60)
    for r in rows:
        per = f"{r['per_pp']:.5f}" if r["per_pp"] else "—"
        print(f"{r['start']:>5}%{r['end']:>6}%{r['gained']:>5}pp{r['rounds']:>6}"
              f"{r['cost']:>10.4f}{per:>11}{r['secs']:>7.1f}s")
    total_cost = sum(r["cost"] for r in rows)
    total_gain = sum(r["gained"] for r in rows)
    print("-" * 60)
    print(f"合计：¥{total_cost:.4f} / {total_gain}pp → 平均 "
          f"{(total_cost / total_gain):.5f} 元/百分点" if total_gain else "无提升")
    ratio_avg = [r["cost"] / gen_cost * 100 for r in rows]
    print(f"成本占该节生成成本：{'/'.join(f'{x:.0f}%' for x in ratio_avg)}"
          f"（验收标准：≤40%）")

    if all_patches:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        f = OUT_DIR / "patches.md"
        f.write_text(
            "# 补漏内容汇总（P2c 防凑词抽查用）\n\n"
            "判断标准：**每条是否为「术语 + 实质说明」**——有定义/功能/约束/举例即为合格；\n"
            "只把术语名写进去、没有任何解释即为凑词（不合格）。\n"
            + "\n".join(all_patches),
            encoding="utf-8",
        )
        print(f"\n补入内容已汇总：{f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
