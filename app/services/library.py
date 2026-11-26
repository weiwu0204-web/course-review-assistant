"""课程资料库：把「生成过的复习文档」和「问过的问题」按课程归到一起。

## 为什么要按课程归组，而不是按时间混排

这两类记录的**产物性质不同**：

| | 复习文档 | 提问记录 |
|---|---|---|
| 产物 | **文件**（能下载重看） | **对话** |
| 找它时想的是 | "上次那份总册在哪" | "这章我问过什么" |

按时间混排，找文档会被一堆提问淹没，反过来也一样。
而按课程归组之后，一门课 = 它的文档 + 它的问题，**跟人回忆的方式一致**：
先想起"计算机网络"，再想起"那次生成的"或"我问过的"。

## ★ 归组的 key 只能是课程名，不能是文件名

这是本轮最容易做错的地方，实际数据直接证伪了「用文件名集合匹配」这条路：

- 计算机网络那 3 个批量任务的 job **全叫 `讲义.pptx`** —— 文件名既分不出课程、也归不了组
- 计组的问答记录（`4.3 高速缓冲存储器.pdf` 等）**根本没有对应的批量任务**

所以课程身份必须由用户在建库/生成时显式给出。`KnowledgeBase.course_name`
与 `batch.json` 的 `course_name` 是同一个语义，前端让两者共用同一个输入框。

## 提问记录的课程名：四级推导，都不靠猜

1. 记录自身的 `course_name`（新记录都有）
2. **同一 kb_id 的其他记录**——同 kb_id ⇒ 同文件集合 ⇒ 同一门课，这是事实不是推断
3. **文件集合相同的记录**（≥2 份时才用）。这一级是给历史补漏的：
   kb 指纹曾经含 mtime（已修），修复前写的记录挂着旧 kb_id，永远对不上新的。
   单份课件不参与——它们常被统一命名成 `讲义.pptx`，不同课程会撞在一起，
   **归错比不归更糟**（用户会以为课件被弄混了）
4. 都没有 → 归到「未归类」。**不猜**

已有的 2 条历史记录是加这个字段之前写的，所以会短暂落在「未归类」；
用户下次就同一批课件提问时，kb_id 相同，新记录的课程名会经由第 2 级把旧记录带回来。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services import batch_runner as br
from app.services.qa import QA_DIR

#: 未填课程名时的归组键。用不可见字符开头，避免与真实课程名撞车。
UNFILED_KEY = "\x00unfiled"
UNFILED_LABEL = "未归类"

#: 每个知识库最多读回的问答条数（最新的 N 条）。
#: 历史会越来越长，而"这门课问过什么"看最近 200 条足够；
#: 真要全量分析，那是另一个按需的接口。
MAX_QA_PER_KB = 200

_WS = re.compile(r"\s+")


def _norm_key(name: str) -> str:
    """课程名 → 归组键。

    只做「去空白 + 大小写归一」这类不可能改变语义的规范化——
    不做同义词合并（"计组" vs "计算机组成原理"），那需要用户判断，
    程序擅自合并会把两门课搅在一起。
    """
    key = _WS.sub("", (name or "")).casefold()
    return key or UNFILED_KEY


@dataclass
class DocEntry:
    """一次批量生成（一门课的一份复习总册）。"""

    batch_id: str
    status: str
    total: int
    done: int
    error: int
    spent_yuan: float
    updated_at: str

    @property
    def openable(self) -> bool:
        return self.done > 0

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "spent_yuan": self.spent_yuan,
            "updated_at": self.updated_at,
            "openable": self.openable,
        }


@dataclass
class QaEntry:
    """一次提问。"""

    kb_id: str
    ts: str
    question: str
    answer: str
    n_hits: int
    citations: dict
    no_hit: bool
    cost_yuan: float
    elapsed_s: float | None

    @property
    def weak(self) -> int:
        """引用待核/未找到的条数——这是"这条答案有多可信"的直接信号。"""
        return int(self.citations.get("weak", 0)) + int(self.citations.get("not_found", 0))

    def to_dict(self, with_answer: bool = True) -> dict:
        d = {
            "kb_id": self.kb_id,
            "ts": self.ts,
            "question": self.question,
            "n_hits": self.n_hits,
            "citations": self.citations,
            "no_hit": self.no_hit,
            "cost_yuan": self.cost_yuan,
            "elapsed_s": self.elapsed_s,
        }
        if with_answer:
            d["answer"] = self.answer
        return d


@dataclass
class CourseGroup:
    key: str
    name: str
    docs: list[DocEntry] = field(default_factory=list)
    qas: list[QaEntry] = field(default_factory=list)
    #: 该课程涉及的知识库 id（可能多个：用户分批建的库）
    kb_ids: list[str] = field(default_factory=list)

    @property
    def last_active(self) -> str:
        stamps = [d.updated_at for d in self.docs if d.updated_at]
        stamps += [q.ts for q in self.qas if q.ts]
        return max(stamps) if stamps else ""

    def to_dict(self, with_answers: bool = True) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "docs": [d.to_dict() for d in self.docs],
            "qas": [q.to_dict(with_answer=with_answers) for q in self.qas],
            "kb_ids": self.kb_ids,
            "last_active": self.last_active,
            "n_docs": len(self.docs),
            "n_qas": len(self.qas),
        }


# ------------------------------------------------------------------ 采集


def _collect_docs() -> list[tuple[str, DocEntry]]:
    """扫 `output/<batch_id>/batch.json` → (course_name, DocEntry)。"""
    out: list[tuple[str, DocEntry]] = []
    root: Path = br.OUTPUT_ROOT
    if not root.exists():
        return out
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        f = d / br.BATCH_JSON
        if not f.exists():
            continue
        try:
            batch = br.Batch.from_dict(json.loads(br.read_text_retry(f)))
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            continue  # 损坏的历史记录不挡整个列表（与 /batches 同一取舍）
        s = batch.summary()
        out.append(
            (
                batch.course_name,
                DocEntry(
                    batch_id=s["batch_id"],
                    status=s["status"],
                    total=s["total"],
                    done=s["done"],
                    error=s["error"],
                    spent_yuan=s["spent_yuan"],
                    updated_at=s["updated_at"],
                ),
            )
        )
    return out


def _read_qa_file(path: Path, limit: int) -> list[dict]:
    """读一个 jsonl 的**最近** limit 条。坏行跳过，不丢整份。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _files_key(files: list) -> str:
    """文件集合的键（排序后用 \\x00 连起来）。

    用不可见分隔符而不是逗号：文件名本身可能含逗号（`第1章, 概述.pdf`）。
    """
    return "\x00".join(sorted(str(f) for f in files or []))


