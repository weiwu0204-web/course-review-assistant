"""课程知识库的检索层：分块 → 倒排索引 → BM25 检索。

## 为什么是 BM25 而不是向量检索（实测结论，不是偏好）

P3 设计时按惯例准备上向量库，先做了探针（`scripts/p3_probe.py`，14 份真实 Ceph 课件）：

| 指标 | 实测 |
|---|---|
| 规模 | 325 个 chunk / 7.8 万字符 |
| 暴力全量打分延迟 | **p50 0.20 ms** |
| 术语 query 召回@5 | **98.8%**（英文 98.5 ／ 数字 100 ／ 纯中文 100） |
| 口语 query 召回@5 | 92.9% → **查询改写后 100%** |

结论：这个规模下 ANN 索引要解决的问题（百万级文档的检索速度）**根本不存在**，
差三四个数量级；而 embedding 也**没有提升空间**——已经到顶了。
所以零依赖的 BM25 是正确选择，本项目**不引入任何新依赖**。见 `docs/P3-RAG.md` §1。

## 分块粒度：一页一块

引用溯源的最小单位是**页码**，所以 chunk 必须与页对齐。
按固定字数硬切会让一页跨两个 chunk，引用时只能给"第 N 页附近"这种模糊说法，
而用户没法拿着它去核对。实测 chunk 长度 p50 186 / max 690 字符，
远低于任何模型的上下文压力，**没有为了均匀而切分的必要**。

## 分词：四层 token

1. **英文/技术术语** `CRUSH` / `pg_num` / `rados`
2. **数字 + 单位** `3副本` / `64KB` / `0.85` ← **必须有**
3. **中文 2-gram** 兜底，保证任何中文串都能被检索
4. **课件术语整词**（`mine_cn_terms` 挖的 3–4 字频次串）—— 默认开启

第 2 层的来由：第一版只做 1+3，中文 query 召回只有 **52.5%**。
拆开看才发现，"非英文"query 里大半是 `3副本` / `0.85` / `64KB` 这类数字术语——
英文正则要求字母开头、中文正则要求汉字，**两边都不匹配 → token 为空 → 必然检索失败**。
而数字阈值恰恰是课件里最高频的考点（副本数、容量阈值、超时秒数）。
补上后中文类 52.5% → 100%，整体 94.5% → 98.8%。

### ★ 第 4 层：在第一个语料上被砍掉、在第二个语料上被证明有用

| 语料 | 1+2+3 层 | 加第 4 层 | 纯中文类 |
|---|---|---|---|
| Ceph 存储（14 份 / 325 chunk） | 98.8% | 98.8% | 100% → 100% |
| **计算机组成原理（39 份 / 705 chunk）** | **97.3%** | **99.7%** | **87.5% → 100%** |

**只看第一个语料，结论是"这一层零收益，砍掉"**——我当时确实砍了，
还把"砍掉"写进了文档当决策依据。换到计组才发现：纯中文术语（`寻址方式` /
`指令周期` / `微操作命令`）在 2-gram 下被切成"寻址/址方/方式"，IDF 被稀释，
必须靠整词才能精确命中。计组的纯中文类从 **87.5% → 100%**。

成本可以忽略：索引 13→15 ms（一次性）、查询 p50 0.16→0.17 ms、p95 0.22→0.41 ms。
**用亚毫秒的延迟换 2.4 个百分点，不值得犹豫。**

> **教训**：`先量再改` 里的"量"必须是**多个语料**。
> 只在一个课程上标定得出的结论，很可能只是那门课的词汇分布特征。
> 这条和 P1 的"平均覆盖率掩盖了后段衰减"是同一类错误——**样本代表性不足**。

不引 jieba 的理由与 `coverage.py` 一致：词典 ~10MB，与"本地小工具"定位不符。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from app.models import ExtractionResult
from app.services.coverage import NUM_UNIT, mine_cn_terms

# ------------------------------------------------------------------ 分词

_TOKEN_EN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_TOKEN_CN = re.compile(r"[\u4e00-\u9fff]+")
_WS = re.compile(r"\s+")

#: 单个 chunk 超过这个长度就不再作为引用片段全文展示（前端只给前 N 字 + 省略号）。
#: 取值依据：实测 chunk p90 = 532 字符，取 600 覆盖绝大多数整页展示需求。
EXCERPT_CHARS = 600


def tokenize(text: str, terms: set[str] | None = None) -> list[str]:
    """四层分词见模块 docstring。

    `terms` = 第 4 层（课件术语整词）的词表。传 None 就退化成前三层——
    **不建议**：计组语料实测纯中文类会从 100% 掉到 87.5%（见模块 docstring）。
    保留这个开关只是为了让评估脚本能对照出这一层的贡献。

    中文 2-gram 与数字单位都**不做去重**——BM25 的 tf 需要真实词频，
    去重会让"同一页反复强调某术语"这个信号消失。
    """
    tokens = [t.lower() for t in _TOKEN_EN.findall(text)]
    tokens += [_WS.sub("", m).lower() for m in NUM_UNIT.findall(text)]
    for run in _TOKEN_CN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    if terms:
        flat = _WS.sub("", text).lower()
        tokens.extend(t for t in terms if t in flat)
    return tokens


def build_course_terms(chunks: list["Chunk"]) -> set[str]:
    """按份课件挖中文术语（3–4 字、频次 ≥3），取并集作为第 4 层的词表。

    为什么按"份"挖而不是全库挖：同一串词在不同课程里含义不同，
    按份挖得到的是**本课程的专有名词**，更准（与 `coverage.py` 的口径一致）。

    耗时：39 份课件 / 14.5 万字符约 1~2 秒，**只在建索引时跑一次**，
    相比上游的课件提取（同规模实测 7.3 秒）可以忽略。
    """
    by_file: dict[str, list[str]] = {}
    for c in chunks:
        by_file.setdefault(c.file, []).append(c.text)
    terms: set[str] = set()
    for texts in by_file.values():
        terms |= mine_cn_terms(" ".join(texts))
    return terms


# ------------------------------------------------------------------ 分块


@dataclass(frozen=True)
class Chunk:
    """一个可检索单元 = 课件的一页。

    `key` 是**稳定标识**（`文件名::页码`），而不是数组下标——
    索引可以存盘、可以重建，用下标做标识时一旦顺序变了引用就全部错位。
    引用溯源这种"差一格就是错"的场景，标识必须是内容决定的。
    """

    key: str
    file: str
    page: int
    title: str
    text: str

    @staticmethod
    def make_key(file: str, page: int) -> str:
        return f"{file}::{page}"

    def excerpt(self, limit: int = EXCERPT_CHARS) -> str:
        """供前端展示的原文片段（引用待核时用户要拿它自查）。"""
        if len(self.text) <= limit:
            return self.text
        return self.text[:limit] + "…"


def _guess_title(text: str) -> str:
    """取首行当标题（长于 40 字说明不是标题，返回空）。

    课件首页常有大标题，但这个信息**不参与检索**——它只用于前端展示
    "这条命中的是哪一页"。所以判断失准的代价很低，不需要复杂启发式。
    """
    first = text.strip().split("\n", 1)[0].strip()
    return first if 0 < len(first) <= 40 else ""


def build_chunks(results: list[ExtractionResult]) -> list[Chunk]:
    """把若干份课件的提取结果切成一页一块。

    空页（无文字、无备注）直接跳过——它们在检索里没有贡献，
    留着只会让"检索到第 N 页"这种噪声结果变多。
    """
    chunks: list[Chunk] = []
    for result in results:
        for slide in result.slides:
            text = (slide.text + "\n" + slide.notes).strip()
            if not text:
                continue
            chunks.append(
                Chunk(
                    key=Chunk.make_key(result.filename, slide.page),
                    file=result.filename,
                    page=slide.page,
                    title=_guess_title(slide.text),
                    text=text,
                )
            )
    return chunks


# ------------------------------------------------------------------ 检索


@dataclass
class Hit:
    """一条检索结果。"""

    chunk: Chunk
    score: float
    rank: int  # 从 1 开始


class BM25Index:
    """BM25 倒排索引（内存态，纯 Python，零依赖）。

    构建耗时实测 11 ms / 325 chunk，所以**不需要把索引本身存盘**——
    存 chunk 就够了，索引每次重建的成本可以忽略。
    """

    def __init__(
        self,
        chunks: list[Chunk],
        k1: float = 1.5,
        b: float = 0.75,
        extra_terms: set[str] | None = None,
    ):
        if not chunks:
            raise ValueError("BM25Index 需要至少一个 chunk")
        self.chunks = chunks
        self.k1, self.b = k1, b
        # 第 4 层词表（课件术语整词）。不传就现挖——按份课件挖，耗时见
        # build_course_terms 的说明（39 份约 1~2 秒，只在建索引时跑一次）。
        self.extra_terms = (
            extra_terms if extra_terms is not None else build_course_terms(chunks)
        )

        docs_tokens = [tokenize(c.text, self.extra_terms) for c in chunks]
        self.n_docs = len(docs_tokens)
        self.doc_len = [len(t) for t in docs_tokens]
        self.avgdl = sum(self.doc_len) / self.n_docs
        self.tf = [Counter(t) for t in docs_tokens]

        df: Counter[str] = Counter()
        for toks in docs_tokens:
            for w in set(toks):
                df[w] += 1
        # BM25 的概率型 IDF（带 0.5 平滑）：词出现在越多文档里，区分度越低。
        # 用这个形式而不是 log(N/df)，是为了让出现在所有文档里的词也不为负分。
        self.idf = {
            w: math.log(1 + (self.n_docs - d + 0.5) / (d + 0.5)) for w, d in df.items()
        }

    def search(self, query: str, topk: int = 5, scope: str | None = None) -> list[Hit]:
        """检索。`scope` = 限定到某一份课件（界面上就是"只在这一节里找"）。

        打分是**全量遍历**（无倒排剪枝、无 ANN）——实测 0.20 ms，
        为它做优化是纯粹的浪费，反而会引入"结果可能不全"的不确定性。
        """
        scores: dict[int, float] = defaultdict(float)
        for w in tokenize(query, self.extra_terms):
            idf = self.idf.get(w)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(w)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * f * (self.k1 + 1) / denom
            # 注意：scope 过滤放在排序之后（见下），不在这里 continue——
            # 提前 continue 会让"限定范围"变成一个必须重算全表的操作，
            # 而现在这样写，同一份索引可以服务任意范围，无需重建。

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if scope:
            ranked = [(i, s) for i, s in ranked if self.chunks[i].file == scope]
        return [
            Hit(chunk=self.chunks[i], score=round(s, 4), rank=r)
            for r, (i, s) in enumerate(ranked[:topk], start=1)
        ]

    # -------------------------------------------------------------- 落盘
    #
    # 只存 chunk，不存索引：重建 11 ms，而 chunk 的**上游成本**（提取课件、
    # 可能还要导出图片）是秒级的。所以缓存的价值全在 chunk 这一层。

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps([asdict(c) for c in self.chunks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def load(path: str | Path) -> "BM25Index":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return BM25Index([Chunk(**d) for d in data])


def chunks_by_key(chunks: list[Chunk]) -> dict[str, Chunk]:
    """`key → Chunk` 映射，供引用核验按"文件名+页码"反查同一页。"""
    return {c.key: c for c in chunks}
