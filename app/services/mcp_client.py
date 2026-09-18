"""MCP client 封装 —— 宿主（FastAPI 进程）这一侧的操作入口。

用法：

    async with open_repair_session(extraction, document, workdir) as client:
        report = await client.call("check_coverage")
        await client.call("submit_patch", {"insert_after": "知识点", "content": "..."})
        final = await client.final_document()

为什么把「会话」而不是「单个工具调用」作为入口：
    每个会话对应一节课件，是一个**独立子进程**，状态（待修文档）活在里面。
    这样 repair 的上下文天然隔离——不会把 A 节的术语算到 B 节头上。
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.models import ExtractionResult

# app/services/mcp_client.py → 项目根（子进程要用它当 cwd 才能 import app.*）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CTX_JSON = "mcp_session.json"
LATEST_DOC = "doc.latest.md"
ENV_CONTEXT = "PPT_MCP_CONTEXT"


@dataclass
class RepairClient:
    """对一次补漏会话的便捷封装。"""

    session: ClientSession
    workdir: Path
    doc_path: Path
    # 会话开始时的文档：server 没落盘时作为兜底返回值（见 final_document）
    initial_document: str = ""

    async def list_tools(self) -> list:
        """原始 MCP Tool 列表（含 name / description / inputSchema）。

        转 Function Calling 的 tools 参数需要 inputSchema，所以这里给对象本身，
        而不是只给名字；tool_names() 是它的便捷包装。
        """
        return (await self.session.list_tools()).tools

    async def tool_names(self) -> list[str]:
        return [t.name for t in await self.list_tools()]

    async def call(self, name: str, arguments: dict | None = None) -> Any:
        """调用工具并把返回的 JSON 文本解析成 Python 对象。

        协议细节：MCP 工具结果统一是 ``content: [TextContent]``。实测 FastMCP
        对 **list 返回值会拆成多个 content 块**（每块一个 JSON 文本），对 dict
        则序列化成一整块。所以这里做两件事：
          ① 多于一块时逐块解析、合并成 list（顺序与工具返回一致）；
          ② 只有一块时按单个对象解析。
        工具侧现在都返回 dict（见 mcp_server 里 list_missing 的注释），
        这里保留兜底是为了换版本 / 换返回值形态时不至于静默丢数据。
        """
        res = await self.session.call_tool(name, arguments or {})
        texts = [
            c.text for c in res.content if getattr(c, "type", None) == "text"
        ]
        if res.isError:
            raise RuntimeError(f"MCP 工具 {name} 返回错误：{texts[0] if texts else ''}")
        if not texts:
            return None
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]

        out: list[Any] = []
        for t in texts:
            try:
                out.append(json.loads(t))
            except json.JSONDecodeError:
                out.append(t)
        return out

    async def final_document(self) -> str:
        """取会话产出的最新文档。

        server 只在**成功应用补丁之后**才写盘，所以这里必须容错：

          · 文件不存在 = 这一轮没有落地任何补丁 → 返回会话开始时的文档
          · 读取出错（Windows 文件锁等）→ 同样退回初始文档

        两种情况抛异常都会把一次「模型判断不需要补」的正常结果变成 error——
        实测踩过（三个 guardrail 测试全挂在这上面）。
        """
        try:
            return self.doc_path.read_text(encoding="utf-8")
        except OSError:
            return self.initial_document


@asynccontextmanager
async def open_repair_session(
    extraction: ExtractionResult, document: str, workdir: Path
) -> AsyncIterator[RepairClient]:
    """起一个 MCP server 子进程，建立会话；退出时自动收掉子进程。"""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    ctx_path = workdir / CTX_JSON
    doc_path = workdir / LATEST_DOC
    ctx_path.write_text(
        json.dumps(
            {
                "extraction": extraction.model_dump(),
                "document": document,
                "doc_out": str(doc_path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, ENV_CONTEXT: str(ctx_path)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield RepairClient(
                session=session,
                workdir=workdir,
                doc_path=doc_path,
                initial_document=document,
            )
