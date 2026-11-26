"""`services/library.py` 的测试：按课程归组。

这里钉住三件容易做错的事：
1. **归组 key 是课程名**，不是文件名（同门课的课件常都叫 `讲义.pptx`）
2. **「未归类」沉底**，不按时间顶到最前面
3. **同 kb_id 的旧记录能被新记录带回课程名**——这是历史数据唯一的补救路径
"""
from __future__ import annotations

import json

import pytest

from app.services import batch_runner as br
from app.services import library


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 output 根与 qa 目录都指到临时目录，绝不碰真实历史。"""
    out = tmp_path / "output"
    out.mkdir()
    qa = out / "qa"
    qa.mkdir()
    monkeypatch.setattr(br, "OUTPUT_ROOT", out)
    monkeypatch.setattr(library, "QA_DIR", qa)
    return out, qa


def write_batch(out, batch_id: str, course_name: str, updated_at: str, n_jobs: int = 2):
    """写一份最小的 batch.json（够 Batch.from_dict 解析即可）。"""
    d = out / batch_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_id": batch_id,
        "course_name": course_name,
        "status": "done",
        "updated_at": updated_at,
        "jobs": [
            {
                "idx": i,
                "name": "讲义.pptx",  # ★ 刻意全用同一个名字：真实数据就是这样
                "slug": f"j{i}",
                "src_pptx": "",
                "status": "done",
                "cost_yuan": 0.25,
            }
            for i in range(1, n_jobs + 1)
        ],
    }
    (d / br.BATCH_JSON).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def append_qa(qa_dir, kb_id: str, records: list[dict]):
    path = qa_dir / f"{kb_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def qa_record(question: str, ts: str, course_name: str = "", kb_id: str = "kb1") -> dict:
    rec = {
        "ts": ts,
        "question": question,
        "rewritten": question,
        "rewrite_applied": False,
        "files": ["讲义.pptx"],
        "hits": [{"file": "讲义.pptx", "page": 1, "rank": 1}],
        "answer": "## 答案\n\n正文",
        "citations": {"total": 3, "supported": 2, "weak": 1, "not_found": 0},
        "cost_yuan": 0.01,
        "elapsed_s": 3.2,
    }
    if course_name:
        rec["course_name"] = course_name
    return rec


class TestGrouping:
    def test_同一课程的多个批次归到一组(self, env):
        """真实数据里「计算机网络」有 3 个批次，用户希望看到它们是一回事。"""
        out, _ = env
        write_batch(out, "b1", "计算机网络", "2026-09-11 11:29:17")
        write_batch(out, "b2", "计算机网络", "2026-09-14 15:59:47")
        write_batch(out, "b3", "计算机网络", "2026-09-11 20:55:53")

        courses = library.collect()
        assert len(courses) == 1
        assert courses[0]["name"] == "计算机网络"
        assert courses[0]["n_docs"] == 3
        # 组内按时间倒序
        assert [d["batch_id"] for d in courses[0]["docs"]] == ["b2", "b3", "b1"]

    def test_不同课程分开(self, env):
        out, _ = env
        write_batch(out, "b1", "计算机网络", "2026-09-11 11:29:17")
        write_batch(out, "b2", "计算机组成原理", "2026-09-14 15:59:47")
        courses = library.collect()
        assert {c["name"] for c in courses} == {"计算机网络", "计算机组成原理"}

    def test_课程名只差空白与大小写视为同一门(self, env):
        """规范化只做「去空白 + 大小写」，不做同义词合并。"""
        out, _ = env
        write_batch(out, "b1", "Computer Networks", "2026-09-11 11:29:17")
        write_batch(out, "b2", " computer  networks ", "2026-09-14 15:59:47")
        courses = library.collect()
        assert len(courses) == 1
        assert courses[0]["n_docs"] == 2

    def test_文件名相同也不影响归组(self, env):
        """★ 核心回归：所有 job 都叫 `讲义.pptx`，仍能按课程名正确归组。

        这正是不用文件名归组的原因——文件名在这里毫无区分度。
        """
        out, _ = env
        write_batch(out, "b1", "计算机网络", "2026-09-11 11:29:17", n_jobs=5)
        write_batch(out, "b2", "计算机组成原理", "2026-09-14 15:59:47", n_jobs=4)
        courses = library.collect()
        assert len(courses) == 2
        assert {c["name"]: c["n_docs"] for c in courses} == {
            "计算机网络": 1,
            "计算机组成原理": 1,
        }


class TestUnfiled:
    def test_没填课程名的记录归到未归类(self, env):
        out, qa = env
        append_qa(qa, "kbX", [qa_record("DMA 是什么", "2026-10-17T14:27:00")])
        courses = library.collect()
        assert len(courses) == 1
        assert courses[0]["name"] == library.UNFILED_LABEL
        assert courses[0]["n_qas"] == 1

    def test_未归类永远排在最后即使它最新(self, env):
        """★ 未归类不是"最新发生的"，是"不知道属于哪门课"——不该顶到第一眼。"""
        out, qa = env
        write_batch(out, "b1", "计算机网络", "2026-09-11 11:29:17")
        append_qa(qa, "kbX", [qa_record("这条更新", "2026-10-17T14:27:00")])

        courses = library.collect()
        assert [c["name"] for c in courses] == ["计算机网络", library.UNFILED_LABEL]

    def test_同库新记录能把旧记录带回课程(self, env):
        """★ 已有旧记录没有 course_name；用户用同一批课件再问一次带上名字后，
        旧记录必须跟着归到这门课下（同 kb_id ⇒ 同文件集合 ⇒ 同一门课，是事实不是推断）。
        """
        out, qa = env
        append_qa(
            qa,
            "kb1",
            [
                qa_record("旧问题", "2026-10-01T10:00:00"),  # 没有 course_name
                qa_record("新问题", "2026-10-17T10:00:00", course_name="计算机组成原理"),
            ],
        )
        courses = library.collect()
        assert len(courses) == 1
        assert courses[0]["name"] == "计算机组成原理"
        assert courses[0]["n_qas"] == 2

    def test_不同库的课程名互不串味(self, env):
        out, qa = env
        append_qa(qa, "kb1", [qa_record("甲问题", "2026-10-01T10:00:00", course_name="甲课")])
        append_qa(qa, "kb2", [qa_record("乙问题", "2026-10-02T10:00:00", course_name="乙课")])
        courses = library.collect()
        assert {c["name"] for c in courses} == {"甲课", "乙课"}

    def test_换了kb_id的同类课件按文件集合归位(self, env):
        """★ 历史补漏：kb 指纹曾含 mtime（已修），修复前的记录挂着旧 kb_id，
        永远对不上新 kb_id。但 files 列表是一致的，所以按文件集合能救回来。
        """
        out, qa = env
        files = ["4.3 高速缓冲存储器.pdf", "5.6 DMA方式.pdf"]
        old = qa_record("旧问题", "2026-10-01T10:00:00")
        old["files"] = files  # 没有 course_name，kb_id 也是旧的
        new = qa_record("新问题", "2026-10-17T10:00:00", course_name="计算机组成原理")
        new["files"] = files
        append_qa(qa, "oldkb", [old])
        append_qa(qa, "newkb", [new])

        courses = library.collect()
        assert len(courses) == 1
        assert courses[0]["name"] == "计算机组成原理"
        assert courses[0]["n_qas"] == 2

    def test_单份课件不按文件集合推断(self, env):
        """★ 单文件同名（`讲义.pptx`）的撞车风险太高 —— 归错比不归更糟。"""
        out, qa = env
        old = qa_record("甲课的问题", "2026-10-01T10:00:00")
        old["files"] = ["讲义.pptx"]
        new = qa_record("乙课的问题", "2026-10-17T10:00:00", course_name="乙课")
        new["files"] = ["讲义.pptx"]
        append_qa(qa, "oldkb", [old])
        append_qa(qa, "newkb", [new])

        names = {c["name"] for c in library.collect()}
        assert names == {"乙课", library.UNFILED_LABEL}


class TestQaRecords:
    def test_组内问答按时间倒序(self, env):
        out, qa = env
        append_qa(
            qa,
            "kb1",
            [
                qa_record("第一个", "2026-10-01T10:00:00", course_name="计组"),
                qa_record("第二个", "2026-10-02T10:00:00", course_name="计组"),
            ],
        )
        qas = library.collect()[0]["qas"]
        assert [q["question"] for q in qas] == ["第二个", "第一个"]

    def test_默认带回答案全文(self, env):
        """点击历史要能就地展开那次的答案，不能只剩一个问题。"""
        out, qa = env
        append_qa(qa, "kb1", [qa_record("DMA 是什么", "2026-10-17T10:00:00", course_name="计组")])
        assert library.collect()[0]["qas"][0]["answer"].startswith("## 答案")

    def test_可要求不返回答案(self, env):
        out, qa = env
        append_qa(qa, "kb1", [qa_record("DMA 是什么", "2026-10-17T10:00:00", course_name="计组")])
        assert "answer" not in library.collect(with_answers=False)[0]["qas"][0]

    def test_坏行跳过不丢整份(self, env):
        out, qa = env
        path = qa / "kb1.jsonl"
        path.write_text(
            json.dumps(qa_record("好问题", "2026-10-17T10:00:00", course_name="计组"), ensure_ascii=False)
            + "\n{这不是 json\n"
            + json.dumps(qa_record("另一个好问题", "2026-10-17T11:00:00", course_name="计组"), ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        qas = library.collect()[0]["qas"]
        assert {q["question"] for q in qas} == {"好问题", "另一个好问题"}

    def test_引用待核数量单独可读(self, env):
        """前端要显示「几 条待核」——weak 与 not_found 都算，坏消息不能藏。"""
        out, qa = env
        rec = qa_record("问题", "2026-10-17T10:00:00", course_name="计组")
        rec["citations"] = {"total": 5, "supported": 2, "weak": 2, "not_found": 1}
        append_qa(qa, "kb1", [rec])
        q = library.collect()[0]["qas"][0]
        assert q["citations"]["weak"] == 2
        assert q["citations"]["not_found"] == 1


class TestRobustness:
    def test_空目录返回空列表(self, env):
        assert library.collect() == []
        assert library.stats()["n_courses"] == 0

    def test_损坏的batch点json不挡列表(self, env):
        out, _ = env
        write_batch(out, "good", "计算机网络", "2026-09-11 11:29:17")
        bad = out / "bad"
        bad.mkdir()
        (bad / br.BATCH_JSON).write_text("{坏 json", encoding="utf-8")
        courses = library.collect()
        assert [c["name"] for c in courses] == ["计算机网络"]

    def test_stats把未归类单独计数(self, env):
        out, qa = env
        write_batch(out, "b1", "计算机网络", "2026-09-11 11:29:17")
        append_qa(qa, "kbX", [qa_record("无主问题", "2026-10-17T10:00:00")])
        s = library.stats()
        assert s == {"n_courses": 1, "n_unfiled": 1}
