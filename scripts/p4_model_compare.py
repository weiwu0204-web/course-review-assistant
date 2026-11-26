"""P4 · 多模型横评：同一批课件、同一套参数，换模型跑一遍，出「覆盖率 vs 成本」表。

## 为什么值钱

覆盖率是本项目的自有指标（P1 那条管线，纯本地、零额外成本），
**只有手里有真实课件的人才能做这张表**——它回答的是"哪家模型在这个任务上性价比最高"，
而不是"哪家跑分高"。

## 口径必须一致（否则表没意义）

| 项 | 固定为 |
|---|---|
| 课件 | 同一批文件（含同一份往年试题，如果传了） |
| 模板 / 风格 | 复习指南 / 详细（**只有这组才出覆盖率**，习题档本就不追求全覆盖） |
| 视觉 | 关（避免"看图能力差异"混进文本能力对比；要测视觉另开一轮） |
| 补漏 | 关（P2 会自己花钱补，混进来就分不清是模型差异还是补漏差异） |
| 温度 | 不传，用各家默认（这条是实测约束，不是选择：见 P3 温度实验，压温度无效） |

## key 从哪来

应用只存一份 key，横评需要多家，所以用环境变量：
    P4_KEY_DEEPSEEK / P4_KEY_ZHIPU / P4_KEY_KIMI / P4_KEY_QWEN
当前生效的那家若没设环境变量，回退用应用里配的 key（方便先跑一家冒烟）。

用法：
    .venv/Scripts/python.exe scripts/p4_model_compare.py --providers deepseek --limit 2
    .venv/Scripts/python.exe scripts/p4_model_compare.py --providers deepseek,zhipu,kimi,qwen
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AI_PROVIDERS, OUTPUT_ROOT, current_provider_id, effective_provider, get_settings
from app.models import GenerateRequest
from app.services import ai_generator as gen
from app.services.extractor import extract_any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = OUTPUT_ROOT / "_p4_compare"


def provider_cfg(pid: str) -> dict | None:
    """取该服务商的生效配置（有档位的按默认档合并）。"""
    base = AI_PROVIDERS.get(pid)
    if not base:
        return None
    if base.get("tiers"):
        merged, _ = effective_provider(pid, base.get("default_tier", ""))
        return merged or base
    return base


def provider_key(pid: str) -> str:
    key = (os.environ.get(f"P4_KEY_{pid.upper()}") or "").strip()
    if key:
        return key
    if pid == current_provider_id():
        return (get_settings().ai_api_key or "").strip()
    return ""


def run_one(path: Path, settings, template: str = "复习指南") -> dict:
    """单份课件跑一次，返回口径统一的读数。"""
    gen.get_settings = lambda: settings  # type: ignore[assignment]  # 仅本进程内生效
    result = extract_any(path)
    request = GenerateRequest(
        filename=path.name,
        template=template,
        style="详细",
        use_vision=False,  # 关视觉：这轮只比文本能力
        thinking=False,
        enable_repair=False,  # 关补漏：不让 P2 的额外调用混进成本
    )
    t0 = time.perf_counter()
    outcome = gen.run_single(result, request)
    dur = time.perf_counter() - t0
    usage = outcome.usage or {}
    return {
        "file": path.name,
        "coverage": outcome.coverage.percent if outcome.coverage else None,
        "coverage_missing": outcome.coverage.missing_terms if outcome.coverage else None,
        "usage": usage,
        "duration_s": round(dur, 1),
        "chars": len(outcome.markdown or ""),
        "truncated": bool(outcome.truncated),
    }


def cost_of(usage: dict, cfg: dict) -> float:
    pin = float(cfg.get("price_in") or 0)
    pout = float(cfg.get("price_out") or 0)
    return round(
        (int(usage.get("prompt_tokens", 0)) * pin + int(usage.get("completion_tokens", 0)) * pout)
        / 1_000_000,
        4,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="课件")
    ap.add_argument("--providers", default="deepseek", help="逗号分隔：deepseek,zhipu,kimi,qwen")
    ap.add_argument("--limit", type=int, default=0, help="每家只跑前 N 份（0=全部）")
    ap.add_argument("--template", default="复习指南")
    args = ap.parse_args()

    corpus = ROOT / args.corpus
    paths = sorted(p for p in corpus.iterdir() if p.suffix.lower() in {".pptx", ".pdf"})
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        raise SystemExit(f"{corpus} 里没有 .pptx/.pdf")

    base_settings = get_settings()
    rows, skipped = [], []

    for pid in [p.strip() for p in args.providers.split(",") if p.strip()]:
        cfg = provider_cfg(pid)
        key = provider_key(pid)
        if cfg is None or not key:
            skipped.append(f"{pid}（{'未知服务商' if cfg is None else '没有 key：设 P4_KEY_' + pid.upper()}）")
            continue
        settings = base_settings.model_copy(
            update={
                "ai_api_key": key,
                "ai_base_url": cfg["base_url"],
                "ai_model": cfg["model"],
                "ai_vision_model": cfg.get("vision_model") or cfg["model"],
                "ai_thinking_off": cfg.get("thinking_off") or {},
            }
        )
        print(f"\n=== {cfg.get('name', pid)} · {cfg['model']} ===")
        for i, path in enumerate(paths, 1):
            try:
                r = run_one(path, settings, args.template)
            except Exception as exc:  # noqa: BLE001 单份失败不影响其他家
                print(f"  [{i}/{len(paths)}] {path.name[:28]}… ✗ {type(exc).__name__}: {exc}")
                continue
            r.update(
                provider=pid,
                model=cfg["model"],
                cost_yuan=cost_of(r["usage"], cfg),
            )
            rows.append(r)
            cov = "—" if r["coverage"] is None else f"{r['coverage']}%"
            print(
                f"  [{i}/{len(paths)}] {path.name[:28]}… 覆盖 {cov} "
                f"¥{r['cost_yuan']:.4f} {r['duration_s']}s"
            )

    if not rows:
        raise SystemExit("没有任何结果。" + ("跳过：" + "；".join(skipped) if skipped else ""))

    # 汇总
    summary = {}
    for pid in {r["provider"] for r in rows}:
        sub = [r for r in rows if r["provider"] == pid]
        covs = [r["coverage"] for r in sub if r["coverage"] is not None]
        summary[pid] = {
            "model": sub[0]["model"],
            "n": len(sub),
            "coverage_mean": round(statistics.mean(covs), 1) if covs else None,
            "coverage_n": len(covs),
            "cost_total": round(sum(r["cost_yuan"] for r in sub), 4),
            "cost_per_section": round(sum(r["cost_yuan"] for r in sub) / len(sub), 4),
            "duration_median": round(statistics.median([r["duration_s"] for r in sub]), 1),
            "tokens_total": sum(
                int(r["usage"].get("prompt_tokens", 0)) + int(r["usage"].get("completion_tokens", 0))
                for r in sub
            ),
            "truncated": sum(1 for r in sub if r["truncated"]),
        }

    print("\n=== 汇总（同一批课件，覆盖率 vs 成本）===")
    print(f"{'服务商':<10}{'模型':<22}{'份数':>4}{'覆盖率':>8}{'每份¥':>9}{'总¥':>8}{'耗时中位':>10}{'截断':>5}")
    for pid, s in summary.items():
        cov = "—" if s["coverage_mean"] is None else f"{s['coverage_mean']}%"
        print(
            f"{pid:<10}{s['model']:<22}{s['n']:>4}{cov:>8}"
            f"{s['cost_per_section']:>9}{s['cost_total']:>8}{s['duration_median']:>9}s{s['truncated']:>5}"
        )
    if skipped:
        print("\n跳过：" + "；".join(skipped))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "corpus": str(corpus),
                "n_files": len(paths),
                "fixed": {
                    "template": args.template,
                    "style": "详细",
                    "use_vision": False,
                    "enable_repair": False,
                },
                "skipped": skipped,
                "summary": summary,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
