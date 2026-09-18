"""批任务 API 测试：建批校验、全流程（建批→轮询→总册/单节下载）、失败重试、取消守卫。

不真调模型：monkeypatch batch_runner 模块命名空间里的 extract_pptx / run_single——
BatchRunner 的默认实现（extract_fn 默认 lambda、generate_fn 默认 run_single）都从该
命名空间取，patch 后 API 全链路走假实现，只验证路由/状态机/落盘/文件下载。
"""
import io
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import reload_settings
from app.main import app
from app.models import ExtractionResult, SlideContent
from app.services import batch_runner as br
from app.services.ai_generator import GenerateOutcome
from app.services.user_config import save_user_settings

client = TestClient(app)


def _isolate(monkeypatch, tmp_path):
    """配置隔离（不碰真实 %APPDATA%）+ output 根目录指到 tmp。"""
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(br, "OUTPUT_ROOT", tmp_path / "output")
    reload_settings()


def _with_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    save_user_settings("deepseek", "sk-test-key")
    reload_settings()


def _fake_extract(src, img_dir=None, **kwargs):
    """兼容两种调用形态：BatchRunner 默认 lambda 与 peek 都以 image_dir= 关键字传参。"""
    stem = Path(src).stem
    return ExtractionResult(
        filename=str(src),
        total_pages=2,
        title=stem,
        image_count=0,
        notes_chars=0,
        slides=[SlideContent(page=1, text="第一章 内容", notes=""),
                SlideContent(page=2, text="考点", notes="")],
    )


def _fake_run_factory(fail_first_for: str = ""):
    """生成假 run_single：可选让某文件第一次调用抛错（验证重试链路）。"""
    calls: dict[str, int] = {}

    def run(result, request, exam_text=""):
        name = Path(result.filename).stem
        calls[name] = calls.get(name, 0) + 1
        if fail_first_for and fail_first_for in name and calls[name] == 1:
            raise RuntimeError("boom")
        return GenerateOutcome(
            markdown=f"# {name} 复习\n\n正文",
            image_count=0,
            truncated=False,
            used_vision=False,
            exam_used=bool(exam_text),
            usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        )

    return run


def _files(*names):
    return [("files", (n, io.BytesIO(b"fake pptx bytes"), "application/octet-stream"))
            for n in names]


