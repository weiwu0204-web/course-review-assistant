"""文档类型 × 输出风格的 prompt 组装逻辑（纯函数，不调真实 API）。

背景：习题生成套用通用风格说明会自相矛盾——「篇幅控制在 500 字以内」装不下
一份模拟卷，「用表格汇总关键信息」也不是出题指令。因此习题生成走独立的
EXAM_STYLE_INSTRUCTIONS，其余文档类型仍走通用 STYLE_INSTRUCTIONS。
"""
from app.models import ExtractionResult, GenerateRequest
from app.services.ai_generator import build_prompt


def _result() -> ExtractionResult:
    return ExtractionResult(filename="t.pptx", total_pages=1, slides=[], title="测试")


def test_exam_template_style_has_no_generic_limit():
    """习题生成 × 精简：不得出现通用风格的「500 字以内」（会把题量逼没）。"""
    req = GenerateRequest(filename="t.pptx", template="习题生成", style="精简")
    prompt = build_prompt(_result(), req, [])
    assert "500 字" not in prompt
    assert "一句话要点" in prompt  # 换成试卷语境的精简要求


def test_exam_template_table_style_falls_back_to_detailed():
    """习题生成 × 表格化（前端已不提供，此处纯防御）：兜底详解，不掺通用表格句。"""
    req = GenerateRequest(filename="t.pptx", template="习题生成", style="表格化")
    prompt = build_prompt(_result(), req, [])
    assert "表格" not in prompt
    assert "完整参考答案与解析" in prompt


def test_review_template_keeps_generic_table_style():
    """复习指南 × 表格化：仍走通用风格（表格对比对复习文档有意义）。"""
    req = GenerateRequest(filename="t.pptx", template="复习指南", style="表格化")
    prompt = build_prompt(_result(), req, [])
    assert "表格对比" in prompt


def test_exam_template_style_exam_source_still_attached():
    """习题生成带往年试题时，题源规则照常追加（风格特判不破坏 exam 逻辑）。"""
    req = GenerateRequest(filename="t.pptx", template="习题生成", style="详细", exam_mode="predict")
    prompt = build_prompt(_result(), req, [], exam_text="一、选择题\n1. TCP 属于（ ）层。")
    assert "预测题" in prompt
    assert "非原题" in prompt
