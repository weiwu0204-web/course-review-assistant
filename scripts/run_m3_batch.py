"""M3 验证驱动：把「课件/」整门课按讲课顺序建成一个批任务，跟踪到结束并落汇总。

为什么要有这个脚本：
  M3 要求「14 份一次跑完 + 留量化记录」。用手点界面没法把顺序固定成讲课顺序
  （目录是字母序），也不方便把耗时/花费/截断数原样存下来。这里走的是同一个
  HTTP 接口（/api/batch/start、/api/batch/{id}），与界面路径完全一致，
  所以数据等价于真实使用，不是另开一条旁路。

用法（先另起一个终端把服务跑起来）：
    .venv/Scripts/python.exe -m uvicorn app.main:app --port 8129
    .venv/Scripts/python.exe scripts/run_m3_batch.py --port 8129

产物：output/_m3_report.json（汇总 + 逐份明细），可据此填 docs/M3-VALIDATION.md 的记录表 A。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COURSE_DIR = PROJECT_ROOT / "课件"

# 重定向到文件/管道时 stdout 默认是块缓冲，进度要跑完才刷出来——强制行缓冲，
# 后台跑的时候才看得到实时进度（踩过一次）。
sys.stdout.reconfigure(line_buffering=True)

# 讲课顺序：从课件首页自带的「第 N 节 / MODULE 0N」标记读出，不是目录的字母序。
COURSE_ORDER = [
    "Ceph 分布式存储实战：分布式存储概述与 Ceph 架构.pptx",   # 第 1 节
    "Ceph 分布式存储实战：Ceph 核心组件深度解析.pptx",        # 第 2 节
    "Ceph 分布式存储实战：集群规划与硬件要求.pptx",            # 第 3 节
    "Ceph 分布式存储实战：cephadm 部署全攻略.pptx",            # 第 4 节
    "Ceph 块存储（RBD）配置与使用.pptx",                      # 第 5 节
    "Ceph 对象存储（RGW）配置与使用.pptx",                    # 第 6 节
    "Ceph 文件系统（CephFS）配置与使用.pptx",                 # 第 7 节
    "Ceph 集群监控与告警.pptx",                               # 第 8 节
    "Ceph 性能调优基础.pptx",                                 # 第 9 节
    "Ceph 常见故障排查：OSD 故障.pptx",                       # 第 10 节
    "Ceph 集群扩容与缩容.pptx",                               # 第 11 节
    "Ceph 数据备份与恢复.pptx",                               # 第 12 节
    "Ceph 与 Kubernetes 集成 (CSI).pptx",                     # 第 13 节
    "第14节：Ceph 集群安全配置.pptx",                         # 第 14 节
]

COURSE_NAME = "Ceph 分布式存储"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8129)
    ap.add_argument("--course", default=COURSE_NAME)
    ap.add_argument("--template", default="复习指南")
    ap.add_argument("--style", default="详细")
    ap.add_argument("--no-vision", action="store_true", help="关闭视觉（省钱但质量可能下降）")
    ap.add_argument("--timeout-min", type=int, default=60, help="等待上限（分钟）")
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    missing = [n for n in COURSE_ORDER if not (COURSE_DIR / n).exists()]
    if missing:
        print("[错误] 这些课件找不到：")
        for m in missing:
            print("   ", m)
        print(f"（实际目录：{COURSE_DIR}）")
        return 1

    files = []
    for name in COURSE_ORDER:
        p = COURSE_DIR / name
        files.append(("files", (name, p.read_bytes(), "application/octet-stream")))

    print(f"建批：{len(files)} 份，课程名《{args.course}》，{args.template}/{args.style}，"
          f"视觉={'关' if args.no_vision else '开'}")
    started = datetime.now()
    t0 = time.time()
    with httpx.Client(timeout=600.0) as c:
        r = c.post(f"{base}/api/batch/start", files=files, data={
            "template": args.template,
            "style": args.style,
            "use_vision": "false" if args.no_vision else "true",
            "thinking": "false",
            "course_name": args.course,
        })
        if r.status_code != 200:
            print(f"[错误] 建批失败 HTTP {r.status_code}：{r.text[:300]}")
            return 1
        d = r.json()
        bid = d["batch_id"]
        print(f"批 id = {bid}｜共 {d['total']} 份，预估 ¥{d.get('est_total_yuan', 0)}\n")

        last = ""
        deadline = time.time() + args.timeout_min * 60
        while True:
            s = c.get(f"{base}/api/batch/{bid}").json()
            line = (f"{s['status']:<9} {s['done']}/{s['total']} 完成  "
                    f"花费 ¥{s.get('spent_yuan', 0):.4f}")
            if line != last:
                print(f"[{datetime.now():%H:%M:%S}] {line}")
                last = line
            if s["status"] != "running":
                break
            if time.time() > deadline:
                print("[超时] 到达等待上限，停止跟踪（任务在服务端仍会继续）")
                break
            time.sleep(10)

        final = c.get(f"{base}/api/batch/{bid}").json()
        master = c.get(f"{base}/api/batch/{bid}/master")
        master_chars = len(master.text) if master.status_code == 200 else 0

    elapsed = time.time() - t0
    jobs = final["jobs"]
    truncated = [j for j in jobs if j.get("warning") == "truncated"]
    report = {
        "batch_id": bid,
        "course_name": args.course,
        "params": {"template": args.template, "style": args.style,
                   "vision": not args.no_vision, "thinking": False},
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(elapsed, 1),
        "elapsed_min": round(elapsed / 60, 1),
        "status": final["status"],
        "total": final["total"],
        "done": final["done"],
        "error": final["error"],
        "spent_yuan": final["spent_yuan"],
        "est_total_yuan": final.get("est_total_yuan", 0),
        "truncated_count": len(truncated),
        "truncated_files": [t["name"] for t in truncated],
        "master_chars": master_chars,
        "master_ready": final.get("master_ready"),
        "jobs": [
            {
                "idx": j["idx"], "name": j["name"], "status": j["status"],
                "total_pages": j.get("total_pages"), "image_count": j.get("image_count"),
                "cost_yuan": j.get("cost_yuan"), "warning": j.get("warning", ""),
                "error": j.get("error", ""),
            }
            for j in jobs
        ],
    }
    out = PROJECT_ROOT / "output" / "_m3_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 62)
    print(f"状态 {report['status']}｜成功 {report['done']}/{report['total']}｜失败 {report['error']}")
    print(f"耗时 {report['elapsed_min']} 分钟｜实际花费 ¥{report['spent_yuan']}"
          f"｜预估 ¥{report['est_total_yuan']}")
    print(f"截断 {report['truncated_count']} 份｜总册 {master_chars} 字")
    print(f"汇总已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