def _wait(batch_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/batch/{batch_id}").json()
        if d.get("status") != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("批任务超时未结束")


# ---------- 建批校验 ----------

def test_start_requires_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    res = client.post("/api/batch/start", files=_files("a.pptx"))
    assert res.status_code == 400
    assert "API Key" in res.json()["detail"]


def test_start_rejects_non_pptx(monkeypatch, tmp_path):
    _with_key(monkeypatch, tmp_path)
    res = client.post("/api/batch/start", files=_files("笔记.txt"))
    assert res.status_code == 400
    assert "pptx" in res.json()["detail"]


def test_start_rejects_too_many(monkeypatch, tmp_path):
    from app.api.batch_routes import MAX_BATCH_FILES

    _with_key(monkeypatch, tmp_path)
    many = [f"{i:02d}.pptx" for i in range(1, MAX_BATCH_FILES + 2)]  # 上限 +1 份
    res = client.post("/api/batch/start", files=_files(*many))
    assert res.status_code == 400
    assert "最多" in res.json()["detail"]


def test_start_rejects_ppt_legacy(monkeypatch, tmp_path):
    _with_key(monkeypatch, tmp_path)
    res = client.post("/api/batch/start", files=_files("老课件.ppt"))
    assert res.status_code == 400
    assert "另存为" in res.json()["detail"]


# ---------- 全流程 ----------

def test_full_flow(monkeypatch, tmp_path):
    """建批 → 轮询到 done → 总册/单节可下载 → 历史列表可见。"""
    _with_key(monkeypatch, tmp_path)
    monkeypatch.setattr(br, "extract_any", _fake_extract)
    monkeypatch.setattr(br, "run_single", _fake_run_factory())

    res = client.post(
        "/api/batch/start",
        files=_files("01_第一章.pptx", "02_第二章.pptx"),
        data={"course_name": "计算机网络", "style": "详细"},
    )
    assert res.status_code == 200
    started = res.json()
    bid = started["batch_id"]
    # 建批响应必须自带 jobs：前端拿到后立即渲染作业列表，缺这个字段会抛错、
    # 连带把轮询启动也吞掉，界面永远停在 0/N（2026-09-10 用户实测踩到）。
    assert [j["name"] for j in started["jobs"]] == ["01_第一章.pptx", "02_第二章.pptx"]
    assert all(j["status"] == "waiting" for j in started["jobs"])

    d = _wait(bid)
    assert d["status"] == "done"
    assert d["total"] == 2 and d["done"] == 2 and d["error"] == 0
    assert d["master_ready"] is True
    # usage 实算记账：100×3 + 200×9 = 2100 / 1e6 元/份
    assert d["spent_yuan"] > 0
    assert all("src_pptx" not in j for j in d["jobs"])  # 本地路径不外泄

    # 总册：课程名 + 两节 + 出处锚点 + 原样正文
    m = client.get(f"/api/batch/{bid}/master")
    assert m.status_code == 200
    text = m.text
    assert "《计算机网络》期末复习总册" in text
    assert "## 第 1 节：01_第一章 复习" in text
    assert "## 第 2 节：02_第二章 复习" in text
    assert "> 来源：01_第一章.pptx（共 2 页）" in text
    assert "正文" in text

    # 单节下载 + 越界 404
    assert client.get(f"/api/batch/{bid}/doc/1").status_code == 200
    assert client.get(f"/api/batch/{bid}/doc/3").status_code == 404

    # 历史列表包含该批
    lst = client.get("/api/batches").json()
    assert any(b["batch_id"] == bid for b in lst["batches"])
    # 单批上限由后端下发（前端不留第二份常量，避免两处漂移）
    from app.api.batch_routes import MAX_BATCH_FILES
    assert lst["max_files"] == MAX_BATCH_FILES

    # 已结束的批不可取消
    assert client.post(f"/api/batch/{bid}/cancel").status_code == 400


def test_flow_with_failed_job_then_retry(monkeypatch, tmp_path):
    """第一份失败 → partial；retry 后重跑该份 → done。"""
    _with_key(monkeypatch, tmp_path)
    monkeypatch.setattr(br, "extract_any", _fake_extract)
    monkeypatch.setattr(br, "run_single", _fake_run_factory(fail_first_for="01_"))

    res = client.post("/api/batch/start", files=_files("01_第一章.pptx", "02_第二章.pptx"),
                      data={"course_name": "测试课"})
    bid = res.json()["batch_id"]
    d = _wait(bid)
    assert d["status"] == "partial"
    assert d["jobs"][0]["status"] == "error"
    assert "boom" in d["jobs"][0]["error"]
    assert d["jobs"][1]["status"] == "done"
    assert d["master_ready"] is True  # 已完成节仍可下载

    r = client.post(f"/api/batch/{bid}/retry")
    assert r.status_code == 200
    d2 = _wait(bid)
    assert d2["status"] == "done"
    assert all(j["status"] == "done" for j in d2["jobs"])


def test_peek(monkeypatch, tmp_path):
    """速览端点：只提取不生成，返回规模与预估费用。"""
    _with_key(monkeypatch, tmp_path)
    monkeypatch.setattr(br, "extract_any", _fake_extract)
    res = client.post(
        "/api/batch/peek",
        files=[("file", ("03_第三章.pptx", io.BytesIO(b"fake"), "application/octet-stream"))],
        data={"use_vision": "true", "thinking": "false", "style": "详细"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["filename"] == "03_第三章.pptx"
    assert data["total_pages"] == 2
    assert data["est_cost_yuan"] >= 0


def test_unknown_batch_404(monkeypatch, tmp_path):
    _with_key(monkeypatch, tmp_path)
    assert client.get("/api/batch/nonexist").status_code == 404
    assert client.get("/api/batch/nonexist/master").status_code == 404
    assert client.post("/api/batch/nonexist/cancel").status_code == 404


def test_orphaned_running_recovers_as_paused(monkeypatch, tmp_path):
    """进程重启后的悬空 running：必须报告为 paused，断点续跑才不是假死。

    场景：批任务跑在后台线程里，线程登记在内存；服务重启（run.py 开着
    reload，改文件即重启）后 batch.json 仍是 running 但没有工作线程。
    若不识别，前端会一直显示「正在生成」、轮询不停、「继续生成」按钮永不出现。
    """
    from app.api import batch_routes

    _with_key(monkeypatch, tmp_path)
    monkeypatch.setattr(br, "extract_any", _fake_extract)
    monkeypatch.setattr(br, "run_single", _fake_run_factory())

    bid = "crashed1"
    out = tmp_path / "output"
    batch = br.Batch(
        batch_id=bid,
        course_name="崩溃测试课",
        output_root=str(out),
        provider={},
        options={"template": "复习指南", "style": "详细",
                 "use_vision": True, "thinking": False},
        status=br.BATCH_RUNNING,
    )
    for i, name in enumerate(["第1章.pptx", "第2章.pptx"], start=1):
        jd = br.job_dir(out, bid, i, br.slugify(name))
        jd.mkdir(parents=True, exist_ok=True)
        src = jd / name
        src.write_bytes(b"fake")
        batch.jobs.append(
            br.JobRecord(idx=i, name=name, slug=br.slugify(name), src_pptx=str(src))
        )
    batch.jobs[0].status = br.STATUS_DONE
    batch.jobs[1].status = br.STATUS_RUNNING  # 崩溃瞬间正在跑的那份
    br.persist_batch(batch)
    assert bid not in batch_routes._LIVE_THREADS  # 本进程确实没有工作线程

    d = client.get(f"/api/batch/{bid}").json()
    assert d["status"] == "paused"              # 不再假死为 running
    assert d["jobs"][1]["status"] == "waiting"  # 悬空 running 已重置为可重跑

    # 刻意不落盘（避免轮询时与后台线程并发写 batch.json）：disk 上仍是 running，
    # 但 retry 的守卫判断 _alive 而非 status，所以「继续生成」照样能用。
    assert br.load_batch(out, bid).status == br.BATCH_RUNNING

    r = client.post(f"/api/batch/{bid}/retry")
    assert r.status_code == 200, r.text
    d2 = _wait(bid)
    assert d2["status"] == "done"
    assert all(j["status"] == "done" for j in d2["jobs"])

