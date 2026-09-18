"""Markdown 补丁插入 —— P2 repair 的「结构性保证」所在（纯函数，无 IO）。

为什么单独一个模块：
    P2 的补漏要求「只补缺失、不改写原文」。如果让模型输出修订后的全文，
    它可能顺手重写别处、引入新缺失——**提示词约束会被违反，程序插入不会**。
    所以模型只输出「插到哪个标题后 + 补什么内容」，由这里的代码负责落地。
    这条是设计稿 §1 决策表里的关键决策，也是最值得在面试里讲的一处。
"""

from __future__ import annotations

import re

# 补漏内容的兜底归宿：定位失败时追加到这个小节（保证模型永远能提交成功）
FALLBACK_HEADING = "知识点补充（自动补漏）"

# 只认二级标题（## ）—— 文档结构由 build_prompt 的模板约定，一级标题是全篇名
_H2 = re.compile(r"^##\s+(.+?)\s*$")


def outline_of(document: str) -> list[str]:
    """列出文档里的二级标题（供模型选插入位置）。"""
    return [m.group(1).strip() for line in document.splitlines() if (m := _H2.match(line))]


def _norm(s: str) -> str:
    """标题比较用：去空白 + 忽略大小写。"""
    return re.sub(r"\s+", "", (s or "")).lower()


def _section_end(lines: list[str], start: int) -> int:
    """从某个二级标题行之后，找到该节的结束行号（下一个 ## 或 # 行，或文件尾）。"""
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].startswith("# "):
            return i
    return len(lines)


def _append_to_fallback(lines: list[str], content: str) -> list[str]:
    """追加/并入兜底小节。已存在则附在它末尾，否则新建。"""
    for i, line in enumerate(lines):
        if (m := _H2.match(line)) and _norm(m.group(1)) == _norm(FALLBACK_HEADING):
            end = _section_end(lines, i)
            block = ["", content.strip(), ""]
            return lines[:end] + block + lines[end:]
    block = ["", f"## {FALLBACK_HEADING}", "", content.strip(), ""]
    return lines + block


def insert_section(
    document: str, insert_after: str | None, content: str
) -> tuple[str, str]:
    """把 content 插到 insert_after 所指小节的末尾。

    返回 ``(新文档, located_by)``，located_by 为 ``"heading"`` 或 ``"fallback"``
    —— 后者表示模型给的标题没找到，内容进了兜底小节。**定位失败不报错**：
    宁可放错位置，也不能让模型卡在"提交不进去"上（它会一直重试，白烧 token）。

    换行风格沿用原文（CRLF 文本不会被改写成 LF）。
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("补丁内容为空")

    crlf = "\r\n" in document
    lines = document.splitlines()
    target = _norm(insert_after or "")

    if target:
        for i, line in enumerate(lines):
            m = _H2.match(line)
            if m and _norm(m.group(1)) == target:
                end = _section_end(lines, i)
                new_lines = lines[:end] + ["", content, ""] + lines[end:]
                return _join(new_lines, crlf), "heading"

    return _join(_append_to_fallback(lines, content), crlf), "fallback"


def _join(lines: list[str], crlf: bool) -> str:
    text = "\n".join(lines)
    # 收束多余空行（连续 3 个以上换行压成 2 个），避免多次插入后文档越补越散
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
    return text.replace("\n", "\r\n") if crlf else text
