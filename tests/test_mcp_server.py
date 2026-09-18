"""P2a 单测：补丁插入（纯函数）/ 会话逻辑 / 走真实 MCP stdio 协议的端到端。

分三层测是有意的：
    · 纯函数层 —— 插入与定位规则，跑得快、覆盖边界
    · 会话层   —— 工具的业务逻辑（校验、回检、落盘），不起子进程
    · 协议层   —— 一个端到端用例，确认 stdio + JSON-RPC 真的通
      （这一层慢，但它是 P2a 的存在意义；协议不通后面全白搭）
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.mcp_server import RepairSession
from app.models import ExtractionResult, SlideContent
from app.services.doc_patch import FALLBACK_HEADING, insert_section, outline_of
from app.services.mcp_client import open_repair_session

# ---------------------------------------------------------------- 测试数据

DOC = "# 第1节 复习指南\n\n## 知识点\n\nCRUSH 算法是 Ceph 的核心机制。\n"


def _extraction() -> ExtractionResult:
    """两页小课件：术语足够多（≥8）以便 reliable 为真。"""
    return ExtractionResult(
        filename="第1节.pptx",
        total_pages=2,
        slides=[
            SlideContent(
                page=1,
                text="CRUSH 算法负责数据分布；rados bench 用于性能测试；osd 负责数据存储",
            ),
            SlideContent(
                page=2,
                text="pg_num 决定归置组数量；ceph osd tree 查看拓扑；mon 监控集群状态",
                notes="重点：pg_num 与 pgp_num 必须保持一致",
            ),
        ],
    )


PATCH = (
    "补充：rados bench 是课件里介绍的性能测试工具（源：第 1 页）；"
    "pg_num 决定归置组数量、pgp_num 须与之一致（源：第 2 页）；"
    "osd 负责实际数据存储，用 ceph osd tree 查看拓扑（源：第 2 页）。"
)


def _session(tmp_path) -> RepairSession:
    return RepairSession(
        extraction=_extraction(), document=DOC, doc_out=tmp_path / "doc.md"
    )


# ---------------------------------------------------------------- 1. 纯函数层


def test_insert_into_existing_heading():
    new, located = insert_section(DOC, "知识点", "补充一段")
    assert located == "heading"
    assert "补充一段" in new
    # 插在小节内、原文之后
    assert new.index("CRUSH 算法") < new.index("补充一段")


def test_unknown_heading_falls_back_quietly():
    """标题写错不能报错——报错会让模型反复重试，白烧 token。"""
    new, located = insert_section(DOC, "根本不存在的小节", "补充一段")
    assert located == "fallback"
    assert FALLBACK_HEADING in new
    assert "补充一段" in new
    assert "CRUSH 算法" in new  # 原文完好


def test_consecutive_fallbacks_reuse_same_section():
    d1, _ = insert_section(DOC, None, "第一段")
    d2, _ = insert_section(d1, None, "第二段")
    assert d2.count(f"## {FALLBACK_HEADING}") == 1
    assert "第一段" in d2 and "第二段" in d2


def test_outline_lists_h2_only():
    doc = "# 一级\n\n## 甲\n\n### 三级\n\n## 乙\n"
    assert outline_of(doc) == ["甲", "乙"]


def test_empty_patch_rejected():
    with pytest.raises(ValueError):
        insert_section(DOC, "知识点", "   ")


def test_crlf_style_preserved():
    """文档是 CRLF 时不能被改写成 LF（Windows 上导出/比对会因此不一致）。"""
    crlf = DOC.replace("\n", "\r\n")
    new, _ = insert_section(crlf, "知识点", "补充")
    assert "\r\n" in new
    assert "\n\n" not in new.replace("\r\n", "")  # 没有残留裸 LF


# ---------------------------------------------------------------- 2. 会话层


def test_coverage_dict_includes_outline_for_positioning(tmp_path):
    """模型看不到全文，必须靠 outline 才知道能插到哪。"""
    d = _session(tmp_path).coverage_dict()
    assert d["outline"] == ["知识点"]
    assert d["reliable"] is True
    assert d["missing_terms"] > 0
    assert d["percent"] < 100
    assert all({"term", "page"} <= set(m) for m in d["missing_detail"])


def test_source_page_content_notes_and_bounds(tmp_path):
    s = _session(tmp_path)
    p1 = s.source_page(1)
    assert "CRUSH" in p1["text"] and p1["truncated"] is False
    p2 = s.source_page(2)
    assert "【备注】" in p2["text"] and "pgp_num" in p2["text"]
    bad = s.source_page(99)
    assert "error" in bad  # 越界给错误信息而不是抛异常


def test_submit_patch_validation(tmp_path):
    s = _session(tmp_path)
    assert s.submit_patch(None, "")["applied"] is False
    assert s.submit_patch(None, "x" * 2500)["applied"] is False  # 超 2000 字
    assert s.submit_patch(None, "第一段")["applied"] is True
    dup = s.submit_patch(None, "第一段")
    assert dup["applied"] is False and "相同" in dup["reason"]


def test_submit_patch_raises_coverage_and_persists(tmp_path):
    s = _session(tmp_path)
    before = s.coverage_dict()
    res = s.submit_patch("知识点", PATCH)
    assert res["applied"] is True
    assert res["located_by"] == "heading"
    assert res["percent_after"] > before["percent"]
    assert s.summary()["patch_count"] == 1
    # 落盘给宿主读
    assert "rados" in (tmp_path / "doc.md").read_text(encoding="utf-8")


def test_summary_shape(tmp_path):
    s = _session(tmp_path)
    s.submit_patch(None, "一段")
    s.submit_patch(None, "不同的一段")
    sm = s.summary()
    assert sm["patch_count"] == 2
    assert sm["located_history"] == ["fallback", "fallback"]
    assert isinstance(sm["final_percent"], int)


# ---------------------------------------------------------------- 3. 协议层（慢）


def test_end_to_end_over_mcp_stdio(tmp_path):
    """真的起一个 MCP 子进程，走 JSON-RPC 调完 4 个工具。

    这一条如果挂了，说明协议层有问题（Windows stdio / 编码 / 子进程），
    后面 P2b 的 Agent 循环根本无从谈起。
    """

    async def run():
        async with open_repair_session(
            _extraction(), DOC, tmp_path / ".mcp"
        ) as c:
            names = await c.tool_names()
            cov = await c.call("check_coverage")
            miss = await c.call("list_missing")
            page = await c.call("read_source_page", {"page": 2})
            res = await c.call(
                "submit_patch", {"insert_after": "知识点", "content": PATCH}
            )
            final = await c.final_document()
            return names, cov, miss, page, res, final

    names, cov, miss, page, res, final = asyncio.run(run())

    assert set(names) == {
        "check_coverage",
        "list_missing",
        "read_source_page",
        "submit_patch",
    }
    assert cov["outline"] == ["知识点"]
    # list_missing 返回 {count, missing:[...]}（不返回裸 list——见 mcp_server 注释）
    assert miss["count"] >= 1 and miss["missing"]
    assert {"term", "page"} <= set(miss["missing"][0])
    # 中文备注往返不自乱码
    assert "pgp_num" in page["text"] and "【备注】" in page["text"]
    assert res["applied"] is True
    assert res["percent_after"] > res["percent_before"]
    assert "rados" in final
    # 上下文文件里带的是源课件数据，供人工排查
    ctx = json.loads((tmp_path / ".mcp" / "mcp_session.json").read_text(encoding="utf-8"))
    assert ctx["extraction"]["filename"] == "第1节.pptx"
