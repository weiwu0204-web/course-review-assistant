"""覆盖率回检：把源课件的知识点术语与生成的成品比对，量化「疑似漏了什么」。

## 为什么要有这个模块

M3 实测（docs/M3-VALIDATION.md §2.6）证明：通用大模型一次处理整门课时，覆盖率会
**静默衰减**（前半程 91.2% → 后半程 74.8%），文档看着完整、不报错，用户无从察觉。
本项目每份课件独立请求，覆盖不衰减（98.2% → 98.5%）——但光"不衰减"没用，
**得让用户看见这个数字**，才构成可验证的差异。这就是覆盖率可见化。

## 判据是实测选出来的，不是拍脑袋（2026-09-13，Ceph 课程 14 节真实数据）

| 判据 | 好文档读数 | 坏文档读数 | 结论 |
|---|---|---|---|
| 整行字面包含 | 29.6% | 6.8% | 太严，好文档只有 29% → 不可展示 |
| 行内 6-gram 复用率 | 61.6% | 24.7% | 语义是「有没有原样抄措辞」，且**看不出前后衰减** |
| 英文术语命中 | 97.5% | 90.2% | 语义对，但只覆盖英文 |
| **本文档实现（中英术语）** | **98.4%** | **83.0%** | ✅ 选定 |

关键点：好文档必须读作 95%+ 才可展示（否则用户以为漏了一半），同时又要有区分度。

## 术语的三条来源（全部零成本、零模型调用）

1. 英文/技术术语（正则）：`CRUSH` / `pg_num` / `rados bench`
2. 数字 + 单位/阈值：`0.85` / `64KB` / `3 副本`
3. 中文术语：**单份课件内出现 ≥3 次的纯中文 3–4 字串**（排除停用字）。
   实测挖出：分布式存储 / 单点故障 / 纠删码 / 横向扩展 / 存储池。

不引入分词库（jieba 词典 ~10MB）的理由：课件里反复出现的短中文串 ≈ 该课件的专有名词，
这个假设对课件文本成立，且候选量小（实测 7–64 个/份）。

## 诚实性约束

判据是**词面**的，不是语义的：术语缺失也可能是被换了说法（`cipher suites` → 「加密套件」）。
所以对外一律称「**疑似未覆盖**」，并附源课件页码供用户自行确认——
判据负责把范围缩小，判断权交回用户。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.models import ExtractionResult

# ------------------------------------------------------------------ 术语来源

# 英文/技术术语：CRUSH、pg_num、rados bench 里的词、k8s、apiVersion...
_EN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}")
# 数字 + 单位/阈值：考试最爱考、且几乎不会被改写。
# 除「数字+单位」外，也收**孤立小数**（0.85 / 1.5 这类几乎都是参数或阈值）；
# 但**不收孤立整数**——那会把页码、序号也当成知识点。
_NUM_UNIT = re.compile(
    r"\d+\.\d+"  # 孤立小数（参数/阈值）
    r"|\d+(?:\.\d+)?\s*(?:%|KB|MB|GB|TB|PB|ms|us|ns|GiB|MiB|TiB|"
    r"字节|副本|小时|分钟|秒|倍|个|块|页|节点|核|GB/s|MB/s)"
)
# 纯中文片段（用于频次挖掘）
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 英文停用词：**课件里当标签/版式用的词，不是知识点**。
# 来源：2026-09-13 把 M3 那门真实课程（14 节）的「我方总册缺失术语」全量导出后，
# 归纳出的系统性误报——缺的都是 "Key Points"/"Best Practices"/"Cause & Resolution"
# 这类幻灯片装饰标签，以及页码/章节序号（i、ii、iii）。
# 判据本身没问题，是这些词不该进术语表；不放行会让好文档被冤枉成 93%。
#
# ⚠️ 刻意**不**收 node / monitoring / optimization / architecture / process / threshold /
#    domain / safety 这类**可能是真概念**的词——它们在某些课里就是知识点。
#    取舍依据：**宁可少报，不可冤枉**（频繁误报会让用户不再信任这个功能；
#    少量漏报只是一份辅助参考的保守偏差）。换课程发现新的标签词，往这里加即可。
_EN_STOP = frozenset(
    """
    page session section series course module concept concepts stage stages step steps
    checklist summary overview agenda appendix example examples note notes tip tips
    key points point best practice practices cause causes issue issues rule rules
    action actions command commands visualization observability scenario watch live
    alerting maintenance frames dimension blocks terminal cli resolution
    ii iii iv vi vii viii ix xi xii xiii xiv xv
    """.split()
)

# 中文术语的构成约束：长度 3–4 字、在一份课件内出现 ≥3 次
CN_TERM_MIN = 3
CN_TERM_MAX = 4
CN_TERM_MIN_FREQ = 3

# 停用字：含这些字的 n-gram 绝大多数是虚词组合（"进行/可以/因此/如下"），不是术语。
# 宁可漏掉少量真术语，也不要把虚词放进来——虚词的"覆盖率"没有意义还会稀释信号。
_STOP_CHARS = frozenset(
    "的了和是在与及或也就都要会对从把被这那有为以不我你他其之而于则若如若因此所以"
    "可以我们他们进行一个一些什么怎么是否以及并且但是同时另外例如比如其中通过使用"
    "需要能够可能应该注意如下以下上述该等"
)

# 归一化：比对前去掉所有空白（PPT 文本层常有多余空格/换行）
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub("", text)


def extract_en_terms(text: str) -> set[str]:
    """英文/技术术语 + 数字单位。

    长度 ≥2 的英文词（避免 a/of 这类噪声），并剔除课件版式标签词（_EN_STOP）。
    """
    terms = {
        t.strip(".").lower()
        for t in _EN_TERM.findall(text)
        if len(t.strip(".")) >= 2 and t.strip(".").lower() not in _EN_STOP
    }
    terms |= {_WS.sub("", m).lower() for m in _NUM_UNIT.findall(text)}
    return terms


def mine_cn_terms(text: str, min_freq: int = CN_TERM_MIN_FREQ) -> set[str]:
    """频次挖掘中文术语：课件内出现 ≥min_freq 次的纯中文 3–4 字串。

    再做一次「子串剪枝」：若某候选是另一个更高频（或同频）候选的子串，则丢弃它，
    避免 文件系统 / 文件系 / 件系统 这类重叠变体把同一个知识点算成三遍。
    """
    s = _norm(text)
    counts: Counter[str] = Counter()
    for n in range(CN_TERM_MIN, CN_TERM_MAX + 1):
        for i in range(len(s) - n + 1):
            g = s[i : i + n]
            if _CJK_RUN.fullmatch(g) and not (set(g) & _STOP_CHARS):
                counts[g] += 1

    kept: set[str] = set()
    # 长串优先：短串若是长串的子串且频次不更高，则视为重叠变体
    for term, freq in sorted(counts.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        if freq < min_freq:
            continue
        if any(term in k and counts[k] >= freq for k in kept):
            continue
        kept.add(term)
    return kept


# ------------------------------------------------------------------ 数据结构


@dataclass
class PageCoverage:
    """单页的覆盖情况。"""

    page: int
    total: int
    missing: list[str]

    @property
    def covered(self) -> int:
        return self.total - len(self.missing)

    @property
    def rate(self) -> float:
        return self.covered / self.total if self.total else 1.0


# 术语太少时百分比没有意义：只有 2 个术语的文件，缺 1 个就是 50%，纯噪声。
# 实测参考：一门课的 25 页课件约 100–280 个术语；6 页小样件只有 12 个。
MIN_TERMS_FOR_PERCENT = 8

# 覆盖率**只对「复习指南 + 详细」这一组参数有意义**——只有这一组承诺
# 「全面覆盖课件的知识点」。其它档位本就不追求全覆盖：
#   · 习题生成：只出题，不搬运知识点
#   · 重点总结：只留要点
#   · 精简：提示词里有字数上限
# 实测证据（2026-09-14）：把用户存量批次全跑一遍
#   · Ceph 课（复习指南/详细）14 节 → 95–100%，与人工核对一致
#   · 计算机网络（习题生成、重点总结）8 节 → 58–61%，被判「疑似遗漏较多」
# 后者是**用不成立的承诺去指责文档**，会给出吓人且错误的结论，故直接不算。
COVERAGE_TEMPLATES = {("复习指南", "详细")}


def applies_to(template: str, style: str) -> bool:
    """这组参数下覆盖率是否是有意义的指标（见 COVERAGE_TEMPLATES 的说明）。

    空值按**默认档位**理解（复习指南 / 详细）——调用方没传不等于「不适用」。
    """
    return (template or "复习指南", style or "详细") in COVERAGE_TEMPLATES


def not_applicable_note(template: str, style: str) -> str:
    """不适用时给用户的一句解释——不能只沉默，否则用户以为功能坏了。"""
    return (
        f"当前是「{template or '复习指南'} / {style or '详细'}」档，本就不追求覆盖课件的"
        "全部知识点，因此不做覆盖率回检。想看覆盖率请切到「复习指南 / 详细」重新生成。"
    )


@dataclass
class CoverageReport:
    """一份课件的覆盖率报告。"""

    filename: str = ""
    total_terms: int = 0
    missing_terms: int = 0
    pages: list[PageCoverage] = field(default_factory=list)
    # 术语 → 首次出现的页码，供「疑似未覆盖」清单标出处
    missing_detail: list[tuple[str, int]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        """覆盖率：命中术语 / 全部术语（0–1）。无术语时视为 1.0（不误报）。"""
        if not self.total_terms:
            return 1.0
        return (self.total_terms - self.missing_terms) / self.total_terms

    @property
    def percent(self) -> int:
        return round(self.rate * 100)

    @property
    def reliable(self) -> bool:
        """术语数是否够多到可以给百分比。否则前端应显示「样本不足」而不是一个数字。"""
        return self.total_terms >= MIN_TERMS_FOR_PERCENT

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "total_terms": self.total_terms,
            "missing_terms": self.missing_terms,
            "percent": self.percent,
            "reliable": self.reliable,
            "missing_detail": [{"term": t, "page": p} for t, p in self.missing_detail],
            "pages": [
                {"page": p.page, "total": p.total, "missing": p.missing} for p in self.pages
            ],
        }


# ------------------------------------------------------------------ 主入口


def measure(result: ExtractionResult, document: str) -> CoverageReport:
    """比对一份课件的提取结果与生成的成品，产出覆盖率报告。

    document 为生成的 Markdown 全文（单份模式传该份文档，批量模式传该节文档——
    不是总册，否则其它节的内容会把本节的缺漏掩盖掉）。
    """
    doc = _norm(document).lower()
    cn_terms = mine_cn_terms(
        " ".join(s.text + " " + s.notes for s in result.slides)
    )

    pages: list[PageCoverage] = []
    seen: set[str] = set()
    missing_detail: list[tuple[str, int]] = []
    total = 0
    missing = 0

    for slide in result.slides:
        page_text = slide.text + " " + slide.notes
        terms = extract_en_terms(page_text)
        terms |= {t for t in cn_terms if t in _norm(page_text)}
        terms -= seen  # 同一术语跨页重复出现只算一次（各页术语表不重复计分）
        if not terms:
            continue

        page_missing = []
        for t in sorted(terms):
            total += 1
            if t not in doc:
                missing += 1
                page_missing.append(t)
                missing_detail.append((t, slide.page))
        seen |= terms
        pages.append(PageCoverage(page=slide.page, total=len(terms), missing=page_missing))

    return CoverageReport(
        filename=result.filename,
        total_terms=total,
        missing_terms=missing,
        pages=pages,
        missing_detail=missing_detail[:50],  # 清单只保留前 50 条，避免刷屏
    )


def verdict(percent: int) -> str:
    """把覆盖率翻译成一句人话（配合前端配色）。

    阈值**不是拍的**，是按已知质量的参照文档标定的（2026-09-13）：
      · 本项目自己的总册（人工核对过覆盖良好）逐节读数 95–100% → 应判「良好」
      · 通用 agent 那份（后 1/3 确认有静默遗漏）后半程读数 77–90% → 应判「需核对」
    取 94 / 86 两个分界，正好把这两类分开（实测区间无重叠）。
    """
    if percent >= 94:
        return "覆盖良好"
    if percent >= 86:
        return "有少量疑似遗漏"
    return "疑似遗漏较多，建议核对"


_DISCLAIMER = (
    "> 术语级自动比对（离线、免费）：把源课件里的知识点术语与本文档比对，"
    "本文档中**未出现**的列为「疑似未覆盖」。\n"
    "> 判据是**词面**而非语义——「疑似」不等于一定漏，可能只是换了个说法"
    "（如 `cipher suites` 写成了「加密套件」）。请对照页码自行确认。"
)


def render_report_section(rows: list[dict], title: str = "附：覆盖率自查") -> str:
    """把若干份的覆盖率渲染成可附在文档末尾的 Markdown 一节。

    rows 每项：{name, percent, reliable, missing: [(term, page), ...]}
    reliable=False（术语太少）时不给百分比——2 个术语缺 1 个就是 50%，那是噪声。

    **默认不附**（用户选择「可开关、默认关」）：总册是拿来复习和分享的，
    塞一段自查报告会让同学以为这份文档不完整。需要时自己勾选。
    """
    if not rows:
        return ""
    lines = ["", "---", "", f"## {title}", "", _DISCLAIMER, ""]

    lines += ["| 节 | 覆盖率 | 疑似未覆盖 |", "|---|---|---|"]
    for r in rows:
        pct = f"{r['percent']}%" if r.get("reliable", True) else "术语过少，不计算"
        n = len(r.get("missing") or [])
        lines.append(f"| {r['name']} | {pct} | {n} 项 |")

    scored = [r for r in rows if r.get("reliable", True)]
    if scored:
        avg = sum(r["percent"] for r in scored) / len(scored)
        lines += ["", f"**平均覆盖率 {avg:.1f}%**"
                  + (f"（{len(rows) - len(scored)} 份因术语过少未计入）"
                     if len(scored) < len(rows) else "")]

    detail = [r for r in rows if r.get("missing")]
    if detail:
        lines += ["", "### 疑似未覆盖明细", ""]
        for r in detail:
            pct = f"{r['percent']}%" if r.get("reliable", True) else "术语过少"
            lines.append(f"**{r['name']}**（覆盖率 {pct}）")
            lines.append("")
            for term, page in r["missing"]:
                lines.append(f"- `{term}`（源课件第 {page} 页）")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