def _collect_qas() -> list[tuple[str, QaEntry]]:
    """扫 `output/qa/*.jsonl` → (course_name, QaEntry)，课程名按四级推导。"""
    if not QA_DIR.exists():
        return []

    # 第一遍：读全部记录 + 把每个库的课程名定下来
    # （同库必然同课 → 用该库记录里最新的非空值）
    course_of_kb: dict[str, str] = {}
    by_files: dict[str, str] = {}
    raw: list[tuple[str, dict]] = []
    for path in sorted(QA_DIR.glob("*.jsonl")):
        kb_id = path.stem
        records = _read_qa_file(path, MAX_QA_PER_KB)
        named = [r.get("course_name") or "" for r in records]
        named = [n for n in named if n.strip()]
        if named:
            course_of_kb[kb_id] = named[-1].strip()
        for r in records:
            raw.append((kb_id, r))

    # 第二遍：建「文件集合 → 课程名」映射。
    #
    # 为什么需要这一级：kb 指纹曾经含 mtime（已修），导致**同一批课件在修复前后
    # 得到不同的 kb_id**——修复前写的记录就永远对不上新 kb_id，只能进「未归类」。
    # 而它们的 `files` 列表与新的完全一致，所以按文件集合能救回来。
    #
    # ★ 只收 **2 份以上** 的记录：单份课件常被统一命名（真实数据里大量叫
    #   `讲义.pptx`），不同课程会撞在一起，那时按文件集合推断会**归错课**——
    #   归错比不归更糟（用户会以为课件被弄混了）。
    for kb_id, r in raw:
        name = (r.get("course_name") or "").strip()
        files = r.get("files") or []
        if name and len(files) >= 2:
            by_files[_files_key(files)] = name

    out: list[tuple[str, QaEntry]] = []
    for kb_id, r in raw:
        name = (r.get("course_name") or "").strip() or course_of_kb.get(kb_id, "")
        if not name:
            files = r.get("files") or []
            if len(files) >= 2:
                name = by_files.get(_files_key(files), "")
        c = r.get("citations") or {}
        out.append(
            (
                name,
                QaEntry(
                    kb_id=kb_id,
                    ts=str(r.get("ts") or ""),
                    question=str(r.get("question") or ""),
                    answer=str(r.get("answer") or ""),
                    n_hits=len(r.get("hits") or []),
                    citations={k: int(c.get(k, 0)) for k in ("total", "supported", "weak", "not_found")},
                    no_hit=bool(r.get("no_hit")),
                    cost_yuan=float(r.get("cost_yuan") or 0),
                    elapsed_s=r.get("elapsed_s"),
                ),
            )
        )
    return out


