"""batch_runner 状态机测试：注入假 extract/generate，不真调模型、不打真实 API。

覆盖：正常跑完 / usage 记账 / 断点续跑 / 单份失败不阻塞 / 余额熔断 / 网络自动暂停 /
取消 / retry 只重置 error / 落盘与 load 往返。
"""
from pathlib import Path

from app.models import ExtractionResult, SlideContent
from app.services.ai_generator import GenerateOutcome
from app.services.batch_runner import (
    BATCH_CANCELLED,
    BATCH_DONE,
    BATCH_PARTIAL,
    BATCH_PAUSED,
    BATCH_RUNNING,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_WAITING,
    DOC_MD,
    META_JSON,
    Batch,
    BatchRunner,
    JobRecord,
    load_batch,
    persist_batch,
    retry_jobs,
    slugify,
)

FAKE_PROVIDER = {
    "name": "fake",
    "price_in": 3.0,
    "price_out": 9.0,
    "vision_price_in": 2.0,
    "vision_price_out": 6.0,
}

OPTIONS = {"template": "复习指南", "style": "详细", "use_vision": True, "thinking": False}


def _fake_extract(total_pages: int = 3, chars_per_page: int = 50):
    def extract(src: str, img_dir):
        stem = Path(src).stem
        return ExtractionResult(
            filename=f"{stem}.pptx",
            total_pages=total_pages,
            title=stem,
            image_count=0,
            notes_chars=0,
            slides=[
                SlideContent(page=i + 1, text="甲" * chars_per_page, notes="")
                for i in range(total_pages)
            ],
        )

    return extract


def _make_batch(tmp_path, n: int = 3, provider=None, batch_id: str = "t1") -> Batch:
    provider = provider or FAKE_PROVIDER
    jobs = [
        JobRecord(
            idx=i + 1,
            name=f"第{i + 1}章-讲义.pptx",
            slug=slugify(f"第{i + 1}章-讲义.pptx"),
            src_pptx=str(tmp_path / f"src_{i}.pptx"),
        )
        for i in range(n)
    ]
    return Batch(
        batch_id=batch_id,
        course_name="测试课",
        output_root=str(tmp_path),
        provider=provider,
        options=dict(OPTIONS),
        jobs=jobs,
        status=BATCH_RUNNING,
    )


class _Recorder:
    """记录每次 generate 收到的 (filename, exam_text)，便于断言顺序与传参。"""

    def __init__(self, outcomes=None, raises: dict[int, Exception] | None = None,
                 on_call=None):
        self.outcomes = outcomes or {}
        self.raises = raises or {}
        self.on_call = on_call
        self.calls: list[tuple[str, str]] = []  # (filename, exam_text)

    def gen(self, result: ExtractionResult, request, exam_text: str) -> GenerateOutcome:
        self.calls.append((request.filename, exam_text))
        if self.on_call:
            self.on_call()
        if request.filename in self.raises:
            raise self.raises[request.filename]
        out = self.outcomes.get(request.filename)
        if out is not None:
            return out
        return GenerateOutcome(
            markdown=f"文档-{request.filename}",
            image_count=0,
            truncated=False,
            usage=None,
            exam_used=bool(exam_text),
        )


def test_run_all_done_and_artifacts(tmp_path):
    """正常跑完：status=done，每个 job 产出 doc.md / meta.json，batch.json 落盘。"""
    batch = _make_batch(tmp_path, n=3)
    rec = _Recorder()
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()

    assert batch.status == BATCH_DONE
    assert all(j.status == STATUS_DONE for j in batch.jobs)
    for j in batch.jobs:
        d = batch.job_dir_of(j)
        assert (d / DOC_MD).exists()
        assert (d / META_JSON).exists()
        assert "文档-" in (d / DOC_MD).read_text(encoding="utf-8")
    assert (batch.root_dir / "batch.json").exists()
    # 串行 + 顺序：按 idx 依次调用
    assert [c[0] for c in rec.calls] == [
        "第1章-讲义.pptx", "第2章-讲义.pptx", "第3章-讲义.pptx"
    ]
    # 提取只做一次并缓存到 extracted.json
    for j in batch.jobs:
        assert (batch.job_dir_of(j) / "extracted.json").exists()


