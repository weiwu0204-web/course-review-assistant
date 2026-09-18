"""覆盖率回检测试。

重点锁住判据的**标定结论**（docs/P1-COVERAGE.md）：术语三来源、版式标签词剔除、
中文术语子串剪枝、以及「好文档读数不低于 95%」这条底线——
判据过严会让功能一开始就失去信任，比少报更糟。
"""
from app.models import ExtractionResult, SlideContent
from app.services.coverage import (
    CoverageReport,
    applies_to,
    extract_en_terms,
    measure,
    mine_cn_terms,
    not_applicable_note,
    verdict,
)


def _result(pages: list[str], notes: list[str] | None = None) -> ExtractionResult:
    slides = []
    for i, text in enumerate(pages, start=1):
        slides.append(SlideContent(page=i, text=text, notes=(notes or [""] * len(pages))[i - 1]))
    return ExtractionResult(
        filename="测试.pptx",
        total_pages=len(pages),
        title="测试",
        image_count=0,
        notes_chars=0,
        slides=slides,
    )


# ---------------------------------------------------------------- 术语抽取


def test_en_terms_and_numbers():
    t = extract_en_terms("Ceph OSD 使用 pg_num 参数，默认 0.85，单块 64KB，3 副本")
    assert "osd" in t and "crush" not in t
    assert "pg_num" in t
    assert "0.85" in t and "64kb" in t and "3副本" in t


def test_layout_labels_are_not_terms():
    """版式标签不是知识点：不能让它们进术语表去冤枉好文档。"""
    t = extract_en_terms("Key Points · Best Practices · Cause / Resolution · Page 12 · Session 3")
    assert t == set(), t


def test_domain_plausible_words_are_kept():
    """刻意不收 node / monitoring 这类可能是真概念的词——宁可少报不可冤枉。"""
    t = extract_en_terms("node monitoring optimization threshold architecture process")
    assert {"node", "monitoring", "optimization", "threshold", "architecture", "process"} <= t


def test_cn_terms_mined_by_frequency():
    pages = ["分布式存储的核心是存储池与纠删码，存储池可横向扩展"] * 3
    cn = mine_cn_terms(" ".join(pages))
    assert "存储池" in cn or "分布式" in cn
    assert cn, "应挖出中文术语"


def test_cn_terms_pruned_to_longest():
    """子串变体只保留最长的那个，避免同一知识点被算成三遍。"""
    text = "文件系统的挂载。文件系统很重要。文件系统扩展。" * 3
    cn = mine_cn_terms(text)
    assert "文件系统" in cn
    assert "文件系" not in cn and "件系统" not in cn


def test_cn_terms_exclude_stopword_grams():
    cn = mine_cn_terms("我们可以进行一下注意这些是可以进行的" * 3)
    assert not (cn & {"我们可", "可以进", "进行一", "些是可"}), cn


# ---------------------------------------------------------------- 覆盖判定


def test_full_coverage_reads_100():
    r = _result(["Ceph 的 CRUSH 算法负责数据分布"])
    rep = measure(r, "本文件讲 Ceph 的 CRUSH 算法负责数据分布。")
    assert rep.percent == 100
    assert rep.missing_terms == 0
    assert verdict(rep.percent) == "覆盖良好"


def test_missing_term_is_flagged_with_page():
    r = _result(["Ceph 的 CRUSH 算法负责数据分布", "另一页讲 pg_num"])
    rep = measure(r, "本文件只讲了 Ceph 的 CRUSH 算法负责数据分布。")
    terms = [t for t, _ in rep.missing_detail]
    assert "pg_num" in terms
    page = dict((t, p) for t, p in rep.missing_detail)["pg_num"]
    assert page == 2
    assert 0 < rep.percent < 100


def test_reworded_term_counts_as_missing_not_as_error():
    """被判为「疑似未覆盖」但概念其实在（换了个说法）——这是判据的已知局限，
    必须仍返回报告而不是抛错，且文案要能自证是「疑似」。"""
    r = _result(["RGW 的 cipher suites 配置"])
    rep = measure(r, "RGW 使用加密套件进行配置。")  # cipher suites 被译成了中文
    # cipher / suites 是两个词面术语，都被判缺失——这正是「疑似」而非「确定」的原因
    assert rep.missing_terms == 2
    assert rep.percent < 100
    # 英文术语 "rgw" 命中，所以不是 0
    assert rep.percent > 0


