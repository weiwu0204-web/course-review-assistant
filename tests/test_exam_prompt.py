"""生成提示词中「往年试题」逻辑的单元测试（纯函数，不调真实 API）。"""
from app.models import ExtractionResult, GenerateRequest
from app.services.ai_generator import EXAM_MAX_CHARS, build_prompt


def _result() -> ExtractionResult:
    return ExtractionResult(filename="t.pptx", total_pages=1, slides=[], title="测试")


def _req(exam_mode: str = "direct") -> GenerateRequest:
    return GenerateRequest(filename="t.pptx", template="复习指南", exam_mode=exam_mode)


def test_no_exam_text_no_exam_section():
    """没有题源时，prompt 不应出现任何真题相关指令。"""
    prompt = build_prompt(_result(), _req(), [], exam_text="")
    assert "往年试题" not in prompt
    assert "历年真题" not in prompt


def test_direct_mode_keeps_questions_verbatim():
    """直搬模式：题目原文必须完整出现在 prompt 里，指令要求原样搬运。"""
    exam = "一、选择题\n1. TCP 是（ ）层协议。\nA 物理 B 传输 C 网络 D 应用"
    prompt = build_prompt(_result(), _req("direct"), [], exam_text=exam)
    assert "本校往年试题" in prompt
    assert exam in prompt  # 题目原文一字不差地带入
    assert "历年真题" in prompt
    assert "原样" in prompt  # 要求不改写题目
    assert "不要编造" in prompt or "绝不编造" in prompt


def test_predict_mode_asks_for_style_imitation():
    """参考预测模式：要求仿风格出预测题，且明示非原题。"""
    exam = "一、选择题（每题2分）\n1. IP 地址属于 OSI（ ）层。"
    prompt = build_prompt(_result(), _req("predict"), [], exam_text=exam)
    assert "预测题" in prompt
    assert "非原题" in prompt  # 不得冒充真实真题
    assert "题型结构" in prompt  # 要求先分析原卷风格


def test_unknown_mode_falls_back_to_direct():
    """exam_mode 传了非法值时，回退到直搬规则而不是报错。"""
    prompt = build_prompt(_result(), _req("hack"), [], exam_text="某题")
    assert "历年真题" in prompt


def test_exam_text_truncated_at_max():
    """超长题源要截断，避免顶爆输入窗口。"""
    exam = "考点" * 20000  # 40000 字符 > EXAM_MAX_CHARS
    prompt = build_prompt(_result(), _req(), [], exam_text=exam)
    # prompt 里出现的题文部分不应超过上限（去掉指令与模板本身的干扰，直接数关键词）
    assert prompt.count("考点") * 2 <= EXAM_MAX_CHARS + 10