def test_run_records_usage_cost(tmp_path):
    """usage 齐全 → 按实际 token × 单价记账，cost_is_estimate=False。"""
    batch = _make_batch(tmp_path, n=1)
    usage = {"prompt_tokens": 500_000, "completion_tokens": 500_000,
             "total_tokens": 1_000_000}
    rec = _Recorder(outcomes={
        "第1章-讲义.pptx": GenerateOutcome(
            markdown="m", image_count=0, truncated=False, usage=usage
        )
    })
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()
    job = batch.jobs[0]
    assert job.status == STATUS_DONE
    assert job.cost_yuan == round((500_000 * 3 + 500_000 * 9) / 1_000_000, 4)  # 6.0
    assert job.cost_is_estimate is False
    meta = (batch.job_dir_of(job) / META_JSON).read_text(encoding="utf-8")
    assert '"usage"' in meta and "prompt_tokens" in meta


def test_truncated_warns(tmp_path):
    batch = _make_batch(tmp_path, n=1)
    rec = _Recorder(outcomes={
        "第1章-讲义.pptx": GenerateOutcome(
            markdown="m", truncated=True, usage=None
        )
    })
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()
    assert batch.jobs[0].warning == "truncated"
    assert batch.jobs[0].status == STATUS_DONE


def test_single_job_error_does_not_block_others(tmp_path):
    """非余额/网络类错误：仅当前 job 记错，其余继续 → partial。"""
    batch = _make_batch(tmp_path, n=3)
    rec = _Recorder(raises={"第2章-讲义.pptx": RuntimeError("some weird failure")})
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()

    assert batch.status == BATCH_PARTIAL
    assert batch.jobs[1].status == STATUS_ERROR
    assert "some weird failure" in batch.jobs[1].error  # other 类透原文
    assert batch.jobs[0].status == STATUS_DONE
    assert batch.jobs[2].status == STATUS_DONE
    # 错误 job 也落盘了 meta（进程此刻挂了不丢现场）
    assert (batch.job_dir_of(batch.jobs[1]) / META_JSON).exists()


def test_balance_aborts_remaining(tmp_path):
    """余额不足：当前 job 记错，剩余 waiting job 全部熔断，不再调用 generate。"""
    batch = _make_batch(tmp_path, n=3)
    rec = _Recorder(raises={
        "第2章-讲义.pptx": RuntimeError("Error code: 402 - Insufficient Balance")
    })
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()

    assert batch.status == BATCH_PARTIAL
    assert batch.jobs[1].status == STATUS_ERROR
    assert "余额不足" in batch.jobs[1].error
    assert batch.jobs[2].status == STATUS_ERROR  # 熔断
    assert "已停止后续课件" in batch.jobs[2].error
    assert len(rec.calls) == 2  # 第 3 份没被调用


def test_network_errors_pause_batch(tmp_path):
    """连续 2 次网络类失败 → 自动暂停，剩余保持 waiting；可恢复继续。"""
    batch = _make_batch(tmp_path, n=4)
    rec = _Recorder(raises={
        "第2章-讲义.pptx": RuntimeError("connection timed out"),
        "第3章-讲义.pptx": RuntimeError("network unreachable"),
    })
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()

    assert batch.status == BATCH_PAUSED
    assert batch.jobs[0].status == STATUS_DONE
    assert batch.jobs[1].status == STATUS_ERROR
    assert "连不上服务商服务器" in batch.jobs[1].error
    assert batch.jobs[2].status == STATUS_ERROR  # 触发暂停的这一份也记错
    assert batch.jobs[3].status == STATUS_WAITING  # 未被消耗，留给用户决定

    # 用户修复网络后「继续」：只剩第 4 份 waiting 会跑，前两失败份需 retry 才重跑
    rec2 = _Recorder()
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec2.gen).run()
    assert batch.jobs[3].status == STATUS_DONE
    assert batch.status == BATCH_PARTIAL  # 仍有 error 份
    assert [c[0] for c in rec2.calls] == ["第4章-讲义.pptx"]


