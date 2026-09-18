"""reducer 汇编器测试：标题提取（含代码块跳过）、总册结构与出处锚点、正文原样保留。"""
from app.services.reducer import build_master_doc, extract_headings


def test_extract_headings_h1_h2_only():
    """只取 H1/H2 进目录，H3 以下不收。"""
    md = "# 第一章\n\n## 1.1 概念\n### 1.1.1 细节\n正文"
    assert extract_headings(md) == ["第一章", "1.1 概念"]


def test_extract_headings_skips_code_blocks():
    """代码块里的 # 是注释不是标题，必须跳过。"""
    md = "# 标题\n```bash\n# 这不是标题\n```\n## 真标题"
    assert extract_headings(md) == ["标题", "真标题"]


def test_extract_headings_empty():
    assert extract_headings("没有任何标题的正文") == []


def test_build_master_doc_structure():
    """总册 = 课程封面 + 目录 + 逐节（出处锚点 + 截断警示 + 正文原样）。"""
    sections = [
        {"idx": 1, "title": "第一章 概述", "source": "01_概述.pptx", "pages": 62,
         "warning": "", "body": "# 第一章 概述\n\n正文A"},
        {"idx": 2, "title": "第二章 物理层", "source": "02_物理层.pptx", "pages": 58,
         "warning": "truncated", "body": "# 第二章 物理层\n\n正文B"},
    ]
    doc = build_master_doc("计算机网络", sections)

    assert doc.startswith("# 《计算机网络》期末复习总册")
    assert "由 2 份课件合成" in doc
    assert "## 目录" in doc
    assert "1. 第一章 概述" in doc
    assert "2. 第二章 物理层（输出被截断，内容可能不完整）" in doc
    assert "## 第 1 节：第一章 概述" in doc
    assert "> 来源：01_概述.pptx（共 62 页）" in doc
    assert "⚠️ 本节生成时输出被截断" in doc


def test_build_master_doc_body_verbatim():
    """汇编式 reduce 的底线：正文原样保留，不改写不删减。"""
    body = "# 第二章 物理层\n\n- 奈奎斯特定理：C = 2B log2 V\n\n> 备注里老师说必考"
    doc = build_master_doc("课", [{"idx": 1, "title": "第二章 物理层",
                                    "source": "02.pptx", "pages": 1,
                                    "warning": "", "body": body}])
    assert body in doc
