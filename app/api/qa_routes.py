"""P3 问答路由：建课程知识库 + SSE 流式问答。

## 为什么建库和问答是两个端点

建库要提取整门课的课件（39 份 PDF 实测 7.3 秒），是一次性成本；
问答要反复调用。**分成两步之后，建一次就能问很多次**——
如果合成一个端点，每次提问都要重付 7 秒。

`kb_id` 由「文件路径 + 大小 + mtime」哈希而来，同一批文件重复建库会命中缓存
（见 `services/kb.py`），所以前端可以放心每次进页面都调一次 `/kb`。

## 为什么问答用 POST + SSE，而不是 GET + EventSource

浏览器原生的 `EventSource` **只支持 GET**，而问答参数（kb_id、question、
topk、scope）塞进 query string 既难看又有长度限制。
所以用 `fetch` + `ReadableStream` 手工解析 SSE 帧（前端约 30 行），
好处是支持 POST、可带 JSON body、可中止（AbortController）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.api.routes import UPLOAD_DIR, _cleanup_stale_uploads
from app.models import AskRequest, KbBuildResponse
from app.services import kb as kb_service
from app.services.extractor import SUPPORTED_EXTENSIONS
from app.services.qa import answer_stream, encode_sse, load_qa_log

router = APIRouter()

#: 一次建库的文件数上限。实测一门课 39 份 / 21MB，留一倍余量。
MAX_KB_FILES = 80

#: SSE 必须关掉各层缓冲，否则事件会被攒成一坨再发，"流式"就名存实亡。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 反向代理（如 nginx）默认会缓冲 text/event-stream，这一条让它别缓冲
    "X-Accel-Buffering": "no",
}


@router.post("/kb", response_model=KbBuildResponse)
async def build_knowledge_base(
    files: list[UploadFile] = File(...),
    course_name: str = Form(""),
) -> KbBuildResponse:
    """上传一门课的课件，建成知识库，返回 kb_id 供 /ask 使用。

    ## 为什么这里自己接收上传，而不是复用别处的文件

    单份模式的 `/extract` 会把课件留在 `uploads/`，但**批量模式的 `/batch/peek`
    用的是临时目录、提取完就删**——前端在批量 tab 里选好的文件在服务端并不存在。
    所以建库接口只能自己收一次上传。

    **只上传这一次**：之后所有问答都只传 `kb_id`，靠 `output/kb/<kb_id>/` 的缓存服务。

    ## 知识库建在课件原文上

    与"有没有生成过复习文档"无关——只提问不生成也是正常用法，
    而且用户不必为了问几个问题先等一份文档生成完。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到课件文件")
    if len(files) > MAX_KB_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多 {MAX_KB_FILES} 份课件（收到 {len(files)} 份），请分批建库",
        )

    # 与单份上传同一套清理（复用 TTL 设置，避免 uploads/ 长期积压）
    _cleanup_stale_uploads()

    upload_id = uuid.uuid4().hex[:8]
    dest_dir = UPLOAD_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    rejected: list[str] = []
    for f in files:
        name = Path(f.filename or "unknown.pptx").name
        if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            rejected.append(f"{name}（不支持的格式，只收 .pptx / .pdf）")
            continue
        dest = dest_dir / name
        dest.write_bytes(await f.read())
        paths.append(dest)

    if not paths:
        detail = "；".join(rejected[:3]) if rejected else "没有可用的课件文件"
        raise HTTPException(status_code=400, detail=detail)

    try:
        kb = kb_service.build(paths, course_name=course_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    failures = kb.failures + rejected
    return KbBuildResponse(**kb.stats(), failures=failures)


@router.get("/kb/{kb_id}")
def kb_info(kb_id: str) -> dict:
    """查一个知识库的缓存状态（前端可用于显示"上次建的库还在"）。"""
    kb = kb_service.load(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在或已过期，请重新建库")
    return kb.stats()


@router.get("/kb/{kb_id}/qa")
def qa_history(kb_id: str, limit: int = 50) -> dict:
    """这门课问过的问题（倒序，最新在前）。

    **刻意不检查知识库缓存是否还在**：历史落在 `output/qa/`，与 `output/kb/` 的
    72 小时缓存是两回事。用户可能过了一周才回来翻"上次问了什么"，
    这时知识库缓存早已过期，但历史必须还在——所以这里只读日志，不要求缓存存在。

    这也意味着前端拿到历史后若要"重问"，得先触发一次重建（`/api/kb`），
    那是前端的责任，不是这里的。
    """
    records = load_qa_log(kb_id, limit=max(1, min(limit, 200)))
    return {"kb_id": kb_id, "count": len(records), "records": records}


@router.post("/ask")
def ask(request: AskRequest) -> StreamingResponse:
    """流式问答。返回 `text/event-stream`，事件类型见 services/qa.py。

    **错误也走事件流**（`{"type":"error"}`）而不是 HTTP 状态码——
    一旦开始输出，状态码就改不了了；而流式过程中出错的概率不低
    （限流、超时、余额不足），必须让前端有个统一的处理路径。
    """
    kb = kb_service.load(request.kb_id)
    if kb is None:
        raise HTTPException(
            status_code=404,
            detail="知识库不存在或已过期（缓存保留 72 小时），请重新建库",
        )

    scope = request.scope or None
    if scope and scope not in kb.files:
        raise HTTPException(
            status_code=400,
            detail=f"指定的课件不在此知识库里：{scope}",
        )

    def generate():
        try:
            for event in answer_stream(
                request.question,
                kb,
                topk=max(1, min(request.topk, 20)),
                scope=scope,
                use_rewrite=request.use_rewrite,
            ):
                yield encode_sse(event)
        except Exception as exc:  # noqa: BLE001 兜底：绝不让连接挂死在半路
            yield encode_sse(
                {"type": "error", "message": f"服务内部错误：{type(exc).__name__}: {exc}"}
            )

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_HEADERS)
