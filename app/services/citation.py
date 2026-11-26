"""引用核验：把「有引用」变成「引用可信」。

## 要解决的问题

RAG 的引用最容易造假。模型给出「（源：某课件 第 7 页）」时，你没法一眼看出
那句论断到底是不是第 7 页说的——它完全可能只是"看起来合理"。
普通 RAG 项目停在这里：**我给了引用，信不信由你**。

本模块往前走一步：**用术语级比对去核验每条引用**——
把论断里的术语与被引页的术语表做交集，看这条引用站不站得住。

## 与 P1 的关系：同一套判据，延伸了一个use case

| | P1 覆盖率回检 | P3 引用核验 |
|---|---|---|
| 问的问题 | 生成的内容**漏没漏**源课件的知识点 | 答案的引用**对不对得上**源课件的页 |
| 判据 | 术语级比对（词面） | **同一个**术语级比对 |
| 成本 | 零（纯函数、不调模型） | 零 |

所以 P3 不是新起炉灶，是**把已经标定过的那把尺子用在一个新地方**。
面试时这条线要讲清楚。

## 诚实性约束（与 P1 同级）

判据是**词面**的，不是语义的：论断换了个说法（`cipher suites` → 「加密套件」）
会读出低分，但引用本身没错。所以：

1. 结论一律叫「**引用待核**」，不叫「引用错误」；
2. 必须**附上被引页的原文片段**，把最终判断权交回用户——
   判据负责把范围缩小，判断权不越位；
3. 页面压根不存在（页码明显是编的）才升级为「**未找到该页**」——
   这一条是硬事实，可以断言。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.coverage import extract_en_terms, mine_cn_terms
from app.services.retriever import BM25Index, Chunk, chunks_by_key

# ------------------------------------------------------------------ 判定阈值
#
# ⚠️ 以下阈值是**在两个语料上联合标定**出来的，不是拍的，也不是单语料标的。
#    可复现的标定过程与全部读数见 `scripts/p3_citation_calibrate.py`。
#    样本 = 三组程序化构造的引用（真引用 / 页码错 / 内容编造），每组 120 条。
#
#    | 语料                        | 规模         |
#    |----------------------------|--------------|
#    | Ceph 存储课（14 份 pptx）   | 325 chunk    |
#    | 计算机组成原理（39 份 pdf） | 705 chunk    |
#
# ## 为什么要两个语料：单语料标定会过拟合
#
# 第一版只在 Ceph 上标定，选出「rank ≤ 1」，在 Ceph 上真引用通过 93.3%。
# 换到计组，同一条规则的真引用通过率**掉到 77.5%** —— 因为计组的知识点
# 分散在 39 个按章节切分的小文件里，相邻章节内容重叠，"最像的那一页"
# 竞争激烈得多。而同一个语料上「coverage ≥ 0.80」反而涨到 92.1pp。
# **两个语料各有一个赢家 —— 挑哪个都是过拟合。**
#
# ## 联合标定结果（真引用通过率 / 假引用通过率）
#
# | 规则                  | Ceph       | 计组       |
# |-----------------------|------------|------------|
# | rank ≤ 1              | 93.3% / 0.0% | 77.5% / 0.0% |
# | coverage ≥ 0.80       | 82.5% / 2.5% | 99.2% / 7.1% |
# | **rank≤1 或 cov≥0.80**| **94.2% / 2.5%** | **99.2% / 7.1%** |
#
# → 组合信号在两个语料上都稳（真引用误报 ≤6%，假引用漏报 ≤8%），选它。
#   coverage 仍**不单独使用**：它在 Ceph 上单独用时假引用通过率 16.7%，
#   只在 rank 失效时（术语命中充分的情况）作为补充证据。
#
# ## 与 P1 相反的取舍（刻意的）
#
# P1 覆盖率回检是「宁可漏报，不可冤枉」；这里用 OR 放宽、偏向**少冤枉真引用**。
# 两个场景的代价不对等：P1 的误报会让用户以为文档不完整、对一个免费功能失去信任；
# 这里的误报只是"提示核对 + 附上原文片段"，而漏报是**用户采信了编造的内容**。
# 措辞上仍用「引用待核」而非「引用错误」，把最终判断权交回用户。
RANK_TOLERANCE = 1

#: coverage 的补充判据门槛。标定中它在计组上是主力（99.2% / 7.1%）。
COVERAGE_FALLBACK = 0.80

#: coverage 参与判定所需的最少术语数。
#: **不是"术语少就放行"**——术语不足时 rank 照样参与判定（见 _verdict）。
#: 这里限制的只是 coverage 这个**百分比指标**的可用性：
#: 1 个术语命中 1 个就是 100%，那是噪声，不能拿它当证据。
MIN_TERMS_FOR_COVERAGE = 2

VERDICT_SUPPORTED = "supported"
VERDICT_WEAK = "weak"
VERDICT_NOT_FOUND = "not_found"

VERDICT_LABEL = {
    VERDICT_SUPPORTED: "引用可信",
    VERDICT_WEAK: "引用待核",
    VERDICT_NOT_FOUND: "未找到该页",
}

# ------------------------------------------------------------------ 引用抽取

# 引用标记：`（源：文件名 第 7 页）` / `（源：第 7 页）` / 支持多页 `第 7、8 页`。
#
# ⚠️ 文件名字段的正则**踩过三次**，务必看清这段再动它：
#   ① 写 `[^（）()]`（排除中英文括号）→ 抽不出 `Ceph 与 Kubernetes 集成 (CSI).pptx`
#   ② 改成 `[^（）]`（只排中文括号）  → 又抽不出 `Ceph 对象存储（RGW）配置与使用.pptx`
#      —— 而这两类名字在本项目的课件里占了大半
#   ③ 改成 `.`（任意字符）+ `第 N 页` 右锚点 → 文件名里带括号的终于能抽了，
#      但**跨引用误匹配**：`（源：8.2 指令周期.pdf 第 1 页；源：8.2 指令周期.pdf 第 2 页）`
#      被整体吞成一条，file 段变成 `8.2 指令周期.pdf 第 1 页；源：8.2 指令周期.pdf`，
#      页码取了最后一个 —— 表现为**三条真引用被判「未找到该页」**（2026-10-15 真跑抓到）。
#
# 正解：允许括号（文件名需要），但**排除分号与换行**——它们是引用之间的分隔符。
# 括号必须放行，因为文件名的括号是**成对**的；分号必须拦掉，因为它标志着
# "上一条引用到此结束"。两者不冲突，关键是选对要排除的字符。
#
# 为什么容忍"没有文件名"的写法：模型的输出不会 100% 守格式，
# 而同一次问答通常只涉及一份课件，省略文件名是合理的简写——
# 拒收它只会让"引用一个都抽不出来"，那比抽得宽松更糟。
_CITE = re.compile(
    r"[（(；;]\s*源\s*[：:]\s*"
    r"(?P<file>[^；;\n]{0,80}?)\s*"
    r"第\s*(?P<pages>\d+(?:\s*[、,，]\s*\d+)*)\s*页"
    # 结尾用 lookahead（**不消费字符**）：一条引用的结束可能是 `）`，也可能是
    # 分号/顿号（后面还跟着下一条），还可能是行尾。若写成 `\s*[）)]` 强制要求闭括号，
    # 连写形式里第一条就永远匹配不上——这也是 ③ 那个坑的另一半。
    r"(?=\s*[）)；;、,，]|\s*[（(]|\s*$)"
)
#: 论断的截取边界与最大长度。200 字 ≈ 一到两句，足够判断"这条引用在支撑什么"，
#: 又不至于把上一段的无关内容卷进来拉低命中率。
_CLAIM_MAX_CHARS = 200
_CLAIM_BOUNDARY = re.compile(r"[。\n；;！？]")
_WS = re.compile(r"\s+")


@dataclass
class Citation:
    """答案里的一处引用声明。多页写法会展开成多条。"""

    claim: str  # 该引用所支撑的论断（截取的上下文）
    file: str
    page: int
    raw: str  # 原始标记文本，回显给用户看

    @property
    def key(self) -> str:
        return Chunk.make_key(self.file, self.page)


@dataclass
class CitationCheck:
    """一条引用的核验结果。两个独立信号：

    · `coverage` —— 论断里的术语有多大比例出现在被引页（词面重叠）
    · `rank`     —— 把论断当 query、在被引课件内检索时，**被引页排第几**
                    （1 = 这段文字最像的页面就是它）。BM25 的 IDF 天然压低了
                    "Ceph"/"集群"这类全课通用词的权重，所以它比 coverage 更抗"通用词刷分"。
    """

    citation: Citation
    found: bool  # 被引页是否真的存在于知识库
    terms_total: int = 0
    terms_matched: int = 0
    coverage: float = 0.0
    verdict: str = VERDICT_WEAK
    excerpt: str = ""  # 被引页原文片段（用户自查用）
    matched_terms: list[str] = field(default_factory=list)
    rank: int = 0  # 被引页在该课件内的相关度排名；0 = 未算出
    n_pages_in_file: int = 0  # 该课件页数，用于解读 rank 的绝对意义

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABEL.get(self.verdict, self.verdict)

    def to_dict(self) -> dict:
        return {
            "claim": self.citation.claim,
            "file": self.citation.file,
            "page": self.citation.page,
            "raw": self.citation.raw,
            "found": self.found,
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "coverage": round(self.coverage, 3),
            "terms_total": self.terms_total,
            "terms_matched": self.terms_matched,
            "matched_terms": self.matched_terms[:12],
            "rank": self.rank,
            "n_pages_in_file": self.n_pages_in_file,
            "excerpt": self.excerpt,
        }


def _norm(text: str) -> str:
    return _WS.sub("", text)


def _claim_of(answer: str, pos: int) -> str:
    """取引用标记**之前**的最近一段文字作为论断。

    为什么往前取而不是往后：中文写作里引用标记跟在论断之后（"……（源：第 7 页）"），
    这是唯一的惯例。往后取会把下一句的无关内容卷进来。

    空 claim 要兜底：模型偶尔会把句号写在括号**之前**（"……分布。（源：第 5 页）"），
    这时按句末切分得到的是空串——不兜底就等于"这条引用没有论断可核验"，
    会白白漏掉一条。兜底为整段 head 即可。
    """
    head = answer[max(0, pos - _CLAIM_MAX_CHARS) : pos]
    bounds = list(_CLAIM_BOUNDARY.finditer(head))
    claim = (head[bounds[-1].end() :] if bounds else head).strip()
    return claim or head.strip()


def extract_citations(answer: str, default_file: str = "") -> list[Citation]:
    """抽出答案里所有引用声明。`default_file` 用于补全省略了文件名的写法。"""
    out: list[Citation] = []
    for m in _CITE.finditer(answer):
        file = (m.group("file") or "").strip() or default_file
        claim = _claim_of(answer, m.start())
        raw = m.group(0)
        for page_str in re.findall(r"\d+", m.group("pages")):
            out.append(Citation(claim=claim, file=file, page=int(page_str), raw=raw))
    return out


# ------------------------------------------------------------------ 术语抽取（核验判据）


def course_terms_of(chunks: list[Chunk]) -> dict[str, set[str]]:
    """按份课件挖中文术语表。

    用 `mine_cn_terms`（3–4 字、频次 ≥3）而不是对论断直接挖——
    论断只有一两句话，频次恒为 1，挖不出任何东西。
    所以中文的识别方式只能是**拿源页的术语表去反查论断命中了哪些**。
    """
    by_file: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_file.setdefault(c.file, []).append(c)
    return {
        f: mine_cn_terms(" ".join(c.text for c in cs)) for f, cs in by_file.items()
    }


def _page_terms(chunk: Chunk, course_terms: set[str]) -> set[str]:
    """某一页的术语集合：英文/数字直接抽 + 该课件术语表里出现在本页的。"""
    terms = extract_en_terms(chunk.text)
    flat = _norm(chunk.text).lower()
    terms |= {t for t in course_terms if t in flat}
    return terms


def _claim_terms(claim: str, course_terms: set[str]) -> set[str]:
    """论断里的可识别术语。

    **分母必须用该课件的术语表，不能用被引页的术语表。**
    用被引页的术语表是循环论证：先假定"论断的词都来自被引页"（因为是从页术语表里反查的），
    再拿这个集合去验证"论断的词是否都在被引页"——coverage 恒等于 1，判据完全失效。

    > 这不是推演出来的，是标定实测出来的：第一版用页术语表做分母，
    > 假引用（论断取自甲页、页码改成乙页）有 **48% 被判「引用可信」**。
    > 换成课术语表后，分母才与"被引页"无关。

    英文/数字术语无需频次，直接抽；中文只能从课件术语表反查
    （论断只有一两句话，频次恒为 1，`mine_cn_terms` 挖不出任何东西）。
    """
    flat = _norm(claim).lower()
    terms = extract_en_terms(claim)
    terms |= {t for t in course_terms if t in flat}
    return terms


def _verdict(rank: int, coverage: float, terms_total: int) -> str:
    """rank 或 coverage 任一成立即判「可信」（阈值与依据见文件头标定表）。

    两个信号各管一类失败，这是联合标定选它们组合的原因：
      · `rank ≤ 1`      —— 「这段文字最像的那一页就是它」，管**引用指到了别处**；
      · `coverage ≥ 0.8` —— 论断的术语绝大多数都出现在被引页，管**术语根本对不上**。
    用 OR 而非 AND：两者都能独立证明引用站得住，多留一个信号是为了**少冤枉真引用**。

    **这里刻意没有"术语太少就放行"的分支**——第一版有（从 P1 的
    `MIN_TERMS_FOR_PERCENT` 抄来的思路），被标定回归测试抓出来：
    错引用里有相当一部分恰好是"论断没有可识别术语"的情况，
    默认放行等于给它们盖章，**实测假引用通过率被抬到 32.5%**。
    术语不足只影响 coverage 这个**百分比指标**是否可用（见 MIN_TERMS_FOR_COVERAGE），
    不影响 rank —— rank 用的是论断的全部 token。
    """
    # rank = 0 表示没算出来（理论上不会发生）。这时靠 coverage 兜底，都没有则判待核。
    if 0 < rank <= RANK_TOLERANCE:
        return VERDICT_SUPPORTED
    if terms_total >= MIN_TERMS_FOR_COVERAGE and coverage >= COVERAGE_FALLBACK:
        return VERDICT_SUPPORTED
    return VERDICT_WEAK


class CitationVerifier:
    """对一份知识库（若干 chunk）做引用核验。

    构造时把术语表与索引算好（一次性），之后 `verify` 是纯函数——
    同一份答案 + 同一份知识库，**每次跑出的结论完全一致**（可复现，不调模型）。
    """

    def __init__(
        self,
        chunks: list[Chunk],
        index: BM25Index | None = None,
        course_terms: dict[str, set[str]] | None = None,
    ):
        self.chunks = chunks
        self.by_key = chunks_by_key(chunks)
        # 术语表可由调用方传入（知识库那边已经挖过一遍，见 services/kb.py）——
        # 按份挖 39 份课件要 1~2 秒，不传参就会白挖第二次。
        self.course_terms = course_terms if course_terms is not None else course_terms_of(chunks)
        self._page_terms_cache: dict[str, set[str]] = {}
        # 排名信号要用索引：把论断当 query，看被引页排第几。
        # **复用检索层同一份索引**（不为核验另建一套，避免两处打分逻辑漂移）；
        # 并把已挖好的课件术语表直接喂给它，省掉一次重复挖掘。
        self._index = index or BM25Index(
            chunks, extra_terms=set().union(*self.course_terms.values())
        )
        self._file_pages: dict[str, int] = {}
        for c in chunks:
            self._file_pages[c.file] = self._file_pages.get(c.file, 0) + 1

    def _terms_of(self, chunk: Chunk) -> set[str]:
        if chunk.key not in self._page_terms_cache:
            self._page_terms_cache[chunk.key] = _page_terms(
                chunk, self.course_terms.get(chunk.file, set())
            )
        return self._page_terms_cache[chunk.key]

    def _rank_of(self, claim: str, key: str) -> int:
        """被引页在「这段文字最像哪一页」里的**全库**排名（1 = 最像）。

        **为什么是全库而不是限定在该课件内**（第一版就是这么写的，被单测抓出来）：
        限定范围内排名会让**单页课件**的引用恒等于第 1 名——一份只有一页的课件，
        引用它永远"正确"，判据直接失效。而多页课件里"论断来自别的课件"
        这种错误也跨不出去。改成全库之后：
          · 真引用：被引页就是这段文字的出处，全库也应排第 1；
          · 页码错：真正的来源页排在它前面；
          · 跨课件错引：同样排在它前面。
        三种失败被同一个信号覆盖。代价只是一次全量打分（实测 0.2 ms）。
        """
        for i, hit in enumerate(self._index.search(claim, topk=len(self.chunks)), start=1):
            if hit.chunk.key == key:
                return i
        return len(self.chunks)  # 该页一个词都没匹配上（等价于排最后）

    def verify(self, answer: str, default_file: str = "") -> list[CitationCheck]:
        checks: list[CitationCheck] = []
        for cite in extract_citations(answer, default_file=default_file):
            chunk = self.by_key.get(cite.key)
            if chunk is None:
                # 页码/文件对不上知识库 —— 这是**硬事实**（不是词面判据的灰色地带），
                # 可以断言"未找到该页"。
                checks.append(CitationCheck(citation=cite, found=False, verdict=VERDICT_NOT_FOUND))
                continue

            course_terms = self.course_terms.get(chunk.file, set())
            page_terms = self._terms_of(chunk)
            c_terms = _claim_terms(cite.claim, course_terms)
            matched = {t for t in c_terms if t in page_terms}
            total = len(c_terms)
            # 术语为 0 时 coverage 记 0.0 而不是 1.0：它是辅助展示指标，
            # 报 1.0 会和 rank 判出的「待核」自相矛盾，让用户以为系统算错了。
            cov = len(matched) / total if total else 0.0
            n_pages = self._file_pages.get(chunk.file, 0)
            rank = self._rank_of(cite.claim, chunk.key)
            checks.append(
                CitationCheck(
                    citation=cite,
                    found=True,
                    terms_total=total,
                    terms_matched=len(matched),
                    coverage=cov,
                    verdict=_verdict(rank, cov, total),
                    excerpt=chunk.excerpt(),
                    matched_terms=sorted(matched),
                    rank=rank,
                    n_pages_in_file=n_pages,
                )
            )
        return checks


def summarize(checks: list[CitationCheck]) -> dict:
    """给前端用的一句话汇总。**不做"总体可信度百分比"**——
    把若干条独立判断平均成一个数会掩盖"其中有一条是编的"这件事，
    而那恰恰是用户最需要知道的信息。所以只报计数。"""
    if not checks:
        return {"total": 0, "supported": 0, "weak": 0, "not_found": 0, "note": "本次回答未引用任何页码"}
    counts = {v: sum(1 for c in checks if c.verdict == v) for v in VERDICT_LABEL}
    note = ""
    if counts[VERDICT_NOT_FOUND]:
        note = f"有 {counts[VERDICT_NOT_FOUND]} 条引用指向不存在的页，请谨慎采信"
    elif counts[VERDICT_WEAK]:
        note = f"有 {counts[VERDICT_WEAK]} 条引用的内容在源页里未找到对应术语，建议核对原文"
    else:
        note = "所有引用均能在源页找到对应内容"
    return {"total": len(checks), **counts, "note": note}
