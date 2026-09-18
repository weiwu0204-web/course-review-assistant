"""覆盖率接入生成链路的测试（P1 · B2）。

链路：run_single 算覆盖率 → GenerateOutcome 携带 → 
  · 单份：/api/generate 响应里回传给前端
  · 批量：写回 JobRecord + meta.json，并单独落盘 coverage.json

不真调模型：monkeypatch `ai_generator.build_client`，用假响应替换。
"""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import reload_settings
from app.main import app
from app.models import ExtractionResult, GenerateRequest, SlideContent
from app.services import ai_generator
from app.services import batch_runner as br
from app.services.coverage import CoverageReport, PageCoverage
from app.services.extractor import extract_any
from app.services.user_config import save_user_settings

client = TestClient(app)
SAMPLE_PPTX = Path(__file__).parent / "samples" / "sample_course.pptx"


def _with_key(monkeypatch, tmp_path):
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reload_settings()
    save_user_settings("deepseek", "sk-test-key")
    reload_settings()


class _FakeClient:
    """假 OpenAI client：create() 回一段固定内容。"""

    def __init__(self, content: str):
        self.calls: list[dict] = []
        outer = self

        def create(**kwargs):
            outer.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content), finish_reason="stop"
                    )
                ],
                usage=None,
            )

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _patch_client(monkeypatch, content: str) -> _FakeClient:
    fake = _FakeClient(content)
    monkeypatch.setattr(ai_generator, "build_client", lambda settings: fake)
    return fake


# ---------------------------------------------------------------- 单份端点


