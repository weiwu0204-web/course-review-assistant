"""汇编式 reducer：把 N 份课节文档拼成「整门课复习总册」。

设计约束（见 docs/P0-ARCHITECTURE.md §6）：**不调用 LLM 重写正文**。
- 改写 N 份正文有信息丢失风险，还要再花一份 token 钱，且不可复现；
- 复习场景冗余无害——第二章 PPT 复习第一章内容，重复出现反而利于复习；
- reducer 只做：自动目录（读各份 doc.md 的 H1/H2）+ 出处锚点（来源文件/页数）
  + 截断警示 + 正文原样汇编，纯字符串操作，成本为零、完全可复现。
"""
from __future__ import annotations

import re
import time

# 只取 H1/H2 做目录：H3 往下是知识点内部结构，进目录反而淹没主干
_HEADING_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*#*\s*$")


def extract_headings(md: str) -> list[str]:
    """取 markdown 的 H1/H2 行文本，供目录与 job 元数据使用。

    跳过代码块内的 `#` 行——那很可能是 shell/python 注释，不是文档结构。
    """
    headings: list[str] = []
    in_code = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            headings.append(m.group(2).strip())
    return headings


def build_master_doc(course_name: str, sections: list[dict]) -> str:
    """把各节内容汇编成总册 markdown。

    sections 每项：{idx, title, source, pages, warning, body}
      - idx：章节序号（= 用户拖入顺序）
      - title：该份 doc 的第一个 H1（调用方兜底为文件名）
      - source / pages：出处锚点，保证任何一段都能回溯到哪份课件
      - warning：如 "truncated"，目录与节首都要提示
      - body：该份 doc.md 全文，原样保留，不改写不删减
    """
    lines: list[str] = [
        f"# 《{course_name}》期末复习总册",
        "",
        f"> 由 {len(sections)} 份课件合成 · 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        " · 按课件顺序编排",
        "",
        "## 目录",
        "",
    ]
    for s in sections:
        warn = "（输出被截断，内容可能不完整）" if s.get("warning") else ""
        lines.append(f"{s['idx']}. {s['title']}{warn}")

    for s in sections:
        lines += ["", "---", "", f"## 第 {s['idx']} 节：{s['title']}", ""]
        lines.append(f"> 来源：{s['source']}（共 {s.get('pages', 0)} 页）")
        if s.get("warning"):
            lines.append(
                "> ⚠️ 本节生成时输出被截断，尾部内容（通常是题目章节）可能缺失，可对单份重试。"
            )
        lines.append("")
        lines.append((s.get("body") or "").strip())

    lines.append("")
    return "\n".join(lines)
