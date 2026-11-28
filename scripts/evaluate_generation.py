"""生成质量评测：三种提取配置的消融实验。

回答一个问题：ppt-reviewer 的「分层提取」到底让生成质量好了多少？
以及每一层（备注 / 表格 / 图表 / 图片视觉模型）各自的边际贡献。

三种配置（对同一份 PPT 生成「复习指南」）：
  - plain   ：只提取文本框（最朴素的 baseline，不解析表格/备注/公式/图表）
  - layered ：完整分层提取（文本+表格+备注+公式+图表数据），纯文本模型
  - vision  ：分层提取 + 把嵌入图片交给视觉模型（线上完整方案）

评测载体：tests/samples/sample_course.pptx（make_sample.py 生成，6 页覆盖
标题/要点/表格/图表/纯图片/带备注六种典型页面，ground truth 完全已知）。

指标：
  1. 知识点召回率  —— ground truth 知识点实体在生成文档中的覆盖比例
  2. 数据保留率    —— 表格 / 图表里的硬数据（复杂度、数值）保留比例
  3. 备注考点召回  —— 只存在于教师备注里的考点出现比例
  4. 图片内容召回  —— 只存在于嵌入图片里的内容出现比例（测视觉模型边际价值）
  5. 幻觉率（参考）—— 生成文档中无法溯源到 PPT 内容的断言行占比，附明细人工核查

用法：
    python scripts/evaluate_generation.py                # 跑全部三种配置
    python scripts/evaluate_generation.py --configs plain,vision
    python scripts/evaluate_generation.py --no-save      # 不写结果文件
    python scripts/evaluate_generation.py --dry-run      # 不调 API，只验证提取链路
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI  # noqa: E402, F401  确保 openai 可导入（纯文本生成也走它）

from app.config import get_settings  # noqa: E402
from app.models import ExtractionResult, GenerateRequest, SlideContent  # noqa: E402
from app.services.ai_generator import generate_review_document  # noqa: E402
from app.services.ppt_extractor import extract_pptx  # noqa: E402
from pptx import Presentation  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "tests" / "samples" / "sample_course.pptx"
OUT_JSON = Path(__file__).resolve().parent.parent / "eval" / "generation_eval_results.json"

CONFIGS = {
    "plain": "只提取文本框（最朴素 baseline）",
    "layered": "分层提取 文本+表格+备注+公式+图表（无图）",
    "vision": "分层提取 + 图片视觉模型（完整方案）",
}

# ---------------------------------------------------------------------------
# Ground truth：从 make_sample.py 的源码推导，保证与样例内容严格一致
# ---------------------------------------------------------------------------

# 知识点实体：用于「知识点召回率」
KNOWLEDGE_POINTS = [
    "快速排序", "分治法", "pivot", "基准", "分区",
    "冒泡排序", "归并排序", "稳定性", "递归",
]

# 硬数据实体：表格 + 图表里的真实数据，用于「数据保留率」
HARD_DATA = [
    "o(n^2)", "o(n log n)",          # 表格里的复杂度
    "n=100", "n=1000", "n=10000",    # 图表分类轴
    "0.01", "0.8", "80.0",           # 冒泡排序性能数值
    "0.001", "0.02", "0.25",         # 快速排序性能数值
]

# 备注独有考点：只在教师备注里出现，用于「备注考点召回」
NOTES_ONLY = [
    "partition", "退化", "必考", "去年",
]

# 图片独有内容：只存在于第 5 页嵌入截图里（英文，视觉模型才能读到）
IMAGE_ONLY = [
    "algorithm complexity", "bubble sort", "quick sort",
]

# 教学通用词：允许生成文档出现而不算幻觉的过渡性表达
GENERIC_TERMS = [
    "排序", "算法", "复杂度", "时间复杂度", "空间复杂度", "性能", "比较", "交换",
    "实现", "示例", "例子", "复习", "重点", "考点", "掌握", "理解", "总结",
    "建议", "章节", "第三章", "数据结构", "张老师", "课程", "知识点", "结论",
    "选择", "冒泡", "快排", "归并", "基准元素", "子序列", "递归调用", "平均情况",
    "最坏情况", "稳定性对比", "手写", "选取", "不当", "过程", "基本思想",
]

# 数值类实体允许的常见变体（如 80.0 可能被模型写成 80）
_NUM_VARIANTS = {
    "80.0": ["80.0", "80"],
}


_SUPERSCRIPTS = str.maketrans({"²": "^2", "³": "^3", "⁰": "^0", "¹": "^1", "⁴": "^4"})


def _norm(s: str) -> str:
    """归一化：小写、去空白与括号、上标转 ^n。

    用于 O(n log n) / O(nlogn) / On² / O(n^2) 这类写法差异的匹配。
    """
    s = s.translate(_SUPERSCRIPTS)
    return re.sub(r"[\s()（）]", "", s).lower()


def _norm_entities(entities: list[str]) -> list[tuple[str, list[str]]]:
    """返回 [(规范名, [可匹配变体])]，数值类带尾零容错。"""
    out = []
    for ent in entities:
        variants = _NUM_VARIANTS.get(ent, [ent])
        out.append((ent, [_norm(v) for v in variants]))
    return out


KNOWLEDGE_N = _norm_entities(KNOWLEDGE_POINTS)
HARD_DATA_N = _norm_entities(HARD_DATA)
NOTES_ONLY_N = _norm_entities(NOTES_ONLY)
IMAGE_ONLY_N = _norm_entities(IMAGE_ONLY)
GENERIC_N = _norm_entities(GENERIC_TERMS)

# 可溯源实体全集（用于幻觉率判定）
TRACEABLE = KNOWLEDGE_N + HARD_DATA_N + NOTES_ONLY_N + IMAGE_ONLY_N + GENERIC_N


# ---------------------------------------------------------------------------
# 朴素提取：模拟「只会读文本框」的最简实现（不含表格/备注/公式/图表）
# ---------------------------------------------------------------------------

def _extract_plain(path: Path) -> ExtractionResult:
    prs = Presentation(path)
    slides: list[SlideContent] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        lines.append(t)
        slides.append(SlideContent(page=i, text="\n".join(lines), notes="", images=[]))
    title = next(
        (next((ln for ln in s.text.splitlines() if ln.strip()), "") for s in slides if s.text.strip()),
        path.name,
    )
    return ExtractionResult(
        filename=path.name, total_pages=len(slides), slides=slides, title=title[:50]
    )


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def _match_entities(doc: str, table: list[tuple[str, list[str]]]) -> tuple[list[str], list[str]]:
    """返回 (命中的规范名列表, 未命中的规范名列表)。"""
    d = _norm(doc)
    hit, miss = [], []
    for name, variants in table:
        if any(v in d for v in variants):
            hit.append(name)
        else:
            miss.append(name)
    return hit, miss


def _split_assertions(markdown: str) -> list[str]:
    """把生成文档切成「断言行」：剥离 markdown 符号后非空的文本行。"""
    out = []
    for line in markdown.splitlines():
        clean = re.sub(r"[#*`>|\[\]()]", "", line).strip()
        if not clean or set(clean) <= {"-", "=", "_"}:
            continue
        out.append(clean)
    return out


_CODE_FEATURES = re.compile(r"[\{\}]|\+\+| for | if | while | int | swap|\d\s*=\s*\d")


def _count_hallucination(markdown: str) -> tuple[list[str], list[str]]:
    """返回 (可溯源行, 可疑行)。可疑行 = 行内不含任何可溯源实体。

    伪代码/代码片段行（含 { } ++ for if 等特征）不算可疑——它们是对
    PPT 内容（如 partition 实现）的转写，不是编造的知识点。
    """
    traceable, suspicious = [], []
    for line in _split_assertions(markdown):
        l = _norm(line)
        if _CODE_FEATURES.search(line):
            traceable.append(line)
            continue
        if any(any(v in l for v in variants) for _, variants in TRACEABLE):
            traceable.append(line)
        else:
            suspicious.append(line)
    return traceable, suspicious


def evaluate_one(config: str, markdown: str) -> dict:
    k_hit, k_miss = _match_entities(markdown, KNOWLEDGE_N)
    d_hit, d_miss = _match_entities(markdown, HARD_DATA_N)
    n_hit, n_miss = _match_entities(markdown, NOTES_ONLY_N)
    i_hit, i_miss = _match_entities(markdown, IMAGE_ONLY_N)
    traceable, suspicious = _count_hallucination(markdown)

    total_assert = len(traceable) + len(suspicious)
    return {
        "config": config,
        "config_desc": CONFIGS[config],
        "知识召回": f"{len(k_hit)}/{len(KNOWLEDGE_POINTS)}",
        "知识召回率": round(len(k_hit) / len(KNOWLEDGE_POINTS), 3),
        "数据保留": f"{len(d_hit)}/{len(HARD_DATA)}",
        "数据保留率": round(len(d_hit) / len(HARD_DATA), 3),
        "备注考点": f"{len(n_hit)}/{len(NOTES_ONLY)}",
        "备注考点率": round(len(n_hit) / len(NOTES_ONLY), 3),
        "图片内容": f"{len(i_hit)}/{len(IMAGE_ONLY)}",
        "图片内容率": round(len(i_hit) / len(IMAGE_ONLY), 3),
        "幻觉率": round(len(suspicious) / total_assert, 3) if total_assert else 0.0,
        "断言行总数": total_assert,
        "命中的知识点": k_hit,
        "漏掉的知识点": k_miss,
        "命中的硬数据": d_hit,
        "漏掉的硬数据": d_miss,
        "漏掉的备注考点": n_miss,
        "漏掉的图片内容": i_miss,
        "可疑断言行": suspicious[:15],
        "markdown": markdown,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_result(config: str, ppt: Path, image_dir: Path | None) -> ExtractionResult:
    if config == "plain":
        return _extract_plain(ppt)
    if config == "layered":
        return extract_pptx(ppt)
    return extract_pptx(ppt, image_dir=image_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成质量消融评测")
    parser.add_argument(
        "--configs",
        default=",".join(CONFIGS),
        help=f"要跑的配置，逗号分隔：{','.join(CONFIGS)}",
    )
    parser.add_argument("--no-save", action="store_true", help="不写结果文件")
    parser.add_argument("--dry-run", action="store_true", help="不调 API，只验证提取链路")
    parser.add_argument("--pptx", default=str(SAMPLE), help="评测用 PPT 路径")
    args = parser.parse_args()

    ppt = Path(args.pptx)
    if not ppt.exists():
        print(f"找不到评测 PPT: {ppt}")
        return 1

    settings = get_settings()
    if not settings.ai_api_key and not args.dry_run:
        print("未配置 AI_API_KEY：无法调用生成模型（用 --dry-run 可只验证提取链路）")
        return 1

    configs = [c.strip() for c in args.configs.split(",") if c.strip() in CONFIGS]
    if not configs:
        print(f"无效配置，可选：{','.join(CONFIGS)}")
        return 1

    print(f"评测 PPT : {ppt.name}（{ppt.stat().st_size // 1024} KB）")
    print(f"模型     : 文本={settings.ai_model} 视觉={settings.ai_vision_model}")
    print("-" * 76)

    tmp = Path(tempfile.mkdtemp(prefix="ppt_eval_"))
    results: list[dict] = []
    try:
        for config in configs:
            print(f"\n[{config}] {CONFIGS[config]}")
            result = build_result(config, ppt, tmp if config == "vision" else None)
            if args.dry_run:
                # 只打印各配置实际会发给模型的文本体量
                from app.services.ai_generator import collect_images

                prompt = build_prompt_dry(result)
                imgs, pages, skipped = collect_images(result, config == "vision")
                print(f"  [dry-run] 文本 {len(prompt)} 字符，图片 {len(imgs)} 张（页 {pages}，跳过装饰 {skipped}）")
                continue

            request = GenerateRequest(
                filename=ppt.name,
                template="复习指南",
                style="详细",
                use_vision=(config == "vision"),
            )
            try:
                markdown, sent_images, _truncated = generate_review_document(result, request)
            except Exception as exc:  # noqa: BLE001
                print(f"  !! 生成失败: {exc}")
                continue

            print(f"  生成完成：{len(markdown)} 字符，实际发送图片 {sent_images} 张")
            metrics = evaluate_one(config, markdown)
            results.append(metrics)

            print(f"  知识点召回 : {metrics['知识召回']}   ({metrics['知识召回率']:.0%})")
            print(f"  数据保留   : {metrics['数据保留']}   ({metrics['数据保留率']:.0%})")
            print(f"  备注考点   : {metrics['备注考点']}   ({metrics['备注考点率']:.0%})")
            print(f"  图片内容   : {metrics['图片内容']}   ({metrics['图片内容率']:.0%})")
            print(f"  幻觉率     : {metrics['幻觉率']:.0%}（可疑 {len(metrics['可疑断言行'])}/{metrics['断言行总数']} 行）")
            if metrics["漏掉的知识点"]:
                print(f"  漏掉知识点 : {metrics['漏掉的知识点']}")
            if metrics["漏掉的硬数据"]:
                print(f"  漏掉硬数据 : {metrics['漏掉的硬数据']}")
            if metrics["漏掉的备注考点"]:
                print(f"  漏掉备注考点: {metrics['漏掉的备注考点']}")
            if metrics["漏掉的图片内容"]:
                print(f"  漏掉图片内容: {metrics['漏掉的图片内容']}")
            if metrics["可疑断言行"]:
                print(f"  可疑断言行(前{len(metrics['可疑断言行'])}条):")
                for s in metrics["可疑断言行"]:
                    print(f"    · {s[:60]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if results and not args.no_save and not args.dry_run:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pptx": ppt.name,
            "template": "复习指南",
            "model_text": settings.ai_model,
            "model_vision": settings.ai_vision_model,
            "configs": list(CONFIGS),
            "results": results,
        }
        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入: {OUT_JSON}")

    if args.dry_run:
        print("\n[dry-run] 提取链路验证完成，未调用 API")
    return 0


def build_prompt_dry(result: ExtractionResult) -> str:
    """dry-run 时估算文本体量（复用 format_for_ai）。"""
    from app.services.ppt_extractor import format_for_ai

    return format_for_ai(result)


if __name__ == "__main__":
    sys.exit(main())
