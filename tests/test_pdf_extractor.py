"""PDF 提取器测试：重点覆盖「文字层健康检测」。

判据来自对 6 份真实 PDF 的实测（见 pdf_extractor 模块文档）：
健康的中文 PDF（简历）与英文 PDF（LLM 课件）乱码字符占比都是 0.0%，
而 4 份损坏的中文课件是 17.9%–42.1% —— 两者完全不重叠。

最容易写错的地方是「用中文占比判断」：英文课件本来就没有中文，
那样会把正常文件误杀。这里专门用 LLM.pdf 的特征做回归。
"""
from pathlib import Path

import pytest

from app.services import extractor as ex
from app.services.extractor import SUPPORTED_EXTENSIONS, extract_any
from app.services.pdf_extractor import (
    PdfExtractionError,
    _strip_repeated_lines,
    assess_text_health,
    extract_pdf,
)
from app.services.ppt_extractor import PptExtractionError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 实测样本：某课件 pypdf 的原始输出（中文字符全部丢失，只剩水印与乱码）
GARBLED = "²ö;w\nÜÔĈå\nÅ\n王道考研/CSKAOYAN.COM\nºÜÔć/=¡*ĬúÓ©Ĩ\n"
NORMAL_CN = "磁盘调度算法：先来先服务、最短寻道时间优先、扫描算法与循环扫描。"
NORMAL_EN = "Selected Topics on Large Language Models, by Jiang Chenghan."


# ---------- 文字层健康检测 ----------

def test_health_accepts_chinese():
    ok, why = assess_text_health(NORMAL_CN * 20)
    assert ok
    assert why == ""


def test_health_accepts_english():
    """英文课件没有中文是正常的，不能因缺中文就判损坏（实测 LLM.pdf）。"""
    ok, why = assess_text_health(NORMAL_EN * 20)
    assert ok
    assert why == ""


def test_health_rejects_garbled():
    ok, why = assess_text_health(GARBLED * 20)
    assert not ok
    assert "损坏" in why


def test_health_rejects_cid_markers():
    """pdfminer 系返回的 (cid:123) 是文字层未映射的另一种表现。"""
    ok, why = assess_text_health("(cid:217)(cid:259)(cid:147)(cid:253) " * 20)
    assert not ok
    assert "映射表" in why


def test_health_skips_short_text():
    """内容太少不做判断：封面页只有标题、短乱码页都交给上层「无内容」检查。"""
    assert assess_text_health("第一章 绪论")[0]
    assert assess_text_health(GARBLED)[0]


# ---------- 跨页重复行过滤（水印 / 页眉页脚） ----------

def test_strip_removes_repeated_watermark():
    pages = [f"王道考研/CSKAOYAN.COM\n第{i}页正文内容" for i in range(1, 6)]
    out = _strip_repeated_lines(pages)
    assert all("王道考研" not in p for p in out)
    assert "第3页正文内容" in out[2]


def test_strip_keeps_unique_lines():
    pages = ["各不相同的内容一", "各不相同的内容二", "各不相同的内容三"]
    assert _strip_repeated_lines(pages) == pages


def test_strip_disabled_for_short_doc():
    """少于 3 页不过滤，避免把真实标题当噪声删掉。"""
    pages = ["同一个标题", "同一个标题"]
    assert _strip_repeated_lines(pages) == pages


def test_strip_keeps_long_repeated_paragraph():
    """只过滤短行：长段落重复更可能是真实内容，误删代价高。"""
    long_line = "这是一段明显超过四十个字符阈值的重复正文内容，不应该被当作水印删除掉。" * 2
    pages = [long_line] * 4
    assert _strip_repeated_lines(pages) == pages


# ---------- extract_any 分发 ----------

def test_supported_extensions():
    assert SUPPORTED_EXTENSIONS == {".pptx", ".pdf"}


def test_extract_any_dispatches_pptx(monkeypatch, tmp_path):
    sentinel = object()
    monkeypatch.setattr(ex, "extract_pptx", lambda p, image_dir=None: sentinel)
    assert ex.extract_any(tmp_path / "a.pptx") is sentinel


def test_extract_any_dispatches_pdf(monkeypatch, tmp_path):
    sentinel = object()
    monkeypatch.setattr(ex, "extract_pdf", lambda p, image_dir=None: sentinel)
    assert ex.extract_any(tmp_path / "a.pdf") is sentinel


def test_extract_any_rejects_legacy_ppt(tmp_path):
    with pytest.raises(PptExtractionError, match=".ppt 旧格式"):
        extract_any(tmp_path / "old.ppt")


def test_extract_any_rejects_unknown_format(tmp_path):
    with pytest.raises(PptExtractionError, match="不支持的文件格式"):
        extract_any(tmp_path / "a.docx")


# ---------- extract_pdf 错误路径 ----------

def test_extract_pdf_missing_file(tmp_path):
    with pytest.raises(PdfExtractionError):
        extract_pdf(tmp_path / "nope.pdf")


def test_extract_pdf_wrong_suffix(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(PdfExtractionError):
        extract_pdf(f)


def test_real_pdfs_never_crash():
    """项目根目录若有真实 PDF 样件，结果必须是「通过」或「明确拦截」，不能崩溃。

    没有样件时跳过 —— 不把用户的临时文件变成测试的硬依赖。
    """
    pdfs = sorted(PROJECT_ROOT.glob("*.pdf"))
    if not pdfs:
        pytest.skip("项目根目录没有 PDF 样件")

    for p in pdfs:
        try:
            result = extract_pdf(p)
        except PdfExtractionError:
            continue  # 明确拦截（文字层损坏）是预期结果之一
        assert result.total_pages >= 1
        assert result.filename == p.name
