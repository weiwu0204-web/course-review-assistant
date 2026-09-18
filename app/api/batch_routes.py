"""批任务 API：多文件建批、速览估价、轮询进度、总册/单节下载、重试/取消、历史列表。

端点一览（契约见 docs/P0-ARCHITECTURE.md §8）：
  POST /api/batch/peek            单份速览：只提取不生成（前端展示页数/图片/预估费用）
  POST /api/batch/start           多文件建批，立即返回 batch_id，后台线程串行执行
  GET  /api/batch/{id}            轮询：批汇总 + 逐份明细（前端 2s 一次）
  GET  /api/batch/{id}/master     下载总册（每个 job 完成后增量重建，随时是最新版）
  GET  /api/batch/{id}/doc/{idx}  下载单节
  POST /api/batch/{id}/retry      重试失败份（缺省全部 error；可指定 indices）
  POST /api/batch/{id}/cancel     取消（worker 在 job 边界生效，不打断当前 job）
  GET  /api/batches               历史批列表（页面刷新找回 / 续跑入口）

状态不驻内存：轮询直接读 batch.json（每 job 完成即原子落盘），进程重启也能恢复现场；
线程存活状态用模块级 _LIVE_THREADS 跟踪，仅用于 retry/cancel 的防并发守卫。
"""
from __future__ import annotations

import json
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from app.config import current_provider, get_settings
from app.services import batch_runner as br
from app.services.coverage import applies_to, not_applicable_note
from app.services.ai_generator import EXAM_MAX_CHARS
from app.services.cost_estimate import estimate_generation_cost
from app.services.extractor import SUPPORTED_EXTENSIONS, PptExtractionError

# EXAM_DIR 与单份 /exam 端点共用一个目录（路径收敛待 PACKAGING 轮统一，见 PACKAGING.md）
from app.api.routes import EXAM_DIR

router = APIRouter()

MAX_BATCH_FILES = 20
"""单批上限。原为 10（P0 设计期的「3–10 份」假设，从未被真实课程检验）。

2026-09-11 实测一门真实课程（Ceph 分布式存储，14 节 / 305 页）发现：一门课十几节
是常态，10 会把「整门课 = 一份总册」这个核心承诺直接截断成两批两份总册。
成本与耗时都不构成理由——14 节实测预估仅 ¥0.77、串行约 15–25 分钟，中途可停、
可关页面、可断点续跑。故抬到 20（覆盖常规学期课程 12–18 讲），仅作安全兜底。

前端的份数上限从 /api/batches 的 max_files 读取，不另写一份常量——
同一规则两处实现迟早漂移（0.8.2 的风格联动就是这么漏的）。"""


class RetryRequest(BaseModel):
    """重试请求体：缺省重试全部 error job；indices 可只重试指定份。"""

    indices: list[int] | None = None


# 线程存活守卫：仅本进程内有效；进程重启后批状态以 batch.json 为准（悬空 job 已被重置）
_LIVE_THREADS: dict[str, threading.Thread] = {}


def _alive(batch_id: str) -> bool:
    t = _LIVE_THREADS.get(batch_id)
    return bool(t and t.is_alive())


def _load_or_404(batch_id: str) -> br.Batch:
    try:
        return br.load_batch(br.OUTPUT_ROOT, batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="未找到该批任务") from None


def _detail(batch: br.Batch) -> dict:
    """轮询响应：批汇总 + 逐份明细（去掉本地绝对路径）+ 总册就绪标记。"""
    d = batch.summary()
    jobs = []
    for j in batch.jobs_in_order():
        item = j.to_dict()
        item.pop("src_pptx", None)  # 服务端内部路径，前端不需要
        jobs.append(item)
    d["jobs"] = jobs
    d["cancel_requested"] = batch.cancel_requested
    d["master_ready"] = br.master_path(batch).exists()
    # 当前档位不做覆盖率回检时给一句解释（前端在任务列表上方显示），
    # 否则用户看到「一个徽标都没有」会以为功能坏了。
    _tpl = batch.options.get("template", "复习指南")
    _sty = batch.options.get("style", "详细")
    d["coverage_note"] = "" if applies_to(_tpl, _sty) else not_applicable_note(_tpl, _sty)
    # 悬空 running：disk 上是 running，但本进程没有对应工作线程——进程重启过
    # （run.py 开着 reload，改文件即重启）。只在响应里降级为 paused，让前端出现
    # 「继续生成」；这里刻意不写盘：轮询极频繁，与后台线程并发写 batch.json 会在
    # Windows 上撞文件锁。retry/cancel 的守卫判断的是 _alive 而非 status，不受影响。
    if batch.status == br.BATCH_RUNNING and not _alive(batch.batch_id):
        d["status"] = br.BATCH_PAUSED
    return d


