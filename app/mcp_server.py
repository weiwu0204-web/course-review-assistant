"""MCP Server：把覆盖率回检能力暴露成工具（P2a 交付物）。

设计要点（详见 docs/P2-REPAIR.md）：
    · **每节一个会话**：宿主为每一节起一个子进程，用环境变量 ``PPT_MCP_CONTEXT``
      指向上下文文件（源课件提取结果 + 待修文档），会话状态只活在进程内。
    · **最小权限**：模型只能通过下面 4 个工具观察和修改一个内存中的文档副本，
      拿不到文件系统、拿不到原始 pptx。
    · **只补不改**：模型不输出修订全文，只提交「插到哪 + 补什么」，
      由 doc_patch.insert_section 落地——结构性保证，不靠提示词自觉。
    · **日志一律走 stderr**：stdout 是 JSON-RPC 通道，往里 print 会直接打断协议。

工具清单：
    check_coverage()                     覆盖率 + 缺失清单 + 文档大纲
    list_missing()                       仅缺失清单（term + 源页页码）
    read_source_page(page)               读源课件某页原文（含备注）
    submit_patch(insert_after, content)  提交补丁并即时回检
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.models import ExtractionResult
from app.services.coverage import CoverageReport, measure
from app.services.doc_patch import insert_section, outline_of

ENV_CONTEXT = "PPT_MCP_CONTEXT"
LATEST_DOC = "doc.latest.md"
MAX_PAGE_CHARS = 3000
MAX_PATCH_CHARS = 2000


def _log(msg: str) -> None:
    """日志走 stderr（stdout 被协议占用）。"""
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


@dataclass
class RepairSession:
    """一节课件的补漏会话（进程内状态）。"""

    extraction: ExtractionResult
    document: str
    doc_out: Path
    last_patch: str = ""
    patch_count: int = 0
    located_history: list[str] = field(default_factory=list)

    # ---------- 读 ----------

    def _report(self) -> CoverageReport:
        return measure(self.extraction, self.document)

    def coverage_dict(self) -> dict:
        rep = self._report()
        return {
            "percent": rep.percent,
            "total_terms": rep.total_terms,
            "missing_terms": rep.missing_terms,
            "reliable": rep.reliable,
            "missing_detail": [
                {"term": t, "page": p} for t, p in rep.missing_detail
            ],
            # 模型看不到文档全文，必须给它章节结构才能指定插入位置
            "outline": outline_of(self.document),
        }

    def missing_list(self) -> dict:
        """返回 dict 而非裸 list：FastMCP 会把 list 返回值拆成多个 content 块，
        每块一个 JSON 文本，客户端解析要额外合并。包一层 dict 让契约稳定。"""
        rep = self._report()
        items = [{"term": t, "page": p} for t, p in rep.missing_detail]
        return {"count": len(items), "missing": items}

    def source_page(self, page: int) -> dict:
        slide = next((s for s in self.extraction.slides if s.page == page), None)
        if slide is None:
            pages = [s.page for s in self.extraction.slides]
            head = ", ".join(str(p) for p in pages[:8])
            more = "…" if len(pages) > 8 else ""
            return {
                "error": f"页码 {page} 不存在；本课件可用页码：{head}{more}"
                f"（共 {len(pages)} 页）"
            }
        text = (slide.text or "").strip()
        if slide.notes:
            text += f"\n\n【备注】{slide.notes.strip()}"
        truncated = len(text) > MAX_PAGE_CHARS
        return {"page": page, "text": text[:MAX_PAGE_CHARS], "truncated": truncated}

    # ---------- 写 ----------

    def submit_patch(self, insert_after: str | None, content: str) -> dict:
        """校验 → 插入 → 回检 → 落盘。校验不过时**返回原因而不是抛错**，
        让模型有机会修正后重交（抛错会让它以为工具坏了，反复重试）。"""
        text = (content or "").strip()
        if not text:
            return {"applied": False, "reason": "补丁内容为空"}
        if len(text) > MAX_PATCH_CHARS:
            return {
                "applied": False,
                "reason": f"内容 {len(text)} 字，超过 {MAX_PATCH_CHARS} 字上限，请精简",
            }
        if text == self.last_patch:
            return {
                "applied": False,
                "reason": "与上一次提交的内容完全相同，请补充新信息或换个切入点",
            }

        before = self._report().percent
        new_doc, located = insert_section(self.document, insert_after, text)
        self.document = new_doc
        self.last_patch = text
        self.patch_count += 1
        self.located_history.append(located)
        after = self._report().percent
        self._persist()
        return {
            "applied": True,
            "located_by": located,
            "percent_before": before,
            "percent_after": after,
            "patch_count": self.patch_count,
        }

    def _persist(self) -> None:
        """把最新文档写盘——宿主在循环结束后从这里取结果。"""
        self.doc_out.parent.mkdir(parents=True, exist_ok=True)
        self.doc_out.write_text(self.document, encoding="utf-8")

    # ---------- 生命周期 ----------

    @classmethod
    def load(cls, ctx_path: Path) -> "RepairSession":
        raw = json.loads(ctx_path.read_text(encoding="utf-8"))
        doc_out = Path(raw.get("doc_out") or (ctx_path.parent / LATEST_DOC))
        return cls(
            extraction=ExtractionResult.model_validate(raw["extraction"]),
            document=raw["document"],
            doc_out=doc_out,
        )

    def summary(self) -> dict:
        """宿主读取的会话小结（落进 RepairRecord）。"""
        return {
            "patch_count": self.patch_count,
            "located_history": list(self.located_history),
            "final_percent": self._report().percent,
        }


def build_server(session: RepairSession) -> FastMCP:
    """把会话包成 MCP server。工具描述会被模型读到，所以写得像给人看的说明。"""
    mcp = FastMCP("ppt-repair")

    @mcp.tool()
    def check_coverage() -> dict:
        """查看当前复习文档对源课件知识点的覆盖率，以及「疑似未覆盖」的术语清单
        （每条带它在源课件中的页码）。同时返回文档的章节大纲 outline——
        提交补丁时用它来指定插入位置，不要凭空编造标题。"""
        return session.coverage_dict()

    @mcp.tool()
    def list_missing() -> dict:
        """列出「疑似未覆盖」的术语及其源课件页码，返回
        {count, missing: [{term, page}, ...]}。比 check_coverage 更省 token，
        只需缺失清单时用它。"""
        return session.missing_list()

    @mcp.tool()
    def read_source_page(page: int) -> dict:
        """读取源课件第 page 页的原文（含讲者备注）。撰写补充内容前应先读它，
        补充内容必须来自这里读到的真实内容。"""
        return session.source_page(page)

    @mcp.tool()
    def submit_patch(insert_after: str | None, content: str) -> dict:
        """提交一段补充内容，追加到 insert_after 指定的二级标题小节末尾。

        insert_after 取自 check_coverage 返回的 outline；写错或省略时内容会进入
        文末的兜底小节。content 应基于 read_source_page 读到的真实课件内容，
        为术语给出实质说明，并标注来源页码；**不要只罗列术语凑覆盖率**。
        返回 {applied, located_by, percent_before, percent_after, patch_count}——
        可用 percent_after 判断这一轮是否有效。"""
        return session.submit_patch(insert_after, content)

    return mcp


def main() -> int:
    ctx = os.environ.get(ENV_CONTEXT)
    if not ctx:
        _log(f"缺少环境变量 {ENV_CONTEXT}，无法启动")
        return 2
    ctx_path = Path(ctx)
    if not ctx_path.exists():
        _log(f"上下文文件不存在：{ctx_path}")
        return 2
    session = RepairSession.load(ctx_path)
    _log(
        f"会话就绪：{session.extraction.filename}，"
        f"初始覆盖率 {session._report().percent}%（{session.extraction.total_pages} 页）"
    )
    build_server(session).run()  # 默认 stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
