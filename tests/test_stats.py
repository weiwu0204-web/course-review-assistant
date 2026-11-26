"""`services/stats.py` 的测试：统计只能报它真的知道的事。

三条要钉住的纪律（都是"看起来没问题、其实会骗人"的地方）：
1. **缺字段 ≠ 零**：老记录没有 token 埋点，不能当"花了 0 token"参与均值
2. **覆盖率 -1（不适用/没算）不能当 0%**：否则覆盖率凭空变差
3. **坏文件跳过**：一个半截 batch.json 不该让整张统计页空白
"""
from __future__ import annotations

import json

from app.services import stats


def _write_batch(root, batch_id: str, jobs: list[dict], course="Ceph", provider=None):
    d = root / batch_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "batch.json").write_text(
        json.dumps(
            {
                "batch_id": batch_id,
                "course_name": course,
                "provider": provider or {"model": "deepseek-v4-flash"},
                "status": "done",
                "jobs": jobs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _job(**kw):
    base = {"status": "done", "name": "x.pptx", "cost_yuan": 0.1, "cost_is_estimate": False}
    base.update(kw)
    return base


class TestGeneration:
    def test_空目录不炸(self, tmp_path):
        d = stats.collect(tmp_path)
        assert d["generation"]["n_jobs"] == 0
        assert d["generation"]["coverage"]["mean"] is None
        assert d["qa"]["n_questions"] == 0

    def test_token缺失的份不参与均值(self, tmp_path):
        """旧记录没有 prompt_tokens → 0。若把它算进均值，token 会被拉低。"""
        _write_batch(
            tmp_path,
            "b1",
            [
                _job(prompt_tokens=100, completion_tokens=50),
                _job(prompt_tokens=0, completion_tokens=0),  # 旧记录
            ],
        )
        g = stats.generation_stats(tmp_path)
        assert g["tokens"]["covered_jobs"] == 1
        assert g["tokens"]["prompt"] == 100
        assert g["tokens"]["completion"] == 50

    def test_覆盖率负一不参与统计(self, tmp_path):
        _write_batch(
            tmp_path,
            "b1",
            [
                _job(coverage_percent=96),
                _job(coverage_percent=100),
                _job(coverage_percent=-1),  # 习题档 / 术语太少
            ],
        )
        cov = stats.generation_stats(tmp_path)["coverage"]
        assert cov["n"] == 2
        assert cov["mean"] == 98.0
        assert cov["below_94"] == 0

    def test_低于94的份被单独数出来(self, tmp_path):
        _write_batch(
            tmp_path,
            "b1",
            [_job(coverage_percent=88), _job(coverage_percent=97)],
        )
        assert stats.generation_stats(tmp_path)["coverage"]["below_94"] == 1

    def test_估算成本单独计数(self, tmp_path):
        _write_batch(
            tmp_path,
            "b1",
            [
                _job(cost_yuan=0.2, cost_is_estimate=False),
                _job(cost_yuan=0.3, cost_is_estimate=True),
            ],
        )
        g = stats.generation_stats(tmp_path)
        assert g["cost_yuan"] == 0.5
        assert g["cost_measured_yuan"] == 0.2
        assert g["cost_estimated_jobs"] == 1

    def test_失败的份不计入成本与覆盖率(self, tmp_path):
        _write_batch(
            tmp_path,
            "b1",
            [_job(status="error", cost_yuan=9.9, coverage_percent=10)],
        )
        g = stats.generation_stats(tmp_path)
        assert g["cost_yuan"] == 0.0
        assert g["coverage"]["n"] == 0

    def test_坏batch文件被跳过(self, tmp_path):
        _write_batch(tmp_path, "ok", [_job(cost_yuan=0.1)])
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "batch.json").write_text("{ 半截", encoding="utf-8")
        assert stats.generation_stats(tmp_path)["n_batches"] == 1

    def test_按课程汇总并排序(self, tmp_path):
        _write_batch(tmp_path, "b1", [_job(), _job()], course="Ceph")
        _write_batch(tmp_path, "b2", [_job()], course="计算机网络")
        rows = stats.generation_stats(tmp_path)["courses"]
        assert rows[0]["course"] == "Ceph" and rows[0]["n_jobs"] == 2
        assert rows[1]["course"] == "计算机网络"

    def test_模型名从job取不到时回退到批次(self, tmp_path):
        _write_batch(tmp_path, "b1", [_job()], provider={"model": "glm-4.7"})
        assert stats.generation_stats(tmp_path)["courses"][0]["models"] == ["glm-4.7"]


class TestQa:
    def _log(self, root, rows: list[dict]):
        d = root / "qa"
        d.mkdir(exist_ok=True)
        (d / "k1.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )

    def test_分项耗时算分位(self, tmp_path):
        self._log(
            tmp_path,
            [
                {"timings": {"first_token_ms": 1000, "rewrite_ms": 800, "total_ms": 3000}},
                {"timings": {"first_token_ms": 3000, "rewrite_ms": 1200, "total_ms": 6000}},
                {"timings": {"first_token_ms": 2000, "rewrite_ms": 1000, "total_ms": 4000}},
            ],
        )
        q = stats.qa_stats(tmp_path)
        assert q["first_token_ms"]["n"] == 3
        assert q["first_token_ms"]["p50"] == 2000
        assert q["rewrite_ms"]["p50"] == 1000

    def test_没有timings的旧记录不参与分位(self, tmp_path):
        self._log(tmp_path, [{"cost_yuan": 0.01}, {"timings": {"first_token_ms": 1500}}])
        q = stats.qa_stats(tmp_path)
        assert q["n_questions"] == 2
        assert q["first_token_ms"]["n"] == 1

    def test_引用可信率(self, tmp_path):
        self._log(
            tmp_path,
            [
                {"citations": {"total": 4, "supported": 3}},
                {"citations": {"total": 6, "supported": 6}},
            ],
        )
        c = stats.qa_stats(tmp_path)["citations"]
        assert c["total"] == 10 and c["supported"] == 9
        assert c["pass_rate"] == 90.0

    def test_坏行被跳过(self, tmp_path):
        d = tmp_path / "qa"
        d.mkdir()
        (d / "k1.jsonl").write_text('{"citations":{"total":2,"supported":2}}\n{坏行\n', encoding="utf-8")
        assert stats.qa_stats(tmp_path)["citations"]["total"] == 2