def test_cancel_between_jobs(tmp_path):
    """cancel_requested 置位后：当前 job 跑完即停，其余保持 waiting。"""
    batch = _make_batch(tmp_path, n=3)

    def on_call():
        if len(rec.calls) == 1:  # 第一个 job 生成完就请求取消
            batch.cancel_requested = True

    rec = _Recorder(on_call=on_call)
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()

    assert batch.status == BATCH_CANCELLED
    assert batch.jobs[0].status == STATUS_DONE
    assert batch.jobs[1].status == STATUS_WAITING
    assert batch.jobs[2].status == STATUS_WAITING
    assert len(rec.calls) == 1


def test_retry_resets_only_error_jobs(tmp_path):
    batch = _make_batch(tmp_path, n=3)
    rec = _Recorder(raises={"第2章-讲义.pptx": RuntimeError("boom")})
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()
    assert batch.status == BATCH_PARTIAL

    targets = retry_jobs(batch)
    assert [t.idx for t in targets] == [2]
    assert batch.jobs[1].status == STATUS_WAITING
    assert batch.jobs[1].error == ""
    # done 的不被误重置
    assert batch.jobs[0].status == STATUS_DONE

    # 限定 indices 时只重置命中的 error job
    batch2 = _make_batch(tmp_path, n=3)
    rec2 = _Recorder(raises={"第1章-讲义.pptx": RuntimeError("a"),
                             "第3章-讲义.pptx": RuntimeError("b")})
    BatchRunner(batch2, extract_fn=_fake_extract(), generate_fn=rec2.gen).run()
    retry_jobs(batch2, indices=[3])
    assert batch2.jobs[0].status == STATUS_ERROR  # 未命中仍 error
    assert batch2.jobs[2].status == STATUS_WAITING


def test_persist_and_load_roundtrip(tmp_path):
    """batch.json 落盘后可 load_batch 恢复现场；running 悬空 job 被重置为 waiting。"""
    batch = _make_batch(tmp_path, n=2)
    rec = _Recorder(outcomes={
        "第1章-讲义.pptx": GenerateOutcome(
            markdown="m1", usage={"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}
        )
    })
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()
    assert batch.status == BATCH_DONE

    loaded = load_batch(tmp_path, "t1")
    assert loaded.batch_id == "t1"
    assert loaded.status == BATCH_DONE
    assert loaded.course_name == "测试课"
    assert [j.status for j in loaded.jobs] == [STATUS_DONE, STATUS_DONE]
    assert loaded.jobs[0].cost_yuan == batch.jobs[0].cost_yuan
    # 文件仍可读
    assert (loaded.job_dir_of(loaded.jobs[0]) / DOC_MD).exists()

    # 悬空 running → load 后变 waiting（模拟进程中断后恢复）
    batch.jobs[1].status = "running"
    persist_batch(batch)
    loaded2 = load_batch(tmp_path, "t1")
    assert loaded2.jobs[1].status == STATUS_WAITING


def test_master_doc_rebuilt_incrementally(tmp_path):
    """总册随 job 完成增量重建：标题取各 doc 第一个 H1，出处/截断警示齐全，正文原样。"""
    from app.services.batch_runner import master_path

    batch = _make_batch(tmp_path, n=2)
    rec = _Recorder(outcomes={
        "第1章-讲义.pptx": GenerateOutcome(markdown="# 第一章 概述\n\n内容A"),
        "第2章-讲义.pptx": GenerateOutcome(markdown="# 第二章 物理层\n\n内容B", truncated=True),
    })
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()

    mp = master_path(batch)
    assert mp.exists()
    text = mp.read_text(encoding="utf-8")
    assert "《测试课》期末复习总册" in text
    assert "## 第 1 节：第一章 概述" in text
    assert "## 第 2 节：第二章 物理层" in text
    assert "> 来源：第2章-讲义.pptx" in text
    assert "⚠️ 本节生成时输出被截断" in text  # 截断警示进总册
    assert "内容A" in text and "内容B" in text  # 汇编不改写正文


def test_all_failed_no_master(tmp_path):
    """全部失败：不产出空总册。"""
    from app.services.batch_runner import master_path

    batch = _make_batch(tmp_path, n=1)
    rec = _Recorder(raises={"第1章-讲义.pptx": RuntimeError("boom")})
    BatchRunner(batch, extract_fn=_fake_extract(), generate_fn=rec.gen).run()
    assert batch.status == BATCH_PARTIAL
    assert not master_path(batch).exists()


def test_read_text_retry_survives_transient_sharing_violation(tmp_path, monkeypatch):
    """Windows 上 replace 换名的瞬间，并发读会拿到 PermissionError。

    worker 每完成一份写一次、前端每 2 秒轮询读一次 → 撞车是常态，读侧必须重试
    （只给写侧重试不够，实测轮询会偶发 500）。
    """
    import time as _t

    from app.services.batch_runner import read_text_retry

    f = tmp_path / "x.json"
    f.write_text('{"a": 1}', encoding="utf-8")

    calls = {"n": 0}
    real = type(f).read_text

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "sharing violation")
        return real(self, *a, **k)

    monkeypatch.setattr(type(f), "read_text", flaky)
    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    assert read_text_retry(f) == '{"a": 1}'
    assert calls["n"] == 3