def _setup_upload(monkeypatch, tmp_path) -> tuple[Path, str]:
    """把样件放进隔离的 uploads 目录，返回 (src_path, filename)。"""
    from app.api import routes

    uploads = tmp_path / "uploads"
    up_id = "u1"
    (uploads / up_id).mkdir(parents=True, exist_ok=True)
    dst = uploads / up_id / SAMPLE_PPTX.name
    shutil.copyfile(SAMPLE_PPTX, dst)
    monkeypatch.setattr(routes, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(routes, "EXAM_DIR", uploads / "exams")
    return dst, f"{up_id}/{SAMPLE_PPTX.name}"


def test_single_endpoint_returns_coverage(monkeypatch, tmp_path):
    """把源课件文字原样当成品 → 术语必然全中，覆盖率应接近满分。"""
    _with_key(monkeypatch, tmp_path)
    dst, filename = _setup_upload(monkeypatch, tmp_path)
    result = extract_any(dst, image_dir=dst.parent / "images")
    source_text = "\n".join(s.text + " " + s.notes for s in result.slides)
    _patch_client(monkeypatch, source_text)

    res = client.post("/api/generate", json={"filename": filename, "use_vision": False})
    assert res.status_code == 200, res.text
    cov = res.json()["coverage"]
    assert cov is not None
    assert cov["total_terms"] > 0, "样例课件应能抽出术语，否则判据失效"
    assert cov["percent"] >= 95, cov
    assert cov["missing_detail"] == []


def test_single_endpoint_coverage_flags_unrelated_doc(monkeypatch, tmp_path):
    """成品与课件无关 → 覆盖率应显著偏低，且给出「疑似未覆盖」条目与页码。"""
    _with_key(monkeypatch, tmp_path)
    _, filename = _setup_upload(monkeypatch, tmp_path)
    _patch_client(monkeypatch, "这份文档讲的是别的东西，与课件无关。")

    res = client.post("/api/generate", json={"filename": filename, "use_vision": False})
    cov = res.json()["coverage"]
    assert cov["percent"] < 60, cov
    assert cov["missing_terms"] > 0
    assert cov["missing_detail"], "应给出可下钻的缺失条目"
    assert all({"term", "page"} == set(x) for x in cov["missing_detail"])


# ---------------------------------------------------------------- 批量落盘


def _outcome(markdown: str = "# 文档", percent: int | None = 91) -> ai_generator.GenerateOutcome:
    """造一个带覆盖率的 outcome。percent=91 → 100 个术语里缺 9 个（数字自洽，免得断言对不上）。"""
    cov = None
    if percent is not None:
        missing = 100 - percent
        cov = CoverageReport(
            filename="x.pptx",
            total_terms=100,
            missing_terms=missing,
            pages=[PageCoverage(page=1, total=100, missing=["crush"] * missing)],
            missing_detail=[("crush", 1)],
        )
        assert cov.percent == percent, "用例自身的数要自洽"
    return ai_generator.GenerateOutcome(markdown=markdown, coverage=cov)


def _batch(tmp_path, batch_id: str = "cov1") -> br.Batch:
    jobs = [
        br.JobRecord(
            idx=1,
            name="第1章.pptx",
            slug=br.slugify("第1章.pptx"),
            src_pptx=str(tmp_path / "src.pptx"),
        )
    ]
    return br.Batch(
        batch_id=batch_id,
        course_name="覆盖率测试课",
        output_root=str(tmp_path),
        provider={"name": "fake", "price_in": 3.0, "price_out": 9.0},
        options={"template": "复习指南", "style": "详细", "use_vision": False,
                 "thinking": False},
        jobs=jobs,
        status=br.BATCH_RUNNING,
    )


def test_job_default_has_no_coverage_data():
    """默认 -1 = 未计算。**不能默认 0**，否则旧任务会被误读成「覆盖率为零」。"""
    job = br.JobRecord(idx=1, name="a.pptx", slug="a", src_pptx="a.pptx")
    assert job.coverage_percent == -1
    assert job.coverage_missing == 0
    assert job.to_dict()["coverage_percent"] == -1


def test_save_job_artifacts_writes_coverage_json(tmp_path):
    """有覆盖率时：meta.json 带摘要，coverage.json 单独落盘供下钻。"""
    batch = _batch(tmp_path)
    job = batch.jobs[0]
    job.status = br.STATUS_DONE
    job.coverage_percent = 91
    job.coverage_missing = 3
    br.save_job_artifacts(batch, job, _outcome())

    d = batch.job_dir_of(job)
    meta = json.loads((d / br.META_JSON).read_text(encoding="utf-8"))
    assert meta["coverage_percent"] == 91
    assert meta["coverage_missing"] == 3

    cov = json.loads((d / br.COVERAGE_JSON).read_text(encoding="utf-8"))
    assert cov["percent"] == 91
    assert cov["missing_detail"] == [{"term": "crush", "page": 1}]


def test_no_coverage_json_when_not_computed(tmp_path):
    """没算出来就不写文件——留个空文件会让读取方以为「算过了但没内容」。"""
    batch = _batch(tmp_path, batch_id="cov2")
    job = batch.jobs[0]
    job.status = br.STATUS_DONE
    br.save_job_artifacts(batch, job, _outcome(percent=None))

    d = batch.job_dir_of(job)
    assert not (d / br.COVERAGE_JSON).exists()
    meta = json.loads((d / br.META_JSON).read_text(encoding="utf-8"))
    assert meta["coverage_percent"] == -1


def test_runner_writes_coverage_back_to_batch_json(tmp_path, monkeypatch):
    """跑完一份后，batch.json 里应带上该份的覆盖率——前端只轮询这个文件。"""
    batch = _batch(tmp_path, batch_id="cov3")

    def extract(src, img_dir):
        return ExtractionResult(
            filename="第1章.pptx",
            total_pages=1,
            slides=[SlideContent(page=1, text="Ceph 的 CRUSH 算法", notes="")],
        )

    runner = br.BatchRunner(
        batch, extract_fn=extract, generate_fn=lambda result, request, exam: _outcome()
    )
    runner.run()

    assert batch.status == br.BATCH_DONE
    reloaded = br.load_batch(tmp_path, "cov3")
    assert reloaded.jobs[0].coverage_percent == 91
    assert reloaded.jobs[0].coverage_missing == 9


# ---------------------------------------------------------------- run_single 兜底


def test_run_single_survives_measure_failure(monkeypatch, tmp_path):
    """覆盖率算挂了不能把已经生成好的文档作废——它只是辅助信息。"""
    _with_key(monkeypatch, tmp_path)
    _patch_client(monkeypatch, "文档正文")

    def boom(*a, **k):
        raise RuntimeError("判据内部炸了")

    monkeypatch.setattr(ai_generator, "measure", boom)
    result = ExtractionResult(
        filename="x.pptx", total_pages=1,
        slides=[SlideContent(page=1, text="Ceph 的 CRUSH 算法", notes="")],
    )
    out = ai_generator.run_single(
        result, GenerateRequest(filename="x.pptx", use_vision=False)
    )
    assert out.markdown == "文档正文"
    assert out.coverage is None, "算失败应记为 None（无数据），而不是抛出"