# ------------------------------------------------------------------ 归组


def collect(with_answers: bool = True) -> list[dict]:
    """全量归组，返回按「最近活动」倒序的课程列表。

    **答案全文一并返回**（`with_answers`）：一门课几十条提问、单条约 1KB，
    整体几十 KB。为了省这点流量去做分页 + 二次请求，得不偿失。
    规模涨到几百条再改（那时前端也该换成按需加载了）。
    """
    groups: dict[str, CourseGroup] = {}

    def group_of(name: str) -> CourseGroup:
        key = _norm_key(name)
        g = groups.get(key)
        if g is None:
            g = CourseGroup(key=key, name=(name.strip() or UNFILED_LABEL))
            groups[key] = g
        return g

    for name, doc in _collect_docs():
        group_of(name).docs.append(doc)
    for name, qa in _collect_qas():
        g = group_of(name)
        g.qas.append(qa)
        if qa.kb_id not in g.kb_ids:
            g.kb_ids.append(qa.kb_id)

    for g in groups.values():
        # 组内倒序：最近发生的在前
        g.docs.sort(key=lambda d: d.updated_at, reverse=True)
        g.qas.sort(key=lambda q: q.ts, reverse=True)

    # 排序：**「未归类」永远沉底**。
    # 它是"我不知道这属于哪门课"，不是"最新发生的"——
    # 按时间把它顶到第一位，用户每次打开历史第一眼看到的都是一坨不认识的东西。
    # 分两趟利用排序的稳定性：先按最近活动倒序，再把未归类稳定地挪到最后。
    ordered = sorted(groups.values(), key=lambda g: g.last_active, reverse=True)
    ordered.sort(key=lambda g: g.key == UNFILED_KEY)
    return [g.to_dict(with_answers=with_answers) for g in ordered]


def stats() -> dict:
    """给页面顶部一句话用的汇总（不返回明细）。

    `n_courses` **不把「未归类」算作一门课**——它是个待归置的桶，不是课程。
    如实分开报，用户才知道"我还有 2 条记录没归到课上"。
    """
    keys = {_norm_key(n) for n, _ in _collect_docs()} | {_norm_key(n) for n, _ in _collect_qas()}
    return {
        "n_courses": len(keys - {UNFILED_KEY}),
        "n_unfiled": 1 if UNFILED_KEY in keys else 0,
    }


__all__ = [
    "CourseGroup",
    "DocEntry",
    "QaEntry",
    "collect",
    "stats",
]