def test_read_text_retry_gives_up_eventually(tmp_path, monkeypatch):
    """一直读不到就抛出，不静默吞掉——别把真故障伪装成「暂无数据」。"""
    import pytest as _pytest

    from app.services.batch_runner import read_text_retry

    f = tmp_path / "y.json"
    f.write_text("{}", encoding="utf-8")

    def always_fail(self, *a, **k):
        raise PermissionError(13, "x")

    monkeypatch.setattr(type(f), "read_text", always_fail)
    with _pytest.raises(PermissionError):
        read_text_retry(f, attempts=3)


def test_from_dict_uses_defaults_for_missing_or_null_fields():
    """旧 batch.json 里没有后来新增的字段时，必须落回 dataclass 默认值而不是 None。

    踩过的坑：原实现 `{k: data.get(k) for k in __dataclass_fields__}` 里 data.get 对
    缺失键返回 None，**覆盖掉默认值**。后果：
      · 前端 JS 里 `null >= 0` 恰好为 true → 会显示「覆盖 null%」；
      · 后端 `None >= 0` 直接抛 TypeError → 在旧批上点「重试」会崩。
    这是所有「后来新增字段」共有的坑，本用例一次性锁住。
    """
    old = {
        "idx": 1, "name": "a.pptx", "slug": "a", "src_pptx": "a.pptx",
        "status": STATUS_DONE, "total_pages": 3,
        "coverage_percent": None, "coverage_missing": None,   # 键存在但值为 null
    }
    j = JobRecord.from_dict(old)
    assert j.coverage_percent == -1, "缺失/null 应落回默认值 -1，而不是 None"
    assert j.coverage_missing == 0
    assert j.coverage_total == 0
    assert j.cost_yuan == 0.0
    assert j.warning == ""


def test_load_legacy_batch_without_coverage_fields(tmp_path):
    """真实回归：加载「旧格式」batch.json（完全没有覆盖率字段）后重建总册不得崩。"""
    import json as _json

    from app.services.batch_runner import BATCH_JSON, rebuild_master

    b = _make_batch(tmp_path, n=1, batch_id="old1")
    for j in b.jobs:
        j.status = STATUS_DONE
        b.job_dir_of(j).mkdir(parents=True, exist_ok=True)
        (b.job_dir_of(j) / DOC_MD).write_text("# 第一节\n\n正文", encoding="utf-8")

    raw = b.to_dict()
    for item in raw["jobs"]:                      # 模拟旧格式：字段根本不存在
        for k in ("coverage_percent", "coverage_missing", "coverage_total"):
            item.pop(k, None)
    d = tmp_path / "old1"
    d.mkdir(parents=True, exist_ok=True)
    (d / BATCH_JSON).write_text(
        _json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    loaded = load_batch(tmp_path, "old1")
    assert loaded.jobs[0].coverage_percent == -1
    assert rebuild_master(loaded) is not None      # 关键：不得抛 TypeError
