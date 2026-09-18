"""批任务编排器：把「一份 PPT → 一份复习文档」单跑 N 遍，带进度/断点/取消/熔断。

这是 P0 批量生成（见 docs/P0-ARCHITECTURE.md）的服务端核心：map 阶段的调度层。
设计要点：

- **与单份共用同一实现**：generate_fn 默认就是 ai_generator.run_single——批与
  /api/generate 不会出现两套生成逻辑；extract_fn 默认 extract_any（按后缀分发
  到 extract_pptx / extract_pdf）。
- **串行执行**：免费档/共享池限流下并发只会放大 429；3–10 份在桌面单机可接受。
- **每完成一个 job 立即原子落盘 batch.json**（写 .tmp 再 replace）：进程被杀/断电后
  load_batch() 能恢复现场，status=running 的悬空 job 重跑即可（幂等，失败请求不计费）。
- **job 级失败不阻塞整批**，但按错误类别做整批决策：
  balance（余额/额度）→ 剩余 job 全部标 error，不白烧；network 连续 ≥2 → 自动暂停，
  避免「网络断了还在空跑」；其余 → 仅当前 job 记错。
- **费用**：outcome 带 usage 时按实际 token × provider 快照单价记账；服务商没返回
  usage 则回退 prepare 阶段算好的估算值（est_cost）。

本模块刻意不依赖 FastAPI/线程调度细节（start() 只是 daemon 线程壳），
run() 可被测试同步调用；extract_fn / generate_fn 可注入假实现，测状态机不真调模型。
"""
from __future__ import annotations

import inspect
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import OUTPUT_ROOT as CONFIG_OUTPUT_ROOT
from app.models import ExtractionResult, GenerateRequest
from app.services.ai_generator import GenerateOutcome, run_single
from app.services.cost_estimate import estimate_generation_cost
from app.services.coverage import MIN_TERMS_FOR_PERCENT, render_report_section
from app.services.errors import classify_error, friendly_ai_error
from app.services.extractor import PptExtractionError, extract_any
from app.services.reducer import build_master_doc, extract_headings

# 状态常量：job / batch 共用的取值为 batch 层加了 done/partial/cancelled/paused
STATUS_WAITING = "waiting"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
BATCH_DONE = "done"
BATCH_PARTIAL = "partial"
BATCH_CANCELLED = "cancelled"
BATCH_PAUSED = "paused"
BATCH_RUNNING = "running"

# 网络类错误连续出现达到该次数 → 自动暂停整批（避免断网时空跑剩余 job）
NETWORK_FAIL_LIMIT = 2

# 批任务默认输出根目录：项目根/output（打包轮与 uploads/ 一起迁到可写用户目录，见 PACKAGING.md）
# 产物根目录统一由 config 定义（ai_generator / agent_repair 用同一个）
OUTPUT_ROOT = CONFIG_OUTPUT_ROOT

# 落盘文件名
BATCH_JSON = "batch.json"
EXTRACTED_JSON = "extracted.json"
DOC_MD = "doc.md"
META_JSON = "meta.json"
COVERAGE_JSON = "coverage.json"
MASTER_MD = "总复习册-{course}.md"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def slugify(name: str) -> str:
    """把课件文件名变成安全的目录名片段（去扩展名、去非法字符、限长）。"""
    stem = Path(name).stem.strip()
    cleaned = "".join(c if (c.isalnum() or c in "-_. ") else "_" for c in stem)
    return cleaned[:60].rstrip(" .") or "课件"


def batch_dir(output_root: str | Path, batch_id: str) -> Path:
    return Path(output_root) / batch_id


def job_dir(output_root: str | Path, batch_id: str, idx: int, slug: str) -> Path:
    return Path(output_root) / batch_id / f"{idx:02d}_{slug}"


# ---------------------------------------------------------------- 状态对象

