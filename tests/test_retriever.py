"""P3a 检索层单测：分词 / 分块 / BM25 / 落盘 / 真实课件回归。

最后那组「真实课件回归」是本文件的重点：其余用例保证**逻辑对**，
它保证**在真语料上真的检索得到**——两者的失败模式完全不同。
（历史教训：覆盖率那边单测全绿、真跑才发现判据选错。）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import ExtractionResult, SlideContent
from app.services.retriever import (
    BM25Index,
    Chunk,
    build_chunks,
    chunks_by_key,
    tokenize,
)

COURSE_DIR = Path(__file__).resolve().parent.parent / "课件"


# ------------------------------------------------------------------ 分词


class TestTokenize:
    def test_英文术语被切出且小写化(self):
        assert "crush" in tokenize("CRUSH 算法负责数据分布")
        assert "pg_num" in tokenize("设置 pg_num 为 128")

    def test_数字带单位作为整词(self):
        """★ 第三层缺失时中文召回腰斩，所以这一条是防倒退的关键用例。"""
        assert "3副本" in tokenize("默认 3 副本")
        assert "64kb" in tokenize("块大小 64KB")
        assert "0.85" in tokenize("阈值设为 0.85")

    def test_中文按2gram展开(self):
        tokens = tokenize("分布式存储")
        assert tokens[-3:] == ["分布", "布式", "式存"] or "分布" in tokens
        assert "存储" in tokens

    def test_单字中文也保留(self):
        assert "盘" in tokenize("盘")

    def test_不产生空token(self):
        assert all(t for t in tokenize("CRUSH 3副本 分布式"))


# ------------------------------------------------------------------ 分块


def _result(filename: str, slides: list[tuple[int, str, str]]) -> ExtractionResult:
    return ExtractionResult(
        filename=filename,
        total_pages=len(slides),
        slides=[
            SlideContent(page=p, text=t, notes=n) for p, t, n in slides
        ],
    )


class TestBuildChunks:
    def test_key是文件名加页码而不是下标(self):
        """key 必须是内容决定的：索引会存盘重建，用下标做标识一旦顺序变了引用全错位。"""
        chunks = build_chunks([_result("a.pptx", [(1, "第一页", "")])])
        assert chunks[0].key == "a.pptx::1"

    def test_备注被并入正文(self):
        """老师把考点写在备注里是常态，漏掉备注等于漏掉考点。"""
        chunks = build_chunks([_result("a.pptx", [(1, "标题", "这里是考点")])])
        assert "考点" in chunks[0].text

    def test_空白页被跳过(self):
        chunks = build_chunks(
            [_result("a.pptx", [(1, "有内容", ""), (2, "   ", "  "), (3, "也有", "")])]
        )
        assert [c.page for c in chunks] == [1, 3]

    def test_多份课件页码不冲突(self):
        chunks = build_chunks(
            [_result("a.pptx", [(1, "甲", "")]), _result("b.pptx", [(1, "乙", "")])]
        )
        assert len({c.key for c in chunks}) == 2
        assert {c.file for c in chunks} == {"a.pptx", "b.pptx"}

    def test_标题行被识别(self):
        chunks = build_chunks([_result("a.pptx", [(1, "Ceph 架构概述\n正文内容", "")])])
        assert chunks[0].title == "Ceph 架构概述"

    def test_超长首行不作为标题(self):
        chunks = build_chunks([_result("a.pptx", [(1, "很" * 50, "")])])
        assert chunks[0].title == ""

    def test_excerpt超长时截断(self):
        chunk = Chunk("k", "f", 1, "", "字" * 1000)
        assert chunk.excerpt(100).endswith("…")


# ------------------------------------------------------------------ BM25


@pytest.fixture
def small_index() -> BM25Index:
    return BM25Index(
        [
            Chunk("a.pptx::1", "a.pptx", 1, "", "Ceph 集群扩容 新增 OSD 节点 规模"),
            Chunk("a.pptx::2", "a.pptx", 2, "", "CRUSH 算法负责数据分布与映射"),
            Chunk("b.pptx::1", "b.pptx", 1, "", "纠删码 erasure coding 编码 冗余"),
        ]
    )


class TestBM25:
    def test_能命中最相关的页(self, small_index):
        hits = small_index.search("CRUSH 数据分布")
        assert hits[0].chunk.key == "a.pptx::2"

    def test_rank从1开始且递增(self, small_index):
        hits = small_index.search("Ceph")
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))

    def test_topk生效(self, small_index):
        assert len(small_index.search("Ceph", topk=1)) == 1

    def test_scope限定到某一份课件(self, small_index):
        hits = small_index.search("纠删码", scope="a.pptx")
        assert all(h.chunk.file == "a.pptx" for h in hits)

    def test_scope之外的好结果不该漏回(self, small_index):
        """scope 过滤在排序之后做——这个用例防的是"先截断 topk 再过滤"导致结果变少。"""
        hits = small_index.search("纠删码", topk=3, scope="b.pptx")
        assert [h.chunk.key for h in hits] == ["b.pptx::1"]

    def test_无匹配词返回空(self, small_index):
        assert small_index.search("zzzz不存在的词zzzz") == []

    def test_空索引被拒绝(self):
        with pytest.raises(ValueError):
            BM25Index([])

    def test_分数降序(self, small_index):
        scores = [h.score for h in small_index.search("Ceph 节点 编码")]
        assert scores == sorted(scores, reverse=True)


class TestPersistence:
    def test_存档往返一致(self, small_index, tmp_path):
        path = tmp_path / "index.json"
        small_index.save(path)
        restored = BM25Index.load(path)
        assert [c.key for c in restored.chunks] == [c.key for c in small_index.chunks]
        assert [c.text for c in restored.chunks] == [c.text for c in small_index.chunks]

    def test_往返后检索结果不变(self, small_index, tmp_path):
        path = tmp_path / "index.json"
        small_index.save(path)
        restored = BM25Index.load(path)
        before = [h.chunk.key for h in small_index.search("CRUSH 数据分布")]
        after = [h.chunk.key for h in restored.search("CRUSH 数据分布")]
        assert before == after

    def test_存的是chunk不是索引(self, small_index, tmp_path):
        """落盘只存 chunk：索引重建 11ms 可忽略，而 chunk 的上游（提取课件）是秒级。"""
        path = tmp_path / "index.json"
        small_index.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list) and set(data[0]) == {"key", "file", "page", "title", "text"}


class TestChunksByKey:
    def test_按文件名加页码反查(self):
        chunks = [
            Chunk("a.pptx::1", "a.pptx", 1, "", "x"),
            Chunk("b.pptx::1", "b.pptx", 1, "", "y"),
        ]
        mapping = chunks_by_key(chunks)
        assert mapping["a.pptx::1"].text == "x"
        assert mapping["b.pptx::1"].file == "b.pptx"


# ------------------------------------------------------------------ 真实课件回归


requires_course = pytest.mark.skipif(
    not COURSE_DIR.exists() or not list(COURSE_DIR.glob("*.pptx")),
    reason="课件目录不存在或为空，跳过真实语料回归",
)


@pytest.fixture(scope="module")
def real_index() -> BM25Index:
    from app.services.extractor import extract_any

    results = [extract_any(p) for p in sorted(COURSE_DIR.glob("*.pptx"))]
    return BM25Index(build_chunks(results))


@requires_course
class TestRealCourseRegression:
    """在真实 14 份 Ceph 课件上验证检索——探针的读数要能被复现。

    这组用例的意义：上面所有用例都可能在"逻辑对但语料不适配"的情况下全绿。
    """

    def test_规模与探针一致(self, real_index):
        assert len(real_index.chunks) == 325

    def test_技术术语能命中正确课件(self, real_index):
        hits = real_index.search("CRUSH 算法", topk=5)
        assert any("核心组件" in h.chunk.file for h in hits)

    def test_数字阈值能检索到内容(self, real_index):
        """数字类查询曾因分词缺一层而 100% 失败，这条是防倒退。"""
        hits = real_index.search("3副本", topk=5)
        assert hits, "数字类查询必须能检索到内容"

    def test_中文术语能命中正确课件(self, real_index):
        hits = real_index.search("纠删码", topk=5)
        assert any("分布式存储概述" in h.chunk.file for h in hits)

    def test_scope限定单份课件可用(self, real_index):
        target = "Ceph 块存储（RBD）配置与使用.pptx"
        hits = real_index.search("块存储 创建 映像", topk=5, scope=target)
        assert hits and all(h.chunk.file == target for h in hits)
