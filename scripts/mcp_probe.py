"""MCP stdio 探针 —— P2a 的第一步（见 docs/P2-REPAIR.md §7「已知风险与探针」）。

为什么先写这个：
    P2 的 repair 循环要靠 MCP 的 stdio transport 起子进程，而本项目跑在
    Windows + Git Bash 下（asyncio 子进程在 Windows 用 ProactorEventLoop，
    和 POSIX 行为不同）。**先花十分钟验证能不能跑通，再写正式模块**——
    否则可能闷头写三天才发现协议层不通。

跑法：
    .venv/Scripts/python.exe scripts/mcp_probe.py

通过判据（三条全绿才算过）：
    1. 能列出 server 暴露的工具
    2. 能调用工具并拿到结果
    3. 能带非 ASCII（中文）参数往返，不乱码
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROBE_MARK = "--as-server"


def _run_server() -> None:
    """探针用的最小 MCP server（1 个工具）。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("probe")

    @mcp.tool()
    def echo(text: str) -> str:
        """回显输入文本，用于验证参数往返与编码。"""
        return f"echo:{text}"

    mcp.run()  # 默认 stdio


async def _run_client() -> int:
    """以子进程方式拉起上面的 server，走完整 MCP 协议调用它。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    script = Path(__file__).resolve()
    params = StdioServerParameters(
        command=sys.executable, args=[str(script), PROBE_MARK]
    )

    checks: list[tuple[str, bool, str]] = []
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                checks.append(("1. 列出工具", "echo" in names, f"tools={names}"))

                r = await session.call_tool("echo", {"text": "hello"})
                text = r.content[0].text if r.content else ""
                checks.append(("2. 调用并取结果", text == "echo:hello", repr(text)))

                r2 = await session.call_tool("echo", {"text": "分片校验（中文）"})
                text2 = r2.content[0].text if r2.content else ""
                checks.append(
                    ("3. 中文参数往返", text2 == "echo:分片校验（中文）", repr(text2))
                )
    except Exception as exc:  # noqa: BLE001 —— 探针失败要看清原因
        print(f"[FAIL] stdio 探针异常：{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    print("=== MCP stdio 探针结果 ===")
    ok = True
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
        ok = ok and passed
    print("结论：", "协议层可用，可以继续 P2a" if ok else "协议层有问题，需要调整方案")
    return 0 if ok else 1


def main() -> int:
    if PROBE_MARK in sys.argv:
        _run_server()
        return 0
    return asyncio.run(_run_client())


if __name__ == "__main__":
    raise SystemExit(main())
