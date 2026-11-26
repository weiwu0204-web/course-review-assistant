"""`services/kb.py` 的测试：知识库指纹与课程名。

**这一组的核心是那个 mtime 回归**：上传副本的 mtime 恒等于「写入时刻」，
把 mtime 算进指纹会让同一份课件每次建库都换 kb_id ——
缓存永不命中、历史记录各挂一个 id 无法归组、`output/kb/` 还会无限堆积。
这个缺陷在写按课程归组时才暴露出来（因为归组开始依赖 kb_id 的稳定性）。
"""
from __future__ import annotations

import os

import pytest

from app.services import kb
from app.services.citation import CitationVerifier
from app.services.retriever import BM25Index, Chunk


def make_kb(kb_id: str = "abc123", course_name: str = "") -> kb.KnowledgeBase:
    chunk = Chunk(
        key="a.pdf::1",
        file="a.pdf",
        page=1,
        title="纠删码",
        text="纠删码通过编码冗余实现数据恢复，副本数为 3。",
    )
    index = BM25Index([chunk], extra_terms=set())
    return kb.KnowledgeBase(
        kb_id=kb_id,
        files=["a.pdf"],
        chunks=[chunk],
        index=index,
        verifier=CitationVerifier([chunk], index=index, course_terms={}),
        course_terms=set(),
        n_pages=1,
        n_chars=len(chunk.text),
        course_name=course_name,
    )


class TestFingerprint:
    def test_同内容不同mtime得到同一kb_id(self, tmp_path):
        """★ 回归：这正是「同一批课件重复建库」的实际形态。

        上传副本每次都是新写入的文件（mtime = 现在），所以指纹里一旦含 mtime，
        同一份课件永远得不到同一个 kb_id —— 实测 `1fcb2694ef42` vs `19ad053014bd`。
        """
        a = tmp_path / "a" / "x.pdf"
        b = tmp_path / "b" / "x.pdf"
        for p, t in ((a, 1_000_000), (b, 2_000_000)):
            p.parent.mkdir(parents=True)
            p.write_bytes(b"%PDF-1.4 fake content")
            os.utime(p, (t, t))

        assert kb._fingerprint([a]) == kb._fingerprint([b])

    def test_同大小不同内容得到不同kb_id(self, tmp_path):
        """mtime 去掉之后，这条是"内容变了要失效"的保底。"""
        a = tmp_path / "a" / "x.pdf"
        b = tmp_path / "b" / "x.pdf"
        for p, content in ((a, b"AAAA"), (b, b"BBBB")):  # 同名同大小，内容不同
            p.parent.mkdir(parents=True)
            p.write_bytes(content)
        assert kb._fingerprint([a]) != kb._fingerprint([b])

    def test_文件名不同得到不同kb_id(self, tmp_path):
        a = tmp_path / "a.pdf"
        b = tmp_path / "b.pdf"
        a.write_bytes(b"same")
        b.write_bytes(b"same")
        assert kb._fingerprint([a]) != kb._fingerprint([b])

    def test_顺序不影响指纹(self, tmp_path):
        """前端选文件的顺序不该产生不同知识库。"""
        a = tmp_path / "a.pdf"
        b = tmp_path / "b.pdf"
        a.write_bytes(b"1111")
        b.write_bytes(b"2222")
        assert kb._fingerprint([a, b]) == kb._fingerprint([b, a])

    def test_文件不存在不抛异常(self, tmp_path):
        """读不到就标记 missing，不能让建库在中途炸掉。"""
        assert kb._fingerprint([tmp_path / "不存在.pdf"])


class TestCourseName:
    def test_序列化往返保留课程名(self):
        restored = kb._deserialize(kb._serialize(make_kb(course_name="计算机组成原理")))
        assert restored.course_name == "计算机组成原理"

    def test_旧缓存没有该字段时退回空串(self):
        """加这个字段之前写的 kb.json 里没有 course_name —— 不能崩。"""
        data = kb._serialize(make_kb())
        data.pop("course_name")
        assert kb._deserialize(data).course_name == ""

    def test_命中缓存时补写课程名(self, tmp_path, monkeypatch):
        """用户第一次建库没填名字，后来填了：索引不动，只补标签。

        **不重建索引**是刻意的——课程名不参与 kb_id 的计算，
        改它不该让用户再等 7 秒提取。
        """
        monkeypatch.setattr(kb, "KB_ROOT", tmp_path / "kb")
        f = tmp_path / "a.pdf"
        f.write_bytes(b"%PDF fake")
        kb_id = kb._fingerprint([f])

        kb._write_cache(make_kb(kb_id=kb_id, course_name=""))  # 先放一份无名缓存
        out = kb.build([f], course_name="计算机组成原理")

        assert out.kb_id == kb_id
        assert out.course_name == "计算机组成原理"
        # 必须落盘：否则下次读回来又没名字了
        assert kb.load(kb_id).course_name == "计算机组成原理"

    def test_已有课程名不被空值冲掉(self, tmp_path, monkeypatch):
        """第二次建库没填名字，不该把已有的名字擦掉。"""
        monkeypatch.setattr(kb, "KB_ROOT", tmp_path / "kb")
        f = tmp_path / "a.pdf"
        f.write_bytes(b"%PDF fake")
        kb_id = kb._fingerprint([f])
        kb._write_cache(make_kb(kb_id=kb_id, course_name="计算机组成原理"))

        assert kb.build([f], course_name="").course_name == "计算机组成原理"

    def test_stats带回课程名(self):
        assert make_kb(course_name="计组").stats()["course_name"] == "计组"


class TestBuildGuards:
    def test_空文件列表抛ValueError(self):
        with pytest.raises(ValueError):
            kb.build([])