@dataclass
class JobRecord:
    """单个课件（job）的输入元信息 + 运行态。可直接 JSON 序列化。"""

    idx: int
    name: str
    slug: str
    src_pptx: str  # 绝对路径
    status: str = STATUS_WAITING
    total_pages: int = 0
    image_count: int = 0
    chars: int = 0
    notes_chars: int = 0
    est_cost: float = 0.0  # prepare 阶段按字数/图片粗估（单份口径）
    cost_yuan: float = 0.0  # 完成时：usage 实算或回退 est_cost
    cost_is_estimate: bool = True
    error: str = ""
    warning: str = ""  # 如 truncated
    # 覆盖率回检（见 services/coverage.py）。-1 表示未计算：旧任务没有该字段，
    # 或计算失败被兜住了——前端据此决定要不要显示，不要显示成 0%。
    coverage_percent: int = -1
    coverage_missing: int = 0
    # 术语总数：用来判断百分比是否可信（太少时百分比是噪声，见 coverage.reliable）。
    # 只存百分比的话，事后无法区分「覆盖 0%（真的全漏）」和「样本太小算不出」。
    coverage_total: int = 0
    # 补漏摘要（空 = 没触发）。给前端一行话："已自动补漏 2 轮：22% → 96%，¥0.011"
    repair_summary: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        """只取已知字段，**缺失或为 null 的走 dataclass 默认值**。

        不能写成 `{k: data.get(k) for k in __dataclass_fields__}`——`data.get` 对缺失键
        返回 None，会把默认值覆盖掉。旧 batch.json 里没有后来新增的字段
        （如 coverage_percent），加载后就变成 None：
          · 前端 JS 里 `null >= 0` 恰好为 true → 会显示「覆盖 null%」；
          · 后端 `None >= 0` 直接抛 TypeError → 在旧批上点「重试」会崩。
        这是所有「后加的字段」共有的坑，在此一次性兜住。
        """
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known and v is not None})


