"""给存量批任务回填覆盖率（P1 覆盖率可见化上线前跑的批次没有这个数据）。

## 为什么需要

覆盖率回检是 0.8.5 之后才有的功能。之前跑的批次 `coverage_percent = -1`（未计算），
界面上不显示徽标。重跑一遍要花钱（一门课 ¥2 左右），而 **课件与生成文档都还留在
job 目录里**，所以纯本地重算即可，零成本、不调模型。

## 数据来源（不重新提取、不调模型）

| 用什么 | 从哪来 |
|---|---|
| 源课件提取结果 | `job目录/extracted.json`（**当时实际用的那份**，有则优先） |
| 源课件（兜底） | `job目录/*.pptx`（extracted.json 缺失时现场提取） |
| 生成的正文 | `job目录/doc.md` |

用 extracted.json 而不是重新提取，除了快，更重要的是**与当时跑的保持一致**——
重新提取可能因库版本不同得到略有差异的结果。

## 安全措施

- **默认 --dry-run**：只报告会改什么，不写盘。
- 真正写入前把 `batch.json` 备份为 `batch.json.bak-<时间戳>`。
- 已有 `coverage.json` 的 job 默认跳过（要重算加 `--force`）。

## 用法

    # 先空跑看一遍（推荐）
    .venv/Scripts/python.exe scripts/backfill_coverage.py --dry-run

    # 只处理某一批
    .venv/Scripts/python.exe scripts/backfill_coverage.py --batch 8e9dc5ac

    # 实际写入
    .venv/Scripts/python.exe scripts/backfill_coverage.py
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _coverage_io import load_job_extraction  # noqa: E402
from app.services import batch_runner as br  # noqa: E402
from app.services.coverage import (  # noqa: E402
    MIN_TERMS_FOR_PERCENT,
    applies_to,
    measure,
    verdict,
)

BACKUP_SUFFIX_FMT = ".bak-%Y%m%d-%H%M%S"


def backfill_job(job: br.JobRecord, batch: br.Batch, force: bool) -> dict:
    """给单个 job 算覆盖率。返回 {status, percent|reason, ...}，不写盘。"""
    d = batch.job_dir_of(job)
    if job.status != br.STATUS_DONE:
        return {"status": "skip", "reason": f"状态是 {job.status}，未完成"}
    if (d / br.COVERAGE_JSON).exists() and not force:
        return {"status": "skip", "reason": "已有覆盖率（加 --force 可重算）"}
    doc = d / br.DOC_MD
    if not doc.exists():
        return {"status": "skip", "reason": "没有 doc.md"}
    if not d.exists():
        return {"status": "skip", "reason": "job 目录不存在"}

    try:
        result = load_job_extraction(d)
    except Exception as exc:  # noqa: BLE001
        return {"status": "skip", "reason": f"提取结果不可用：{type(exc).__name__}"}
    if result is None:
        return {"status": "skip", "reason": "找不到课件文件"}

    rep = measure(result, br.read_text_retry(doc))
    return {
        "status": "ok",
        "report": rep,
        "percent": rep.percent,
        "total": rep.total_terms,
        "missing": rep.missing_terms,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="给存量批任务回填覆盖率（纯本地，零成本）")
    ap.add_argument("--output-root", default=str(br.OUTPUT_ROOT),
                    help="批任务产物根目录（默认项目 output/）")
    ap.add_argument("--batch", default="", help="只处理指定 batch_id（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写盘（建议先跑这个）")
    ap.add_argument("--force", action="store_true", help="已有覆盖率的也重算")
    args = ap.parse_args()

    root = Path(args.output_root)
    if not root.exists():
        print(f"[错误] 产物目录不存在：{root}")
        return 1

    batch_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if args.batch:
        batch_dirs = [p for p in batch_dirs if p.name == args.batch]
        if not batch_dirs:
            print(f"[错误] 找不到批任务：{args.batch}")
            return 1

    print(f"扫描 {root}（{len(batch_dirs)} 个目录）"
          f"{'  [空跑模式，不写盘]' if args.dry_run else ''}\n")

    total_ok = total_skip = total_batch_skip = 0
    for bd in batch_dirs:
        try:
            batch = br.load_batch(root, bd.name)
        except Exception as exc:  # noqa: BLE001
            print(f"— {bd.name}: 读取失败（{type(exc).__name__}），跳过")
            continue

        tpl = batch.options.get("template", "复习指南")
        sty = batch.options.get("style", "详细")
        print(f"— 《{batch.course_name or bd.name}》 ({bd.name})  [{tpl}/{sty}]")
        if not applies_to(tpl, sty):
            # 不是「复习指南/详细」就不算：这些档位本就不追求全覆盖，算了会得出
            # 58% 这类吓人且错误的数字（实测用户存量批次就是如此）。
            print("    · 跳过：该档位不追求完整覆盖，覆盖率不适用\n")
            total_batch_skip += 1
            continue
        changed = False
        for job in batch.jobs_in_order():
            r = backfill_job(job, batch, args.force)
            label = f"第{job.idx}节 {job.name[:34]}"
            if r["status"] == "skip":
                total_skip += 1
                print(f"    · {label:<46} 跳过：{r['reason']}")
                continue

            rep = r["report"]
            v = verdict(rep.percent)
            flag = "" if rep.total_terms >= MIN_TERMS_FOR_PERCENT else "（术语过少，界面显示样本不足）"
            print(f"    · {label:<46} {rep.percent:>3}%  术语 {rep.total_terms}"
                  f" 缺 {rep.missing_terms}  {v}{flag}")
            total_ok += 1
            changed = True

            if not args.dry_run:
                d = batch.job_dir_of(job)
                (d / br.COVERAGE_JSON).write_text(
                    _json(rep.to_dict()), encoding="utf-8"
                )
                job.coverage_percent = rep.percent
                job.coverage_missing = rep.missing_terms
                job.coverage_total = rep.total_terms

        if changed and not args.dry_run:
            bak = bd / (br.BATCH_JSON + time.strftime(BACKUP_SUFFIX_FMT))
            shutil.copyfile(bd / br.BATCH_JSON, bak)
            br.persist_batch(batch)
            br.rebuild_master(batch)   # 总册只在意「附覆盖率自查」开关是否打开
            print(f"    → 已写回 batch.json（备份：{bak.name}）")
        elif changed:
            print("    → 空跑，未写盘")
        print()

    print(f"完成：{total_ok} 份已计算，{total_skip} 份跳过"
          + (f"，{total_batch_skip} 个批次因档位不适用整体跳过" if total_batch_skip else ""))
    if args.dry_run and total_ok:
        print("确认无误后去掉 --dry-run 再跑一次即可写入。")
    return 0


def _json(obj) -> str:
    import json as _json_mod
    return _json_mod.dumps(obj, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