def test_empty_source_is_not_a_false_alarm():
    """源课件没有可提取术语时不得误报（覆盖率记 1.0）。"""
    rep = measure(_result(["", "   "]), "任意文档")
    assert rep.rate == 1.0
    assert rep.total_terms == 0


def test_pure_english_course_still_works():
    """英文课件没有中文术语，判据不能因此空转。"""
    r = _result(["CephFS provides POSIX-compliant shared filesystem via MDS daemon"])
    good = measure(r, "CephFS provides a POSIX-compliant shared filesystem via MDS daemon.")
    bad = measure(r, "本节讲块存储。")
    assert good.percent > bad.percent
    assert bad.percent < 60


def test_term_counted_once_across_pages():
    """同一术语跨页重复出现只算一次，否则重复页会稀释覆盖率。"""
    r = _result(["CRUSH 算法", "CRUSH 算法再讲一次", "CRUSH 算法"])
    rep = measure(r, "文档里只提到 CRUSH 算法。")
    assert rep.total_terms == 1


# ---------------------------------------------------------------- 报告与判定


def test_report_to_dict_shape():
    r = _result(["CRUSH 与 pg_num"])
    d = measure(r, "CRUSH").to_dict()
    assert set(d) == {"filename", "total_terms", "missing_terms", "percent",
                      "reliable", "missing_detail", "pages"}
    assert d["missing_detail"] and set(d["missing_detail"][0]) == {"term", "page"}


def test_verdict_thresholds_calibrated():
    """分界按已知质量文档标定：好的 95–100、差的 77–90，94/86 正好分开。"""
    assert verdict(97) == "覆盖良好"
    assert verdict(94) == "覆盖良好"
    assert verdict(93) == "有少量疑似遗漏"
    assert verdict(86) == "有少量疑似遗漏"
    assert verdict(85) == "疑似遗漏较多，建议核对"
    assert verdict(77) == "疑似遗漏较多，建议核对"


def test_applies_only_to_full_coverage_template():
    """覆盖率只对「复习指南 / 详细」有意义——只有这一档承诺全面覆盖知识点。

    其它档位本就不追求全覆盖（习题生成只出题、重点总结只留要点、精简有字数上限），
    硬打分等于用不成立的承诺指责文档。实测证据：用户存量批次里
    Ceph 课（复习指南/详细）14 节 95–100%，而计算机网络（习题生成、重点总结）8 节
    只有 58–61% —— 后者是假警报。
    """
    assert applies_to("复习指南", "详细") is True
    assert applies_to("习题生成", "详细") is False
    assert applies_to("重点总结", "详细") is False
    assert applies_to("复习指南", "精简") is False
    assert applies_to("复习指南", "表格化") is False
    # 缺省/空值按默认档位理解（默认就是复习指南/详细）
    assert applies_to("", "") is True
    assert applies_to("复习指南", "") is True


def test_not_applicable_note_explains_and_suggests():
    """不适用的时候不能沉默——要说清为什么，并给出怎么做才能看覆盖率。"""
    note = not_applicable_note("习题生成", "详细")
    assert "习题生成" in note and "详细" in note
    assert "复习指南" in note, "要告诉用户切到哪一档才能看覆盖率"
    assert "不做覆盖率回检" in note or "不追求" in note


def test_missing_detail_is_capped():
    """缺失清单要有上限，否则一节几百条会刷屏。"""
    r = _result([" ".join(f"term{i}" for i in range(120))])
    rep = measure(r, "什么都没覆盖")
    assert len(rep.missing_detail) <= 50
    assert isinstance(rep, CoverageReport)


def test_reliable_flag_gates_tiny_samples():
    """术语太少时不该给百分比：2 个术语缺 1 个就是 50%，纯噪声。"""
    tiny = measure(_result(["CRUSH 算法"]), "无关文档")
    assert tiny.total_terms <= 3
    assert tiny.reliable is False
    assert tiny.to_dict()["reliable"] is False

    # 注意跨页去重：三页必须用**不同**术语，否则总共仍只算 6 个
    pages = [f"term{i * 6} term{i * 6 + 1} term{i * 6 + 2} "
             f"term{i * 6 + 3} term{i * 6 + 4} term{i * 6 + 5}" for i in range(3)]
    big = measure(_result(pages), "什么都没覆盖")
    assert big.total_terms >= 8, big.total_terms
    assert big.reliable is True