@dataclass
class Batch:
    """一次批任务的完整状态（spec + 运行态），batch.json 就是它的序列化。"""

    batch_id: str
    course_name: str
    output_root: str
    provider: dict  # effective_provider 快照（含单价），费用按它算，不随设置改动漂移
    options: dict  # {template, style, use_vision, thinking}
    jobs: list[JobRecord] = field(default_factory=list)
    exam_text: str = ""  # 快照；≤20000 字（与 ai_generator 截断一致）
    status: str = BATCH_RUNNING
    cancel_requested: bool = False
    created_at: str = ""
    updated_at: str = ""

    # ------- 序列化 -------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["jobs"] = [j.to_dict() for j in self.jobs]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Batch":
        jobs = [JobRecord.from_dict(j) for j in data.get("jobs", [])]
        return cls(
            batch_id=data["batch_id"],
            course_name=data.get("course_name", ""),
            output_root=data.get("output_root", str(OUTPUT_ROOT)),
            provider=data.get("provider", {}),
            options=data.get("options", {}),
            jobs=jobs,
            exam_text=data.get("exam_text", ""),
            status=data.get("status", BATCH_RUNNING),
            cancel_requested=data.get("cancel_requested", False),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    # ------- 路径与便捷访问 -------
    @property
    def root_dir(self) -> Path:
        return batch_dir(self.output_root, self.batch_id)

    def job_dir_of(self, job: JobRecord) -> Path:
        return job_dir(self.output_root, self.batch_id, job.idx, job.slug)

    def job(self, idx: int) -> JobRecord | None:
        return next((j for j in self.jobs if j.idx == idx), None)

    def jobs_in_order(self) -> list[JobRecord]:
        return sorted(self.jobs, key=lambda j: j.idx)

    # ------- 汇总统计（供轮询端点直接读） -------
    def summary(self) -> dict:
        total = len(self.jobs)
        done = sum(1 for j in self.jobs if j.status == STATUS_DONE)
        error = sum(1 for j in self.jobs if j.status == STATUS_ERROR)
        spent = round(sum(j.cost_yuan for j in self.jobs), 4)
        est_total = round(sum(j.est_cost for j in self.jobs), 4)
        return {
            "batch_id": self.batch_id,
            "course_name": self.course_name,
            "status": self.status,
            "total": total,
            "done": done,
            "error": error,
            "running": sum(1 for j in self.jobs if j.status == STATUS_RUNNING),
            "waiting": sum(1 for j in self.jobs if j.status == STATUS_WAITING),
            "spent_yuan": spent,
            "est_total_yuan": est_total,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------- 落盘

def read_text_retry(path: Path, attempts: int = 10) -> str:
    """读文本时容忍 Windows 上的瞬时共享冲突。

    写侧是「写 .tmp 再 replace」，而 replace 换名的那一瞬间目标文件处于换名窗口，
    此时并发的读会拿到 ERROR_SHARING_VIOLATION（Python 表现为 PermissionError）。
    worker 每完成一份写一次、前端每 2 秒轮询读一次——撞车是常态而非异常。
    实测：只给写侧重试不够，读侧同样会撞（否则轮询会偶发 500）。
    """
    for attempt in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable")


def persist_batch(batch: Batch) -> None:
    """原子写 batch.json：写唯一命名的 .tmp 再 replace，进程中断不会留下半个文件。

    两个 Windows 现实问题（2026-09-13 实测踩到，测试偶发 PermissionError）：
      1. 并发写如果共用同一个 .tmp 名字，两次写会互相踩 → tmp 名带进程/线程标识；
      2. replace 的目标文件正被另一个请求读取时，Windows 会拒绝（读取句柄没带
         FILE_SHARE_DELETE）→ 短退避重试几次。
    这不是极端情况：worker 每完成一份写一次、前端每 2 秒轮询读一次，撞车是常态。
    """
    batch.updated_at = _now()
    batch.root_dir.mkdir(parents=True, exist_ok=True)
    tmp = batch.root_dir / f"{BATCH_JSON}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(
        json.dumps(batch.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    target = batch.root_dir / BATCH_JSON
    for attempt in range(8):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            # 目标正被读；总退避 ~0.7s，远大于读一次 JSON 的耗时
            time.sleep(0.02 * (attempt + 1))
    tmp.replace(target)  # 最后一次不再兜：让异常暴露，不静默吞掉


def load_batch(output_root: str | Path, batch_id: str) -> Batch:
    """从磁盘恢复批任务（断点续跑入口）；文件不存在抛 FileNotFoundError。"""
    data = json.loads(
        read_text_retry(Path(output_root) / batch_id / BATCH_JSON)
    )
    batch = Batch.from_dict(data)
    # 悬空的 running：上次进程在 job 中途被杀，恢复后视为可重跑
    for j in batch.jobs:
        if j.status == STATUS_RUNNING:
            j.status = STATUS_WAITING
    return batch


def save_job_artifacts(batch: Batch, job: JobRecord, outcome: GenerateOutcome) -> None:
    """把单份生成结果写进 job 目录：doc.md + meta.json（+ 有覆盖率则 coverage.json）。"""
    d = batch.job_dir_of(job)
    d.mkdir(parents=True, exist_ok=True)
    (d / DOC_MD).write_text(outcome.markdown or "", encoding="utf-8")
    meta = {
        "filename": job.name,
        "idx": job.idx,
        "slug": job.slug,
        "status": job.status,
        "total_pages": job.total_pages,
        "image_count": job.image_count,
        "chars": job.chars,
        "exam_used": outcome.exam_used,
        "used_vision": outcome.used_vision,
        "truncated": outcome.truncated,
        "usage": outcome.usage,
        "cost_yuan": job.cost_yuan,
        "cost_is_estimate": job.cost_is_estimate,
        "error": job.error,
        "warning": job.warning,
        "coverage_percent": job.coverage_percent,
        "coverage_missing": job.coverage_missing,
        "coverage_total": job.coverage_total,
        "repair": outcome.repair.model_dump() if outcome.repair else None,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    (d / META_JSON).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 覆盖率报告单独落盘（含「疑似未覆盖」清单与出处页码），供前端下钻查看。
    # 没有就不写文件——留一个空文件会让读取方以为「算过了但没内容」。
    cov = outcome.coverage_dict()
    if cov is not None:
        (d / COVERAGE_JSON).write_text(
            json.dumps(cov, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def save_extracted(batch: Batch, job: JobRecord, result: ExtractionResult) -> None:
    """把提取结果落盘成 job 目录下的 extracted.json（prepare 提取一次，job 复用）。"""
    d = batch.job_dir_of(job)
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTRACTED_JSON).write_text(
        result.model_dump_json(), encoding="utf-8"
    )


def master_path(batch: Batch) -> Path:
    """总册产物路径：output/<batch_id>/总复习册-<课程名>.md。"""
    return batch.root_dir / MASTER_MD.format(course=slugify(batch.course_name or "课程"))


def rebuild_master(batch: Batch) -> Path | None:
    """增量重建总册：收集全部 done job 的 doc.md 原文 + 标题，汇编落盘。

    每完成一个 job 就重建一次（汇编是纯字符串操作，成本可忽略），
    中途任何时刻下载到的都是「当前已完成节」的最新版总册。
    没有任何 done job 时返回 None（不产出空总册）。
    """
    sections = []
    # 覆盖率自查：默认不附（用户选择「可开关、默认关」）。总册是拿来复习与分享的，
    # 塞一段自查报告会让同学以为这份文档不完整——需要时在选项里勾选。
    cov_rows: list[dict] = []
    for job in batch.jobs_in_order():
        if job.status != STATUS_DONE:
            continue
        f = batch.job_dir_of(job) / DOC_MD
        if not f.exists():
            continue
        md = f.read_text(encoding="utf-8")
        headings = extract_headings(md)
        sections.append(
            {
                "idx": job.idx,
                "title": headings[0] if headings else Path(job.name).stem,
                "source": job.name,
                "pages": job.total_pages,
                "warning": job.warning,
                "body": md,
            }
        )
        if (job.coverage_percent or -1) >= 0:
            cov_rows.append(
                {
                    "name": f"第 {job.idx} 节《{Path(job.name).stem}》",
                    "percent": job.coverage_percent,
                    "reliable": job.coverage_total >= MIN_TERMS_FOR_PERCENT,
                    "missing": _read_coverage_missing(batch, job),
                }
            )
    if not sections:
        return None
    doc = build_master_doc(batch.course_name or "课程", sections)
    if batch.options.get("attach_coverage") and cov_rows:
        doc += render_report_section(cov_rows)
    out = master_path(batch)
    out.write_text(doc, encoding="utf-8")
    return out


def _read_coverage_missing(batch: Batch, job: JobRecord) -> list[tuple[str, int]]:
    """从 job 的 coverage.json 取「疑似未覆盖」明细；取不到就当空（总册照常产出）。

    读盘失败不算错误：总册是主产物，覆盖率只是附录。
    """
    try:
        raw = read_text_retry(batch.job_dir_of(job) / COVERAGE_JSON)
        data = json.loads(raw)
        return [(d["term"], d["page"]) for d in data.get("missing_detail", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


# ---------------------------------------------------------------- 费用记账

def _repair_summary(rec) -> str:
    """把补漏记录压成一行给前端看。

    空串 = 没触发或没提升（前端就不显示徽标旁的提示）；
    回滚要如实说——用户得知道"试过但没变好"。
    """
    if rec is None or rec.final_percent <= rec.trigger_percent:
        return "已尝试自动补漏但未改善，保留原文档" if rec and rec.rolled_back else ""
    return (
        f"已自动补漏 {len(rec.rounds)} 轮："
        f"{rec.trigger_percent}% → {rec.final_percent}%，¥{rec.total_cost_yuan:.3f}"
    )


def _account_cost(
    job: JobRecord, outcome: GenerateOutcome, provider: dict
) -> None:
    """usage 齐全 → 按实际 token × 单价记账；否则保持 prepare 阶段的估算值。

    outcome.image_count>0 说明请求走了视觉模型，整个请求按视觉单价计
    （与 cost_estimate 的 billed_model 判定一致：同请求只按一个模型收）。
    """
    usage = outcome.usage
    if usage and usage.get("total_tokens", 0) > 0:
        vision = outcome.image_count > 0
        pin = provider.get("vision_price_in" if vision else "price_in")
        pout = provider.get("vision_price_out" if vision else "price_out")
        pin = float(pin) if pin is not None else float(provider.get("price_in", 0))
        pout = float(pout) if pout is not None else float(provider.get("price_out", 0))
        job.cost_yuan = round(
            (usage["prompt_tokens"] * pin + usage["completion_tokens"] * pout) / 1_000_000,
            4,
        )
        job.cost_is_estimate = False
    else:
        job.cost_yuan = round(job.est_cost, 4)
        job.cost_is_estimate = True


def estimate_job_cost(
    job: JobRecord, batch: Batch, use_vision: bool, thinking: bool
) -> float:
    """按 prepare 阶段已知的字数/图片数粗估单份费用（展示与 est_cost 记账共用）。"""
    provider = batch.provider
    est = estimate_generation_cost(
        provider,
        chars=job.chars,
        image_count=job.image_count,
        use_vision=use_vision,
        exam_chars=len(batch.exam_text),
        style=batch.options.get("style", "详细"),
        thinking=thinking,
        pages=job.total_pages,
    )
    return round(est["cost_yuan"], 4)


# ---------------------------------------------------------------- 编排器

class BatchRunner:
    """串行跑完一个批的所有 waiting job；每步落盘，可取消/暂停，可注入假实现测试。"""

    def __init__(
        self,
        batch: Batch,
        extract_fn=None,
        generate_fn=None,
    ) -> None:
        self.batch = batch
        # 默认实现即真实管线；测试注入假函数验证状态机
        self._extract = extract_fn or (
            lambda src, img_dir: extract_any(Path(src), image_dir=img_dir)
        )
        self._generate = generate_fn or run_single
        # 目标实现是否接受 repair_workdir —— **按签名判断，不能按"是否传了
        # generate_fn"判断**：测试是 monkeypatch 掉 br.run_single 的（generate_fn
        # 仍为 None），那时 generate_fn is None 为真却拿到的是 3 参数假函数，
        # 多传一个关键字参数会 TypeError，整个批次变 partial（实测踩到）。
        try:
            self._pass_repair_dir = (
                "repair_workdir" in inspect.signature(self._generate).parameters
            )
        except (TypeError, ValueError):
            self._pass_repair_dir = False

    def start(self) -> threading.Thread:
        """daemon 线程壳：路由层调用后立即返回，轮询端点读 batch.json。"""
        t = threading.Thread(
            target=self.run, name=f"batch-{self.batch.batch_id}", daemon=True
        )
        t.start()
        return t

    # ------- worker -------

    def run(self) -> None:
        batch = self.batch
        if batch.status not in (BATCH_RUNNING, BATCH_PAUSED):
            return  # done/cancelled 等终态不重复执行
        if batch.status == BATCH_PAUSED:
            batch.status = BATCH_RUNNING  # 用户「继续」后恢复
        persist_batch(batch)

        consecutive_network = 0
        for job in batch.jobs_in_order():
            if batch.cancel_requested:
                batch.status = BATCH_CANCELLED
                persist_batch(batch)
                return
            if job.status == STATUS_DONE:
                continue  # 断点续跑：已完成的不重做
            if job.status == STATUS_ERROR:
                continue  # 失败份不自动重跑：由用户 retry_jobs 显式重置，避免重复计费

            job.status = STATUS_RUNNING
            job.started_at = _now()
            job.error = ""
            job.warning = ""
            persist_batch(batch)

            try:
                result = self._obtain_extraction(job)
                request = GenerateRequest(
                    filename=job.name,
                    template=batch.options.get("template", "复习指南"),
                    style=batch.options.get("style", "详细"),
                    use_vision=bool(batch.options.get("use_vision", True)),
                    thinking=bool(batch.options.get("thinking", False)),
                    exam_mode=batch.options.get("exam_mode") or "direct",
                    enable_repair=bool(batch.options.get("enable_repair", False)),
                )
                if self._pass_repair_dir:
                    # 补漏过程文件放进该 job 目录，和 doc.md 并排，便于事后核对
                    outcome = self._generate(
                        result,
                        request,
                        batch.exam_text,
                        repair_workdir=batch.job_dir_of(job) / "repair",
                    )
                else:
                    outcome = self._generate(result, request, batch.exam_text)
            except Exception as exc:  # noqa: BLE001 job 级失败不阻塞整批
                kind = classify_error(exc)
                if isinstance(exc, PptExtractionError) or kind == "other":
                    job.error = str(exc)
                else:
                    job.error = friendly_ai_error(exc)
                job.status = STATUS_ERROR
                job.finished_at = _now()
                # 错误也要落盘 meta（error 字段），进程此刻挂了不丢现场
                save_job_artifacts(batch, job, GenerateOutcome(markdown=""))
                if kind == "balance":
                    # 余额/额度类：剩余 job 全标 error，避免继续白打
                    self._abort_remaining(job, "余额不足或额度用尽，已停止后续课件")
                    batch.status = BATCH_PARTIAL
                    persist_batch(batch)
                    return
                if kind == "network":
                    consecutive_network += 1
                    if consecutive_network >= NETWORK_FAIL_LIMIT:
                        batch.status = BATCH_PAUSED  # 网络不稳：暂停，由用户决定继续
                        persist_batch(batch)
                        return
                persist_batch(batch)  # 单次失败也即时落盘
                continue

            _account_cost(job, outcome, batch.provider)
            # 补漏花的钱也是这一份的成本——批次的"已花费"是各 job 求和，会自动带上
            if outcome.repair is not None and outcome.repair.total_cost_yuan:
                job.cost_yuan = round(job.cost_yuan + outcome.repair.total_cost_yuan, 4)
                job.cost_is_estimate = False
            job.repair_summary = _repair_summary(outcome.repair)
            job.warning = "truncated" if outcome.truncated else ""
            # 覆盖率随 outcome 一起下来（见 ai_generator.run_single）。
            # 没算出来就保持 -1，前端按「无数据」处理，不要伪装成 0%。
            if outcome.coverage is not None:
                job.coverage_percent = outcome.coverage.percent
                job.coverage_missing = outcome.coverage.missing_terms
                job.coverage_total = outcome.coverage.total_terms
            job.status = STATUS_DONE
            job.finished_at = _now()
            consecutive_network = 0
            save_job_artifacts(batch, job, outcome)
            persist_batch(batch)
            rebuild_master(batch)  # 增量重建总册：随时可下载「已完成节」的最新版

        # 自然结束：汇总状态
        if batch.cancel_requested:
            batch.status = BATCH_CANCELLED
        elif any(j.status == STATUS_ERROR for j in batch.jobs):
            batch.status = BATCH_PARTIAL
        else:
            batch.status = BATCH_DONE
        persist_batch(batch)
        rebuild_master(batch)  # 收尾兜底：确保总册与最终状态一致

    # ------- 内部 -------

    def _obtain_extraction(self, job: JobRecord) -> ExtractionResult:
        """优先读 prepare 落盘的 extracted.json；没有（直接建批未 prepare）则现场提取。"""
        d = self.batch.job_dir_of(job)
        cached = d / EXTRACTED_JSON
        if cached.exists():
            return ExtractionResult.model_validate_json(cached.read_text(encoding="utf-8"))
        img_dir = d / "images"
        result = self._extract(job.src_pptx, img_dir)
        job.total_pages = result.total_pages
        job.image_count = result.image_count
        job.chars = sum(
            len(s.text) + len(s.notes) for s in result.slides
        )
        job.notes_chars = result.notes_chars
        save_extracted(self.batch, job, result)
        return result

    def _abort_remaining(self, current: JobRecord, reason: str) -> None:
        """把当前 job 之后仍 waiting 的 job 标记为 error（熔断）。"""
        for j in self.batch.jobs_in_order():
            if j.idx > current.idx and j.status == STATUS_WAITING:
                j.status = STATUS_ERROR
                j.error = reason


def retry_jobs(batch: Batch, indices: list[int] | None = None) -> list[JobRecord]:
    """把指定 job（缺省=全部 error）重置为 waiting；done 不受影响（避免误覆盖）。

    返回被重置的 job 列表，供路由层决定是否接着跑。
    """
    targets = [j for j in batch.jobs if j.status == STATUS_ERROR]
    if indices is not None:
        wanted = set(indices)
        targets = [j for j in targets if j.idx in wanted]
    for j in targets:
        j.status = STATUS_WAITING
        j.error = ""
    return targets