def _validate_courseware(name: str, size: int) -> None:
    suffix = Path(name).suffix.lower()
    if suffix == ".ppt":
        raise HTTPException(
            status_code=400,
            detail=f"《{name}》是 .ppt 旧格式：请用 PowerPoint 另存为 .pptx 再上传",
        )
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"《{name}》不是支持的课件格式（仅 .pptx / .pdf）",
        )
    limit = get_settings().max_upload_size_mb * 1024 * 1024
    if size > limit:
        raise HTTPException(
            status_code=413, detail=f"《{name}》超过 {get_settings().max_upload_size_mb}MB 限制"
        )


@router.post("/batch/peek")
async def batch_peek(
    file: UploadFile,
    use_vision: bool = Form(True),
    thinking: bool = Form(False),
    style: str = Form("详细"),
    exam_chars: int = Form(0),
) -> dict:
    """单份速览：只提取不生成，供前端在「开始生成」前展示每份的规模与预估费用。"""
    name = Path(file.filename or "unknown.pptx").name
    content = await file.read()
    _validate_courseware(name, len(content))

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / name
        dest.write_bytes(content)
        try:
            result = br.extract_any(dest, image_dir=Path(td) / "images")
        except PptExtractionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    chars = sum(len(s.text) + len(s.notes) for s in result.slides)
    est = estimate_generation_cost(
        current_provider(),
        chars=chars,
        image_count=result.image_count,
        use_vision=use_vision,
        exam_chars=max(0, exam_chars),
        style=style,
        thinking=thinking,
        pages=result.total_pages,
    )
    return {
        "filename": name,
        "total_pages": result.total_pages,
        "image_count": result.image_count,
        "chars": chars,
        "est_cost_yuan": est["cost_yuan"],
    }


@router.post("/batch/start")
async def batch_start(
    files: list[UploadFile] = File(...),
    template: str = Form("复习指南"),
    style: str = Form("详细"),
    use_vision: bool = Form(True),
    thinking: bool = Form(False),
    course_name: str = Form(""),
    exam_id: str = Form(""),
    exam_mode: str = Form("direct"),
    attach_coverage: bool = Form(False),
    enable_repair: bool = Form(False),
) -> dict:
    """多文件建批：校验 → 逐份落盘 → 生成 Batch → 后台线程串行执行。

    立即返回 batch_id，前端凭它轮询进度。参数（模板/风格/视觉/思考/真题）
    对每一份 job 相同，快照进 batch.json，不随设置页改动漂移。
    """
    settings = get_settings()
    if not settings.ai_api_key:
        raise HTTPException(
            status_code=400,
            detail="尚未配置 AI API Key：请点击右上角「设置」选择服务商并粘贴你的 Key。",
        )
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一份 .pptx / .pdf 课件")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400, detail=f"一次最多 {MAX_BATCH_FILES} 份课件；课件较多请分批生成"
        )

    payloads: list[tuple[str, bytes]] = []
    for f in files:
        name = Path(f.filename or "unknown.pptx").name
        content = await f.read()
        _validate_courseware(name, len(content))
        payloads.append((name, content))

    exam_text = ""
    if exam_id:
        exam_file = EXAM_DIR / f"{exam_id}.txt"
        if not exam_file.exists():
            raise HTTPException(status_code=404, detail="未找到该往年试题，请重新上传")
        exam_text = exam_file.read_text(encoding="utf-8")[:EXAM_MAX_CHARS]

    batch_id = uuid.uuid4().hex[:8]
    course = course_name.strip() or Path(payloads[0][0]).stem or "课程"
    batch = br.Batch(
        batch_id=batch_id,
        course_name=course,
        output_root=str(br.OUTPUT_ROOT),
        provider=current_provider(),  # 单价快照：记账不随设置页换家漂移
        options={
            "template": template,
            "style": style,
            "use_vision": use_vision,
            "thinking": thinking,
            "exam_mode": exam_mode if exam_id else "",
            # 覆盖率自查是否附在总册末尾。默认不附：总册是拿来复习与分享的。
            "attach_coverage": attach_coverage,
            # 覆盖率低于阈值时自动补漏（要额外花钱，默认关）
            "enable_repair": enable_repair,
        },
        exam_text=exam_text,
        status=br.BATCH_RUNNING,
    )

    for i, (name, content) in enumerate(payloads):
        slug = br.slugify(name)
        jd = br.job_dir(batch.output_root, batch_id, i + 1, slug)
        jd.mkdir(parents=True, exist_ok=True)
        src = jd / name
        src.write_bytes(content)
        batch.jobs.append(
            br.JobRecord(idx=i + 1, name=name, slug=slug, src_pptx=str(src))
        )

    br.persist_batch(batch)
    _LIVE_THREADS[batch_id] = br.BatchRunner(batch).start()
    # 必须返回 _detail（含 jobs 数组）而不是 summary()：前端拿到响应后立即渲染
    # 作业列表，summary() 里没有 jobs 会让渲染抛错、连带把轮询启动也吞掉，
    # 界面就永远停在 0/N。
    return _detail(batch)


