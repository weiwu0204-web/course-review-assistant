"""课程知识库：把一批课件建成可检索、可引用核验、可缓存的知识库。

## 为什么要有这一层

P3a 交付的 `retriever` 与 `citation` 是**无状态的纯组件**：每次调用都要重新提取课件
（39 份 PDF 实测 7.3 秒）再重建索引。问答是高频交互，每次 7 秒不可接受，
所以需要一层「**建一次、存下来、反复用**」的管理。

## ★ 知识库建在**课件原文**上，不建在生成的复习文档上

这一点值得写死在代码里（用户曾经误解为"不生成文档知识库就没内容"）：

| | |
|---|---|
| 课件是原始真相，生成的文档是二手转述 | 文档本身就有遗漏（P1/P2 正是在治这个）——建在文档上等于把"生成的遗漏"固化成"课件里没有" |
| 引用必须能指回源课件的页码 | 用户才能拿着页码去核对；引用到生成的文档则无从核对 |
| 文档是"一份输出"，知识库是"一份资产" | 后者应在课件上传时即建好，与"有没有生成过文档"无关 |

所以 `build()` 的输入是**课件文件列表**，与生成流程零耦合。
顺带白赚一个好处：只想问几个问题的课件，不用等生成完文档就能问。

## 缓存策略

产物落在 `output/kb/<kb_id>/`，`kb_id` 由「文件名 + 大小 + **内容哈希**」算出——
**同一批课件重复建库会命中同一个缓存**；内容一变 kb_id 就变，不需要额外的失效逻辑。
（⚠️ 不能把 mtime 算进去：上传副本的 mtime 恒等于上传时刻，见 `_fingerprint`。）
旧目录由 TTL 清理（见 cleanup_stale）。

缓存内容 = chunks + 按份课件的术语表。**不存索引**：重建 BM25 只要 15 ms
（存下来反而增加格式漂移的风险），而真正贵的上游是课件提取。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import OUTPUT_ROOT
from app.services.citation import CitationVerifier, course_terms_of
from app.services.extractor import extract_any
from app.services.retriever import BM25Index, Chunk, build_chunks

KB_ROOT: Path = OUTPUT_ROOT / "kb"

#: 知识库缓存保留时长（小时）。与 uploads 的 TTL 独立——上传副本过期后
#: 知识库还有用（用户可能只是在整理文件），给更长的保留期。
KB_TTL_HOURS = 72

#: 单个知识库的页数上限，防止误把整个磁盘目录传进来。
MAX_PAGES = 5000


@dataclass
class KnowledgeBase:
    """一份可反复问答的课程知识库。"""

    kb_id: str
    files: list[str]  # 课件文件名（不含目录），用于展示与引用兜底
    chunks: list[Chunk]
    index: BM25Index
    verifier: CitationVerifier
    course_terms: set[str]  # 各份课件术语表的并集，供查询改写给出词汇约束
    n_pages: int
    n_chars: int
    #: 课程名（用户建库时填的，可为空）。
    #:
    #: **为什么必须显式记录，而不是从文件名推断**：同一位老师的课件常统一命名为
    #: `讲义.pptx`（本项目实际数据里 3 个批量任务各 4–5 份全叫这个名字），
    #: 靠文件名既分不出课程、也归不了组。而且"历史记录按课程归组"这个功能的
    #: 唯一可靠身份就是用户自己填的那个名字。
    course_name: str = ""
    #: 建库时提取失败的课件（单份失败不中断整批，但要如实告诉用户少了什么）
    failures: list[str] = field(default_factory=list)

    @property
    def n_files(self) -> int:
        return len(self.files)

    def stats(self) -> dict:
        """给前端展示的一句话统计——用户要能确认"我这次建库覆盖了什么"。"""
        return {
            "kb_id": self.kb_id,
            "course_name": self.course_name,
            "n_files": self.n_files,
            "n_pages": self.n_pages,
            "n_chars": self.n_chars,
            "n_chunks": len(self.chunks),
            "n_terms": len(self.course_terms),
        }


def _fingerprint(paths: list[Path]) -> str:
    """kb_id = 「文件名 + 大小 + 内容哈希」的摘要。

    用内容属性而不是随机 id：**同一批文件反复建库必须命中同一个缓存**。

    ## ⚠️ 不能把 mtime 算进指纹

    第一版是 `名字|大小|mtime`，看起来"文件一改 kb_id 就变"很合理——**实际是坏的**：
    问答链路的输入是**上传副本**（`uploads/<upload_id>/xxx.pdf`），副本的 mtime
    恒等于"写入时刻"，即每次上传都是新值。于是同一份课件两次建库得到两个 kb_id
    （实测 `1fcb2694ef42` vs `19ad053014bd`），后果是三重的：

    | 后果 | 表现 |
    |---|---|
    | 缓存永不命中 | 每次建库都重付 7 秒提取（39 份 PDF） |
    | 历史归组失效 | 每条提问记录各挂一个 kb_id，无法互相带出课程名 |
    | 目录堆积 | 每次建库都新建一个 `output/kb/<id>/`，72 小时后才清 |

    改用**内容哈希**：同名同大小但内容不同的文件也能正确区分，
    而且读一遍的成本（21MB 约 50ms）相比提取（7s）可以忽略。
    """
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: x.name):
        try:
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|".encode())
            with p.open("rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError:
            h.update(f"{p.name}|missing".encode())
    return h.hexdigest()[:12]


def _serialize(kb: KnowledgeBase) -> dict:
    return {
        "kb_id": kb.kb_id,
        "course_name": kb.course_name,
        "files": kb.files,
        "n_pages": kb.n_pages,
        "n_chars": kb.n_chars,
        "failures": kb.failures,
        "chunks": [asdict(c) for c in kb.chunks],
        # 术语表按份存（dict），因为它同时服务两件事：
        # ① 检索第 4 层的词表（取并集）② 引用核验按文件反查（要按份）
        "course_terms": {f: sorted(t) for f, t in course_terms_of(kb.chunks).items()},
    }


def _deserialize(data: dict) -> KnowledgeBase:
    chunks = [Chunk(**d) for d in data["chunks"]]
    terms = {f: set(t) for f, t in (data.get("course_terms") or {}).items()}
    union = set().union(*terms.values()) if terms else set()
    index = BM25Index(chunks, extra_terms=union)
    return KnowledgeBase(
        kb_id=data["kb_id"],
        files=list(data["files"]),
        chunks=chunks,
        index=index,
        verifier=CitationVerifier(chunks, index=index, course_terms=terms),
        course_terms=union,
        n_pages=int(data["n_pages"]),
        n_chars=int(data["n_chars"]),
        course_name=str(data.get("course_name") or ""),
        failures=list(data.get("failures") or []),
    )


def load(kb_id: str) -> KnowledgeBase | None:
    """按 id 读缓存；不存在或损坏返回 None（调用方自行决定是否重建）。"""
    path = KB_ROOT / kb_id / "kb.json"
    if not path.exists():
        return None
    try:
        return _deserialize(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 缓存损坏不该让功能挂掉——重建即可
        return None


def build(paths: list[Path], course_name: str = "", use_cache: bool = True) -> KnowledgeBase:
    """从课件文件列表建库（或命中缓存直接返回）。

    抛 `ValueError` 的情况：没有任何可用文件、或全部提取失败。
    单份文件提取失败**不中断**整批——一门课里有 1 份坏文件不该让整门课不能问。

    `course_name` 只是**标签**，不参与 `kb_id` 的计算：同一批课件叫不叫这个名字，
    索引内容完全一样。所以命中缓存时不重建，只在缓存里没名字而这次给了的时候补写。
    """
    if not paths:
        raise ValueError("没有可用的课件文件")

    course_name = (course_name or "").strip()
    paths = sorted(paths, key=lambda p: p.name)
    kb_id = _fingerprint(paths)
    if use_cache:
        cached = load(kb_id)
        if cached is not None:
            if course_name and cached.course_name != course_name:
                # 补写：用户第一次可能没填名字，后来填了。只改标签，不动索引。
                cached.course_name = course_name
                _write_cache(cached)
            return cached

    results = []
    failures: list[str] = []
    for p in paths:
        try:
            results.append(extract_any(p))
        except Exception as exc:  # noqa: BLE001 单份失败不中断
            failures.append(f"{p.name}：{exc}")

    chunks = build_chunks(results)
    if not chunks:
        detail = ("；".join(failures[:3])) if failures else "所有课件都没有可提取的文字"
        raise ValueError(f"没有提取到任何可检索的内容（{detail}）")
    if len(chunks) > MAX_PAGES:
        raise ValueError(
            f"这批课件共 {len(chunks)} 页，超过单库上限 {MAX_PAGES} 页，请分批建库"
        )

    terms = course_terms_of(chunks)
    union = set().union(*terms.values()) if terms else set()
    index = BM25Index(chunks, extra_terms=union)

    kb = KnowledgeBase(
        kb_id=kb_id,
        files=[r.filename for r in results],
        chunks=chunks,
        index=index,
        verifier=CitationVerifier(chunks, index=index, course_terms=terms),
        course_terms=union,
        n_pages=len(chunks),
        n_chars=sum(len(c.text) for c in chunks),
        course_name=course_name,
        failures=failures,
    )
    _write_cache(kb)
    return kb


def _write_cache(kb: KnowledgeBase) -> None:
    """落盘缓存。**失败不抛**——内存里的 kb 是好的，只是下次要重建。"""
    try:
        dest = KB_ROOT / kb.kb_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kb.json").write_text(
            json.dumps(_serialize(kb), ensure_ascii=False), encoding="utf-8"
        )
        if kb.failures:
            (dest / "failures.json").write_text(
                json.dumps(kb.failures, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except OSError:
        pass


def cleanup_stale(ttl_hours: int = KB_TTL_HOURS) -> int:
    """清掉超期的知识库缓存，返回删除目录数。

    与 `routes._cleanup_stale_uploads` 同一套思路：**只删 mtime 超期的**，
    刚建好的目录 mtime 是当前时间，绝不可能被误删。
    """
    if not KB_ROOT.exists():
        return 0
    now = time.time()
    removed = 0
    for entry in KB_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            if now - entry.stat().st_mtime <= ttl_hours * 3600:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:  # noqa: BLE001 被占用等瞬时错误，跳过而不误删
            continue
    return removed


def file_stats(files: list[str]) -> Counter:
    """按扩展名统计（给建库结果展示用）。"""
    return Counter(Path(f).suffix.lower() for f in files)
