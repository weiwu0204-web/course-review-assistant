"""可观测（P4）：把散落在磁盘上的作业记录汇成一张统计表。

## 为什么不新建数据库

数据本来就在磁盘上，而且**已经是结构化 JSON**：
- `output/<batch_id>/batch.json` —— 每份课件的 token / 成本 / 覆盖率 / 耗时
- `output/qa/<kb_id>.jsonl` —— 每次提问的分项耗时、token、引用核验结果

再建一张表只是把同一份事实抄一遍，还要处理"两边不一致听谁的"。
所以这里**只做读+聚合**，零写入、零迁移，旧数据（缺字段）也能直接读。

## 三条统计纪律

1. **缺字段 ≠ 零**。旧 batch.json 没有 `prompt_tokens`（那时还没埋点），
   加载出来是 0。把 0 算进 token 均值会把结果拉低——所以同时报「有记录份数」，
   0 的样本被排除在均值外，而不是当成"花了 0 token"。
2. **覆盖率只在算得出来的份上统计**。`coverage_percent = -1` 表示"没算/不适用"
   （习题档、术语太少），把它当 0% 会让覆盖率凭空变差。这和 P1 的判据纪律同源。
3. **估算成本与实测成本分开计**。服务商没回 usage 时是估算值，
   混进"实测成本"里会让成本曲线失真。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.config import OUTPUT_ROOT

BATCH_JSON = "batch.json"
QA_DIRNAME = "qa"


def _percentile(values: list[float], q: float) -> float | None:
    """线性插值分位数（不引 numpy：这里最多几百个样本，sort 一下足够）。"""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return round(data[0], 1)
    pos = (len(data) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(data) - 1)
    val = data[lo] + (data[hi] - data[lo]) * (pos - lo)
    return round(val, 1)


def _iter_batch_files(root: Path):
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        path = entry / BATCH_JSON
        if not path.is_file():
            continue
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 坏文件跳过：统计页不该因为一个半截文件整页空白


def generation_stats(root: Path = OUTPUT_ROOT) -> dict:
    jobs: list[dict] = []
    courses: dict[str, list[dict]] = {}
    n_batches = 0

    for batch in _iter_batch_files(root):
        n_batches += 1
        course = (batch.get("course_name") or "").strip() or "（未命名批次）"
        for job in batch.get("jobs") or []:
            row = {
                "course": course,
                "batch_id": batch.get("batch_id", ""),
                "status": job.get("status", ""),
                "name": job.get("name", ""),
                "model": job.get("model", "") or (batch.get("provider") or {}).get("model", ""),
                "cost": float(job.get("cost_yuan") or 0.0),
                "cost_is_estimate": bool(job.get("cost_is_estimate", True)),
                "prompt_tokens": int(job.get("prompt_tokens") or 0),
                "completion_tokens": int(job.get("completion_tokens") or 0),
                "duration_s": float(job.get("duration_s") or 0.0),
                "coverage": int(job.get("coverage_percent", -1)),
                "total_pages": int(job.get("total_pages") or 0),
            }
            jobs.append(row)
            courses.setdefault(course, []).append(row)

    done = [j for j in jobs if j["status"] == "done"]
    with_tokens = [j for j in done if j["prompt_tokens"] > 0]
    with_cov = [j for j in done if j["coverage"] >= 0]
    with_dur = [j for j in done if j["duration_s"] > 0]
    measured_cost = [j for j in done if not j["cost_is_estimate"]]

    by_status = {}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1

    course_rows = []
    for name, rows in courses.items():
        d = [r for r in rows if r["status"] == "done"]
        cov = [r["coverage"] for r in d if r["coverage"] >= 0]
        tok = [r["prompt_tokens"] + r["completion_tokens"] for r in d if r["prompt_tokens"] > 0]
        course_rows.append(
            {
                "course": name,
                "n_jobs": len(rows),
                "n_done": len(d),
                "cost_yuan": round(sum(r["cost"] for r in d), 4),
                "tokens": sum(tok) if tok else 0,
                "coverage_mean": round(sum(cov) / len(cov), 1) if cov else None,
                "coverage_n": len(cov),
                "duration_s_median": _percentile([r["duration_s"] for r in d if r["duration_s"]], 0.5),
                "models": sorted({r["model"] for r in d if r["model"]}),
            }
        )
    course_rows.sort(key=lambda r: -r["n_jobs"])

    return {
        "n_batches": n_batches,
        "n_jobs": len(jobs),
        "by_status": by_status,
        "cost_yuan": round(sum(j["cost"] for j in done), 4),
        "cost_measured_yuan": round(sum(j["cost"] for j in measured_cost), 4),
        "cost_estimated_jobs": len(done) - len(measured_cost),
        "tokens": {
            "covered_jobs": len(with_tokens),
            "prompt": sum(j["prompt_tokens"] for j in with_tokens),
            "completion": sum(j["completion_tokens"] for j in with_tokens),
        },
        "coverage": {
            "n": len(with_cov),
            "mean": round(sum(j["coverage"] for j in with_cov) / len(with_cov), 1)
            if with_cov
            else None,
            "p50": _percentile([j["coverage"] for j in with_cov], 0.5),
            "p90": _percentile([j["coverage"] for j in with_cov], 0.9),
            "below_94": sum(1 for j in with_cov if j["coverage"] < 94),
        },
        "duration_s": {
            "n": len(with_dur),
            "p50": _percentile([j["duration_s"] for j in with_dur], 0.5),
            "p90": _percentile([j["duration_s"] for j in with_dur], 0.9),
        },
        "courses": course_rows,
    }


def qa_stats(root: Path = OUTPUT_ROOT) -> dict:
    qa_dir = root / QA_DIRNAME
    records: list[dict] = []
    if qa_dir.exists():
        for path in sorted(qa_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                continue

    def timed(records, key):
        vals = [
            (r.get("timings") or {}).get(key)
            for r in records
            if (r.get("timings") or {}).get(key) is not None
        ]
        return [v for v in vals if isinstance(v, (int, float))]

    first = timed(records, "first_token_ms")
    rewrite = timed(records, "rewrite_ms")
    total = timed(records, "total_ms")
    cites = [r.get("citations") or {} for r in records]
    n_cites = sum(int(c.get("total", 0) or 0) for c in cites)
    n_sup = sum(int(c.get("supported", 0) or 0) for c in cites)

    return {
        "n_questions": len(records),
        "cost_yuan": round(sum(float(r.get("cost_yuan") or 0) for r in records), 4),
        "first_token_ms": {
            "n": len(first),
            "p50": _percentile(first, 0.5),
            "p90": _percentile(first, 0.9),
        },
        "rewrite_ms": {"n": len(rewrite), "p50": _percentile(rewrite, 0.5)},
        "total_ms": {"n": len(total), "p50": _percentile(total, 0.5)},
        "citations": {
            "total": n_cites,
            "supported": n_sup,
            "pass_rate": round(n_sup / n_cites * 100, 1) if n_cites else None,
        },
        "rewrite_applied": sum(1 for r in records if r.get("rewrite_applied")),
        "no_hit": sum(1 for r in records if r.get("no_hit")),
    }


def collect(root: Path = OUTPUT_ROOT) -> dict:
    """统计页的完整数据。一次读盘，不做缓存（几百个文件，几十毫秒）。"""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generation": generation_stats(root),
        "qa": qa_stats(root),
    }
