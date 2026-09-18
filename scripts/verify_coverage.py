"""覆盖率体检 / 回归验收：对一份或多份「课件 ↔ 成品文档」算覆盖率并做基准断言。

## 用途

1. **体检**：看某个批任务每一节的覆盖率，定位哪一节薄。
2. **回归验收**：改了判据、换了模型、调整了提示词之后，跑一遍看有没有退化
   （`--expect` 超出容差即退出码 1，可接进 CI 或提交前手动跑）。
3. **复现 M3 对照**：`--compare` 拿一份外部生成的文档与同一批课件逐节比对，
   复现「通用 agent 后半程掉队」那条结论。

**只读**：不写任何文件、不调模型、零成本。

## M3 基准（2026-09-13/14 实测，复习指南 / 详细，Ceph 课 14 节）

| 对象 | 平均 | 前 7 节 | 后 7 节 |
|---|---|---|---|
| 本项目总册（各节 vs 本节 doc） | **97.7%** | 98.6% | 96.9% |
| 通用 agent 的整门课文档 | 89.5% | 95.1% | **83.9%** |

关键不在平均值，而在**后半程**：本项目几乎不衰减（-1.7 点），通用 agent 掉 11.2 点。

## 用法

    # 体检某个批任务
    .venv/Scripts/python.exe scripts/verify_coverage.py --batch 8e9dc5ac

    # 回归：平均覆盖率不得低于 94.7%（= 97.7 - 3）
    .venv/Scripts/python.exe scripts/verify_coverage.py --batch 8e9dc5ac --expect 97.7 --tol 3

    # 复现 M3 对照：拿外部文档与同一批课件比
    .venv/Scripts/python.exe scripts/verify_coverage.py --batch 8e9dc5ac --compare Ceph期末复习文档.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _coverage_io import halves_summary, load_job_extraction  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import batch_runner as br  # noqa: E402
from app.services.coverage import applies_to, measure, verdict  # noqa: E402

# M3 实测基准（改判据时应当同步更新，并在 CHANGELOG 写明为什么变）
BASELINE = {
    "ours_avg": 97.7,
    "ours_front": 98.6,
    "ours_back": 96.9,
    "opponent_avg": 89.5,
    "opponent_front": 95.1,
    "opponent_back": 83.9,
}


def measure_batch(batch: br.Batch, doc_override: str = "") -> tuple[list[dict], list[str]]:
    """逐节算覆盖率。doc_override 非空时用同一份外部文档比所有节（复现对照）。"""
    rows: list[dict] = []
    notes: list[str] = []
    for job in batch.jobs_in_order():
        if job.status != br.STATUS_DONE:
            continue
        d = batch.job_dir_of(job)
        if doc_override:
            doc = doc_override
        else:
            doc_path = d / br.DOC_MD
            if not doc_path.exists():
                notes.append(f"第 {job.idx} 节：没有 doc.md，跳过")
                continue
            doc = br.read_text_retry(doc_path)
        try:
            result = load_job_extraction(d)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"第 {job.idx} 节：提取结果不可用（{type(exc).__name__}），跳过")
            continue
        if result is None:
            notes.append(f"第 {job.idx} 节：找不到课件文件，跳过")
            continue
        rep = measure(result, doc)
        rows.append({
            "idx": job.idx, "name": Path(job.name).stem,
            "percent": rep.percent, "total": rep.total_terms,
            "missing": rep.missing_terms, "reliable": rep.reliable,
        })
    return rows, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="覆盖率体检 / 回归验收（只读、零成本）")
    ap.add_argument("--output-root", default=str(br.OUTPUT_ROOT))
    ap.add_argument("--batch", required=True, help="要体检的 batch_id")
    ap.add_argument("--compare", default="", help="拿这份外部文档与同一批课件比对（复现对照）")
    ap.add_argument("--expect", type=float, default=None,
                    help="期望的平均覆盖率；低于 expect-tol 即判定回归")
    ap.add_argument("--tol", type=float, default=3.0, help="容差（百分点，默认 3）")
    ap.add_argument("--show-missing", type=int, default=0,
                    help="每节额外列出前 N 条「疑似未覆盖」（默认 0）")
    args = ap.parse_args()

    root = Path(args.output_root)
    try:
        batch = br.load_batch(root, args.batch)
    except FileNotFoundError:
        print(f"[错误] 找不到批任务 {args.batch}（在 {root} 下）")
        return 1

    tpl = batch.options.get("template", "复习指南")
    sty = batch.options.get("style", "详细")
    tag = "【对照外部文档】" if args.compare else ""
    print(f"批任务 {batch.batch_id}《{batch.course_name}》 [{tpl}/{sty}] {tag}")
    print(f"总册基准（M3 实测）：平均 {BASELINE['ours_avg']}%"
          f"｜前 7 节 {BASELINE['ours_front']}%｜后 7 节 {BASELINE['ours_back']}%")

    if not applies_to(tpl, sty):
        # 与产品口径一致：这些档位本就不追求全覆盖，数字没有意义，不能拿来判定
        print(f"\n[跳过] 「{tpl} / {sty}」档不追求覆盖课件全部知识点，覆盖率不适用"
              "（详见 docs/P1-COVERAGE.md §2.4）。")
        return 0

    doc_override = ""
    if args.compare:
        p = Path(args.compare)
        if not p.exists():
            print(f"[错误] 找不到对照文档 {args.compare}")
            return 1
        doc_override = p.read_text(encoding="utf-8")

    if args.compare:
        # 外部文档 + 内部各节 doc：两份并排，看差距与衰减
        ours_rows, notes = measure_batch(batch)
        theirs_rows, _ = measure_batch(batch, doc_override=doc_override)
        if not ours_rows:
            print("[错误] 没有可比较的节")
            return 1
        print(f"\n{'节':<4}{'页/术语':>9}{'本项目':>9}{'对照':>8}{'差值':>8}")
        print("-" * 40)
        for a, b in zip(ours_rows, theirs_rows):
            print(f"{a['idx']:<4}{a['total']:>9}{a['percent']:>8}%{b['percent']:>7}%"
                  f"{a['percent'] - b['percent']:>+8}")
        o = [r["percent"] for r in ours_rows]
        t = [r["percent"] for r in theirs_rows]
        print("-" * 40)
        print(f"本项目：{halves_summary(o)}")
        print(f"对 照：{halves_summary(t)}")
        print(f"\n基准对照（M3）：本项目 平均 {BASELINE['ours_avg']}%（前 {BASELINE['ours_front']}"
              f"/后 {BASELINE['ours_back']}）· 对照片 平均 {BASELINE['opponent_avg']}%"
              f"（前 {BASELINE['opponent_front']}/后 {BASELINE['opponent_back']}）")
        print("关键看**后半程**：本项目几乎不衰减，通用 agent 会明显掉队。")
    else:
        rows, notes = measure_batch(batch)
        if not rows:
            print("[错误] 没有已完成的节")
            return 1
        print(f"\n{'节':<4}{'课件':<30}{'术语':>6}{'覆盖率':>8}  判定")
        print("-" * 64)
        for r in rows:
            note = "" if r["reliable"] else "（术语过少，不计算）"
            print(f"{r['idx']:<4}{r['name'][:28]:<30}{r['total']:>6}{r['percent']:>7}%  "
                  f"{verdict(r['percent'])}{note}")
            if args.show_missing:
                rep_note = ""
                print(f"{'':<8}疑似未覆盖前 {args.show_missing} 条："
                      f"{rep_note or '（用 --show-missing 需配合单节查看，见 coverage.json）'}")
        print("-" * 64)
        print(halves_summary([r["percent"] for r in rows]))

        if args.expect is not None:
            avg = sum(r["percent"] for r in rows) / len(rows)
            floor = args.expect - args.tol
            ok = avg >= floor
            print(f"\n回归判定：实测 {avg:.1f}% vs 期望 {args.expect}%"
                  f"（容差 {args.tol} → 下限 {floor:.1f}%）  "
                  f"{'PASS' if ok else 'FAIL：疑似退化，请核对判据或提示词改动'}")
            if not ok:
                return 1

    if notes:
        print("\n跳过项：")
        for n in notes:
            print(f"  · {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
