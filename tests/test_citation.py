"""P3a 引用核验单测。

前面几组测的是**逻辑**，最后一组「标定回归」测的是**判据还灵不灵**——
判据的区分能力是靠真实语料标定出来的，改了任何一环（分词、术语抽取、阈值）
都可能让它悄悄退化，而那正是这个功能唯一的卖点。
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from app.services.citation import (
    RANK_TOLERANCE,
    VERDICT_NOT_FOUND,
    VERDICT_SUPPORTED,
    VERDICT_WEAK,
    CitationVerifier,
    extract_citations,
    summarize,
)
from app.services.retriever import Chunk, build_chunks

COURSE_DIR = Path(__file__).resolve().parent.parent / "课件"

# ------------------------------------------------------------------ 语料桩
#
# 术语密度是刻意做高的：`_verdict` 在术语 <2 个时会直接放行（不下结论），
# 所以桩文本太短会让"假引用应判待核"的用例永远失败——那不是判据的问题，是样本的问题。
CHUNKS = [
    Chunk(
        "a.pptx::1",
        "a.pptx",
        1,
        "",
        "CRUSH 算法负责数据分布，使用一致性哈希，pg_num 决定归置组数量。",
    ),
    Chunk(
        "a.pptx::2",
        "a.pptx",
        2,
        "",
        "RBD 块存储通过 rbd 命令创建映像，支持快照与克隆，需要 rbd pool。",
    ),
    Chunk(
        "b.pptx::1",
        "b.pptx",
        1,
        "",
        "cephadm 用于引导集群，bootstrap 命令初始化第一个 monitor 节点。",
    ),
]


@pytest.fixture(scope="module")
def verifier() -> CitationVerifier:
    return CitationVerifier(CHUNKS)


# ------------------------------------------------------------------ 引用抽取


class TestExtractCitations:
    def test_基本形式(self):
        cites = extract_citations("CRUSH 负责分布（源：a.pptx 第 1 页）")
        assert len(cites) == 1
        assert (cites[0].file, cites[0].page) == ("a.pptx", 1)

    def test_文件名含英文括号(self):
        """★ 回归：标定时抓到的真 bug。

        真实课件名里就有 `Ceph 与 Kubernetes 集成 (CSI).pptx`，
        第一版正则用 `[^（）()]` 排除了英文括号 → **整套样本一条都抽不出来**
        （表现是"未抽出引用"三组各 20~35 条），而看起来像是判据不好使。
        """
        cites = extract_citations("CSI 插件负责挂载（源：Ceph 与 Kubernetes 集成 (CSI).pptx 第 5 页）")
        assert len(cites) == 1
        assert cites[0].file == "Ceph 与 Kubernetes 集成 (CSI).pptx"
        assert cites[0].page == 5

    def test_文件名含中文括号(self):
        """★ 回归：改完英文括号**又踩的一个**。

        改成 `[^（）]`（只排中文括号）之后，`Ceph 对象存储（RGW）配置与使用.pptx`
        这类名字又抽不出来了——而 14 份课件里含中文括号的占一半。
        """
        cites = extract_citations("对象存储网关（源：Ceph 对象存储（RGW）配置与使用.pptx 第 20 页）")
        assert len(cites) == 1
        assert cites[0].file == "Ceph 对象存储（RGW）配置与使用.pptx"
        assert cites[0].page == 20

    def test_省略文件名时用默认值(self):
        cites = extract_citations("正文（源：第 7 页）", default_file="x.pptx")
        assert cites[0].file == "x.pptx"

    def test_多页写法展开成多条(self):
        cites = extract_citations("正文（源：a.pptx 第 7、8 页）")
        assert [c.page for c in cites] == [7, 8]

    def test_论断取引用标记之前(self):
        answer = "前面无关的话。CRUSH 负责数据分布（源：a.pptx 第 1 页）"
        assert extract_citations(answer)[0].claim == "CRUSH 负责数据分布"

    def test_句号写在括号前也不丢论断(self):
        """模型偶尔这么写，切分后是空串——不兜底等于白漏一条引用。"""
        cites = extract_citations("CRUSH 负责数据分布。（源：a.pptx 第 1 页）")
        assert cites[0].claim.strip() != ""

    def test_分号连写的多条引用不合并(self):
        """★ 回归：正则第三个坑（真跑抓到的）。

        真实模型的输出里常见「（源：A 第 1 页；源：A 第 2 页）」这种连写形式。
        file 段若用 `.` 匹配任意字符，会把两条**吞成一条**——
        表现为 file 变成 `A.pdf 第 1 页；源：A.pdf`、页码取最后一个，
        于是三条真引用被判「未找到该页」。
        """
        answer = "取指阶段（源：8.2 指令周期.pdf 第 1 页；源：8.2 指令周期.pdf 第 2 页）"
        cites = extract_citations(answer)
        assert [(c.file, c.page) for c in cites] == [
            ("8.2 指令周期.pdf", 1),
            ("8.2 指令周期.pdf", 2),
        ]

    def test_分号连写时文件名不含分号(self):
        """合并 bug 的直接特征：file 里不该出现分号或「第 N 页」字样。"""
        answer = "结论（源：甲.pdf 第 1 页；源：乙.pdf 第 2 页；源：丙.pdf 第 3 页）"
        for cite in extract_citations(answer):
            assert "；" not in cite.file and ";" not in cite.file
            assert "第" not in cite.file

    def test_半角括号与冒号也认(self):
        cites = extract_citations("正文 (源: a.pptx 第 3 页)")
        assert len(cites) == 1 and cites[0].page == 3

    def test_没有引用时返回空(self):
        assert extract_citations("这段回答没有给引用") == []


# ------------------------------------------------------------------ 核验


class TestVerify:
    def test_真引用判可信(self, verifier):
        answer = "CRUSH 算法负责数据分布，使用一致性哈希，pg_num 决定归置组数量（源：a.pptx 第 1 页）"
        checks = verifier.verify(answer)
        # 论断逐字来自该页 —— 但判据走的是"检索排名"，不是"字符串相等"，
        # 所以这条同时验证了 rank 信号在自匹配场景下确实给出第 1 名。
        assert checks[0].rank == 1
        assert checks[0].verdict == VERDICT_SUPPORTED

    def test_页码错判待核(self, verifier):
        answer = "CRUSH 算法负责数据分布，使用一致性哈希，pg_num 决定归置组数量（源：a.pptx 第 2 页）"
        checks = verifier.verify(answer)
        assert checks[0].found is True
        assert checks[0].rank > RANK_TOLERANCE
        assert checks[0].verdict == VERDICT_WEAK

    def test_跨课件引用判待核(self, verifier):
        answer = "CRUSH 算法负责数据分布，使用一致性哈希，pg_num 决定归置组数量（源：b.pptx 第 1 页）"
        assert verifier.verify(answer)[0].verdict == VERDICT_WEAK

    def test_不存在的页判未找到(self, verifier):
        """页码对不上知识库是**硬事实**，不是词面判据的灰色地带，可以断言。"""
        checks = verifier.verify("随便什么内容（源：a.pptx 第 999 页）")
        assert checks[0].found is False
        assert checks[0].verdict == VERDICT_NOT_FOUND

    def test_不存在的课件判未找到(self, verifier):
        assert verifier.verify("内容（源：没这个.pptx 第 1 页）")[0].verdict == VERDICT_NOT_FOUND

    def test_术语太少也按rank判定而不放行(self, verifier):
        """★ 回归：第一版有个"术语太少就直接放行"的分支（思路抄自 P1 的
        MIN_TERMS_FOR_PERCENT），结果**实测假引用通过率被抬到 32.5%**——
        错引用里恰好有相当一部分是"论断没有可识别术语"的情况。

        rank 用的是论断的全部 token，不依赖术语抽取抽出来几个，
        所以这个前置条件本来就不该存在。
        """
        checks = verifier.verify("这个（源：b.pptx 第 1 页）")
        assert checks[0].terms_total < 2  # 确实抽不出术语
        assert checks[0].verdict == VERDICT_WEAK  # 但仍然照判
        assert checks[0].coverage == 0.0  # 不报 1.0 与判定自相矛盾

    def test_附带源页原文供自查(self, verifier):
        checks = verifier.verify("CRUSH 负责分布（源：a.pptx 第 1 页）")
        assert "CRUSH" in checks[0].excerpt

    def test_结果可复现(self, verifier):
        """判据必须是纯函数：同一输入跑两次结论完全一致（不调模型、无随机）。"""
        answer = "CRUSH 算法负责数据分布，使用一致性哈希（源：a.pptx 第 1 页）"
        assert verifier.verify(answer)[0].to_dict() == verifier.verify(answer)[0].to_dict()

    def test_to_dict字段完整(self, verifier):
        d = verifier.verify("CRUSH 算法负责数据分布，pg_num 决定数量（源：a.pptx 第 1 页）")[0].to_dict()
        for key in ("claim", "file", "page", "verdict", "verdict_label", "coverage", "rank", "excerpt"):
            assert key in d


# ------------------------------------------------------------------ 汇总


class TestSummarize:
    def test_无引用时的提示(self):
        assert summarize([])["total"] == 0
        assert "未引用" in summarize([])["note"]

    def test_计数与提示(self, verifier):
        answer = (
            "CRUSH 算法负责数据分布，使用一致性哈希，pg_num 决定归置组数量（源：a.pptx 第 1 页）"
            "另一句编的（源：a.pptx 第 999 页）"
        )
        result = summarize(verifier.verify(answer))
        assert result["total"] == 2
        assert result["not_found"] == 1
        assert "不存在" in result["note"]

    def test_不给出总体百分比(self, verifier):
        """把若干条独立判断平均成一个数，会掩盖"其中有一条是编的"——而那恰恰最关键。"""
        result = summarize(verifier.verify("CRUSH 负责分布（源：a.pptx 第 1 页）"))
        assert not any(k in result for k in ("score", "percent", "confidence"))


# ------------------------------------------------------------------ 标定回归

requires_course = pytest.mark.skipif(
    not COURSE_DIR.exists() or not list(COURSE_DIR.glob("*.pptx")),
    reason="课件目录不存在或为空，跳过标定回归",
)


@pytest.fixture(scope="module")
def real_verifier():
    from app.services.extractor import extract_any

    results = [extract_any(p) for p in sorted(COURSE_DIR.glob("*.pptx"))]
    chunks = build_chunks(results)
    return CitationVerifier(chunks), chunks


@requires_course
class TestCalibrationRegression:
    """真语料上的标定回归。

    标定读数（claim 200 字，见 scripts/p3_citation_calibrate.py）：
        真引用通过 **87.0%**，假引用通过 **1.0%**
    这里的阈值留了余量（真 ≥80%，假 ≤8%），因为样本量比标定小得多；
    它要防的是"判据整体失效"，不是复现小数点。
    """

    @staticmethod
    def _sample(chunks, rng, n=40):
        usable = [c for c in chunks if len(c.text) >= 40]
        return rng.sample(usable, min(n, len(usable)))

    def test_真引用通过率不低于标定值(self, real_verifier):
        verifier, chunks = real_verifier
        rng = random.Random(20261015)
        sample = self._sample(chunks, rng)
        ok = 0
        for c in sample:
            answer = f"{c.text[:200].replace(chr(10), ' ')}（源：{c.file} 第 {c.page} 页）"
            checks = verifier.verify(answer)
            ok += bool(checks) and checks[0].verdict == VERDICT_SUPPORTED
        assert ok / len(sample) >= 0.80, f"真引用通过率 {ok/len(sample):.1%} 低于 80%"

    def test_假引用通过率不高于标定值(self, real_verifier):
        """页码错：论断取自甲页、引用标成乙页。"""
        verifier, chunks = real_verifier
        rng = random.Random(20261016)
        by_file: dict[str, list] = {}
        for c in chunks:
            by_file.setdefault(c.file, []).append(c)
        sample = [
            c
            for c in self._sample(chunks, rng, 60)
            if len([x for x in by_file[c.file] if x.page != c.page]) > 0
        ][:40]

        passed = 0
        for c in sample:
            other = rng.choice([x for x in by_file[c.file] if x.page != c.page])
            answer = f"{c.text[:200].replace(chr(10), ' ')}（源：{c.file} 第 {other.page} 页）"
            checks = verifier.verify(answer)
            passed += bool(checks) and checks[0].verdict == VERDICT_SUPPORTED
        rate = passed / len(sample)
        assert rate <= 0.08, f"假引用通过率 {rate:.1%} 偏高，判据在退化"
