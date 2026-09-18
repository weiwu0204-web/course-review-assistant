"""PPT 提取服务单元测试。"""
from pathlib import Path

from pptx import Presentation

from app.services.ppt_extractor import (
    PptExtractionError,
    extract_pptx,
    format_for_ai,
)


def _make_sample_pptx(tmp_path: Path) -> Path:
    """构造一个包含文字、表格、组合形状的测试 PPT。"""
    pptx_path = tmp_path / "sample.pptx"
    prs = Presentation()

    # 第一页：标题 + 正文
    slide1 = prs.slides.add_slide(prs.slide_layouts[1])
    slide1.shapes.title.text = "数据结构复习"
    slide1.placeholders[1].text = "栈：后进先出\n队列：先进先出"

    # 第二页：表格
    slide2 = prs.slides.add_slide(prs.slide_layouts[6])
    table_shape = slide2.shapes.add_table(2, 2, 0, 0, 4000000, 1500000)
    table = table_shape.table
    table.cell(0, 0).text = "操作"
    table.cell(0, 1).text = "复杂度"
    table.cell(1, 0).text = "入栈"
    table.cell(1, 1).text = "O(1)"

    prs.save(pptx_path)
    return pptx_path


def test_extract_text(tmp_path: Path):
    path = _make_sample_pptx(tmp_path)
    result = extract_pptx(path)

    assert result.total_pages == 2
    assert result.title == "数据结构复习"
    assert "栈" in result.slides[0].text
    assert "O(1)" in result.slides[1].text


def test_extract_missing_file(tmp_path: Path):
    try:
        extract_pptx(tmp_path / "not_exist.pptx")
        assert False, "应当抛出异常"
    except PptExtractionError:
        pass


def test_format_for_ai(tmp_path: Path):
    result = extract_pptx(_make_sample_pptx(tmp_path))
    text = format_for_ai(result)
    assert "第 1 页" in text
    assert "数据结构复习" in text