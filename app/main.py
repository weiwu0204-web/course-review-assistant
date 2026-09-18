"""PPT 复习助手：FastAPI 应用入口。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.batch_routes import router as batch_router
from app.api.routes import router
from app.api.settings_routes import router as settings_router

app = FastAPI(
    title="PPT 复习助手",
    description="上传老师 PPT，自动提取内容并用 AI 生成期末复习文档",
    version="0.8.12",
)

app.include_router(router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(batch_router, prefix="/api")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")