@router.get("/batch/{batch_id}")
def batch_status(batch_id: str) -> dict:
    """轮询端点：读 batch.json（每 job 完成即落盘），进程重启后照样能恢复。"""
    return _detail(_load_or_404(batch_id))


@router.get("/batch/{batch_id}/master")
def batch_master(batch_id: str):
    """下载总册（最新增量版：哪怕还有份没跑完，已完成节也在里面）。"""
    batch = _load_or_404(batch_id)
    path = br.master_path(batch)
    if not path.exists():
        raise HTTPException(status_code=404, detail="总册尚未生成：还没有课件完成")
    return FileResponse(
        path, filename=path.name, media_type="text/markdown; charset=utf-8"
    )


@router.get("/batch/{batch_id}/doc/{idx}")
def batch_doc(batch_id: str, idx: int):
    """下载单节文档。"""
    batch = _load_or_404(batch_id)
    job = batch.job(idx)
    if job is None:
        raise HTTPException(status_code=404, detail="没有这一节")
    f = batch.job_dir_of(job) / br.DOC_MD
    if job.status != br.STATUS_DONE or not f.exists():
        raise HTTPException(status_code=404, detail="该节尚未生成完成")
    return PlainTextResponse(
        f.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8"
    )


@router.get("/batch/{batch_id}/coverage/{idx}")
def batch_coverage(batch_id: str, idx: int) -> dict:
    """取某一节的覆盖率明细（「疑似未覆盖」清单 + 出处页码），供前端下钻。

    只读 `coverage.json`。没有该文件说明这份没算出覆盖率（或还没跑完），
    返回 404——前端据此显示「无数据」，**不要伪装成 0%**。
    """
    batch = _load_or_404(batch_id)
    job = batch.job(idx)
    if job is None:
        raise HTTPException(status_code=404, detail="没有这一节")
    f = batch.job_dir_of(job) / br.COVERAGE_JSON
    if not f.exists():
        raise HTTPException(status_code=404, detail="该节没有覆盖率数据")
    try:
        return json.loads(br.read_text_retry(f))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="覆盖率数据损坏") from exc


@router.post("/batch/{batch_id}/retry")
def batch_retry(batch_id: str, payload: RetryRequest | None = None) -> dict:
    """重跑失败份（缺省=全部 error）；paused 的批恢复时 waiting 份也会继续跑。"""
    batch = _load_or_404(batch_id)
    if _alive(batch_id):
        raise HTTPException(status_code=400, detail="批任务正在运行，请等它结束后再重试")
    targets = br.retry_jobs(batch, payload.indices if payload else None)
    waiting = [j for j in batch.jobs if j.status == br.STATUS_WAITING]
    if not targets and not waiting:
        raise HTTPException(status_code=400, detail="没有可重试的课件")
    batch.status = br.BATCH_RUNNING
    batch.cancel_requested = False
    br.persist_batch(batch)
    _LIVE_THREADS[batch_id] = br.BatchRunner(batch).start()
    return _detail(batch)


@router.post("/batch/{batch_id}/cancel")
def batch_cancel(batch_id: str) -> dict:
    """停止批任务：运行中则置标记（worker 在 job 边界生效，不打断当前份）；
    已暂停/部分完成的批直接标 cancelled。done/cancelled 的批无需取消。"""
    batch = _load_or_404(batch_id)
    if _alive(batch_id):
        batch.cancel_requested = True
        br.persist_batch(batch)
        return _detail(batch)
    if batch.status in (br.BATCH_DONE, br.BATCH_CANCELLED):
        raise HTTPException(status_code=400, detail="该批任务已结束，无需取消")
    batch.status = br.BATCH_CANCELLED
    br.persist_batch(batch)
    return _detail(batch)


@router.get("/batches")
def batch_list() -> dict:
    """历史批列表：页面刷新找回运行中任务 / 断点续跑入口。"""
    items = []
    if br.OUTPUT_ROOT.exists():
        for d in br.OUTPUT_ROOT.iterdir():
            f = d / br.BATCH_JSON
            if not d.is_dir() or d.name.startswith(".") or not f.exists():
                continue
            try:
                batch = br.Batch.from_dict(json.loads(br.read_text_retry(f)))
            except (json.JSONDecodeError, KeyError):
                continue  # 损坏的历史记录不挡列表
            items.append(batch.summary())
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    # max_files 一并下发：前端的上限文案与校验都从这里取，避免各写一份常量
    return {"batches": items, "max_files": MAX_BATCH_FILES}
