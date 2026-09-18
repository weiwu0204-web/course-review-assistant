"""生成一份模拟真实课程 PPT，用于验证提取器的覆盖情况。

覆盖 6 种典型页面：标题页、要点页、表格页、图表页、纯图片页、带备注页。
运行：python scripts/make_sample.py
"""
from pathlib import Path

from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "samples"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _fake_screenshot(path: Path) -> Path:
    """生成一张模拟"老师直接贴的截图"。"""
    img = Image.new("RGB", (960, 540), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 920, 500], outline="black", width=3)
    draw.text((80, 90), "Algorithm Complexity Comparison", fill="black")
    draw.text((80, 140), "Bubble Sort   O(n^2)", fill="black")
    draw.text((80, 180), "Quick Sort    O(n log n)", fill="black")
    img.save(path)
    return path


def build() -> Path:
    prs = Presentation()
    blank = prs.slide_layouts[6]

    # 1. 标题页
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "数据结构与算法"
    s.placeholders[1].text = "第三章 排序\n主讲：张老师"

    # 2. 要点页
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "3.2 快速排序"
    s.placeholders[1].text = (
        "基本思想：分治法\n"
        "1. 选取基准元素 pivot\n"
        "2. 分区：小于 pivot 放左边，大于放右边\n"
        "3. 递归排序左右两个子序列\n"
        "平均时间复杂度 O(n log n)，最坏 O(n^2)"
    )

    # 3. 表格页
    s = prs.slides.add_slide(blank)
    s.shapes.add_textbox(Inches(0.5), Inches(0.4), Inches(6), Inches(0.6)).text_frame.text = (
        "排序算法对比"
    )
    rows, cols = 4, 4
    table = s.shapes.add_table(rows, cols, Inches(0.5), Inches(1.2), Inches(9), Inches(2)).table
    header = ["算法", "平均复杂度", "最坏复杂度", "稳定"]
    for c, h in enumerate(header):
        table.cell(0, c).text = h
    data = [
        ["冒泡排序", "O(n^2)", "O(n^2)", "是"],
        ["快速排序", "O(n log n)", "O(n^2)", "否"],
        ["归并排序", "O(n log n)", "O(n log n)", "是"],
    ]
    for r, row in enumerate(data, start=1):
        for c, val in enumerate(row):
            table.cell(r, c).text = val

    # 4. 图表页（文字藏在图表数据里，页面上几乎没有可直接读的文本）
    s = prs.slides.add_slide(blank)
    s.shapes.add_textbox(Inches(0.5), Inches(0.4), Inches(6), Inches(0.6)).text_frame.text = (
        "性能随规模变化"
    )
    chart_data = CategoryChartData()
    chart_data.categories = ["n=100", "n=1000", "n=10000"]
    chart_data.add_series("冒泡排序", (0.01, 0.8, 80.0))
    chart_data.add_series("快速排序", (0.001, 0.02, 0.25))
    s.shapes.add_chart(
        XL_CHART_TYPE.LINE, Inches(0.5), Inches(1.2), Inches(9), Inches(4), chart_data
    )

    # 5. 纯图片页（老师直接贴的截图，没有任何文字层）
    s = prs.slides.add_slide(blank)
    shot = _fake_screenshot(OUT_DIR / "_screenshot.png")
    s.shapes.add_picture(str(shot), Inches(0.5), Inches(0.5), width=Inches(9))

    # 6. 带备注的页（备注里写着真正的考点）
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "本章小结"
    s.placeholders[1].text = "掌握快速排序的分区过程"
    s.notes_slide.notes_text_frame.text = (
        "【重点·必考】手写 partition 函数的实现过程\n"
        "【易错点】pivot 选取不当会退化成 O(n^2)\n"
        "去年期末考了快排与归并的稳定性对比，今年大概率还考"
    )

    path = OUT_DIR / "sample_course.pptx"
    prs.save(path)
    return path


if __name__ == "__main__":
    print(build())
