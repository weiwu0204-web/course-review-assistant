"""端到端回归：往年试题全流程（支持 直搬真题 / 参考风格出预测题 两种模式）。

用法: uv run python scripts/e2e_exam_flow.py <课件.pptx路径> [试题txt路径] [direct|predict]

流程: 上传课件 → 解析试题(文本) → 扫描版PDF应报400 → 生成(指定模式)
     → 产物写入 eval/

实现说明：用 FastAPI TestClient（ASGI 直连）而非起真实服务——
本机沙箱里 httpx 走 TCP 连 127.0.0.1 会 ReadTimeout（socket/urllib 均正常），
TestClient 不经过网络栈，稳定且更快。
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

ppt_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "课件.pptx"
exam_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "eval" / "题库_网络分层模型_通用.md"
exam_mode = sys.argv[3] if len(sys.argv) > 3 else "direct"
assert exam_mode in ("direct", "predict"), f"exam_mode 必须是 direct 或 predict，收到 {exam_mode}"

MODE_TAG = {"direct": "直搬真题", "predict": "预测题"}[exam_mode]


def main() -> None:
    with TestClient(app) as c:
        # 1) 上传课件
        with ppt_path.open("rb") as f:
            r = c.post("/api/extract", files={
                "file": (ppt_path.name, f, "application/vnd.openxmlformats-officedocument.presentationml.presentation")
            })
        assert r.status_code == 200, f"extract 失败: {r.status_code} {r.text[:300]}"
        filename = r.json()["filename"]
        print(f"[1] 课件上传并提取 OK: {filename}，{r.json()['total_pages']} 页，{r.json()['image_count']} 图")

        # 2) 解析试题（文本）
        exam_text = exam_path.read_text(encoding="utf-8")
        r = c.post("/api/exam", data={"text": exam_text})
        assert r.status_code == 200, f"exam(文本) 失败: {r.status_code} {r.text[:300]}"
        d = r.json()
        exam_id = d["exam_id"]
        print(f"[2] 试题解析 OK: exam_id={exam_id}，{d['chars']} 字符，预览: {d['preview'][:60]}…")

        # 3) 扫描版 PDF 应被友好拒绝
        from io import BytesIO

        from PIL import Image

        pdf_io = BytesIO()
        Image.new("RGB", (500, 300), "white").save(pdf_io, format="PDF")
        r = c.post("/api/exam", files={"file": ("scan.pdf", pdf_io.getvalue(), "application/pdf")})
        assert r.status_code == 400, f"扫描版PDF 应报400，实际 {r.status_code}: {r.text[:200]}"
        print(f"[3] 扫描版 PDF 友好报错 OK: {r.json()['detail'][:60]}…")

        # 4) 生成（指定模式 + 视觉）
        r = c.post("/api/generate", json={
            "filename": filename,
            "template": "复习指南",
            "style": "详细",
            "use_vision": True,
            "exam_id": exam_id,
            "exam_mode": exam_mode,
        })
        assert r.status_code == 200, f"generate 失败: {r.status_code} {r.text[:400]}"
        data = r.json()
        md = data["markdown"]
        out = ROOT / "eval" / f"e2e_{MODE_TAG}_{ppt_path.stem}.md"
        out.write_text(md, encoding="utf-8")
        print(f"[4] 生成 OK: {len(md)} 字符，视觉图 {data['image_count']} 张，截断={data.get('truncated')} → {out.name}")
        if exam_mode == "direct":
            print(f"    含真题章节: {'历年真题' in md}")
        else:
            print(f"    含预测题章节: {'预测题' in md}")
            print(f"    章节注明非原题: {'非原题' in md or '非真题' in md or '模拟' in md}")


if __name__ == "__main__":
    main()
