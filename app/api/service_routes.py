"""HTTP entrypoint for the service. Desktop settings and local file routes are separate."""

from contextlib import asynccontextmanager
import logging
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.courses.service import ReviewService, ServiceError
from app.infrastructure.object_store import ObjectStoreError

logger = logging.getLogger(__name__)
Id = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Nonnegative = Annotated[int, Field(ge=0, lt=2**31 - 1)]


class Login(BaseModel):
    username: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class CourseInput(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]


class BatchInput(BaseModel):
    materials: Annotated[list[Id], Field(min_length=1, max_length=20)]
    exam: Id | None = None
    exam_mode: Literal["direct", "predict"] = "direct"
    template: Literal["复习指南", "重点总结", "习题生成"] = "复习指南"
    style: Literal["精简", "详细", "表格化"] = "详细"
    use_vision: bool = False

    @field_validator("materials")
    @classmethod
    def unique_materials(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("同一课件不可重复提交")
        return value


def create_service_app(service: ReviewService, *, dispose_on_shutdown: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            if dispose_on_shutdown:
                service.db.close()

    app = FastAPI(title="PPT 复习助手 · 服务版", lifespan=lifespan)
    api = APIRouter(prefix="/api/v1")
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def reject_large_requests(request, call_next):
        # Uvicorn is also used directly during preview. A reverse proxy should enforce
        # its own request-size and transfer-time limits before a public rollout.
        length = request.headers.get("content-length")
        if request.method == "POST" and request.headers.get("content-type", "").startswith("multipart/form-data"):
            if length is None:
                return JSONResponse({"detail": "上传需要 Content-Length"}, status_code=411)
            if not length.isdecimal() or int(length) > service.upload_limit + 64 * 1024:
                return JSONResponse({"detail": "上传大小超限"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    def service_error(_, exc: ServiceError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers=headers)

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(ObjectStoreError)
    @app.exception_handler(BotoCoreError)
    def dependency_error(request, exc):
        route = getattr(request.scope.get("route"), "path", "<unknown>")
        logger.warning("Dependency unavailable: %s %s (%s)", request.method, route, type(exc).__name__)
        return JSONResponse({"detail": "服务存储暂不可用"}, status_code=503)

    def current_owner(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        return service.owner(creds.credentials if creds else None)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        if not service.db.ready() or not service.objects.ready():
            raise ServiceError(503, "服务存储尚未就绪")
        return {"status": "ready"}

    @api.post("/sessions", status_code=201)
    def login(body: Login, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"token": service.login(body.username, body.password),
                "expires_in": service.session_ttl, "token_type": "bearer"}

    @api.delete("/sessions/current", status_code=204)
    def logout(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
               _owner: str = Depends(current_owner)):
        service.logout(creds.credentials)
        return Response(status_code=204)

    @api.post("/courses", status_code=201)
    def create_course(body: CourseInput, owner: str = Depends(current_owner)):
        return service.create_course(owner, body.title)

    @api.get("/courses")
    def list_courses(owner: str = Depends(current_owner)):
        return service.list_courses(owner)

    @api.get("/courses/{course}/materials")
    def list_materials(course: Id, owner: str = Depends(current_owner)):
        return service.list_materials(owner, course)

    @api.post("/courses/{course}/materials", status_code=201)
    def upload_material(
        course: Id, file: Annotated[UploadFile, File()],
        base_revision: Annotated[Nonnegative, Form()] = 0,
        kind: Annotated[Literal["courseware", "exam"], Form()] = "courseware",
        owner: str = Depends(current_owner),
    ):
        return service.publish(owner, course, material=None, kind=kind, base=base_revision,
                               filename=file.filename or "", source=file.file, size=file.size or 0)

    @api.post("/courses/{course}/materials/{material}/revisions", status_code=201)
    def publish(
        course: Id, material: Id, file: Annotated[UploadFile, File()],
        base_revision: Annotated[Nonnegative, Form()],
        owner: str = Depends(current_owner),
    ):
        return service.publish(owner, course, material=material, kind=None, base=base_revision,
                               filename=file.filename or "", source=file.file, size=file.size or 0)

    @api.get("/courses/{course}/materials/{material}/revisions/{number}")
    def revision(course: Id, material: Id, number: Nonnegative, owner: str = Depends(current_owner)):
        data = service.revision(owner, course, material, number)
        return {k: data[k] for k in ("id", "material_id", "number", "name", "bytes")}

    @api.post("/courses/{course}/batches", status_code=202)
    def submit(
        course: Id, body: BatchInput,
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
        owner: str = Depends(current_owner),
    ):
        return service.submit(
            owner, course, key, body.materials, body.exam, body.model_dump(exclude={"materials", "exam"}),
        )

    @api.get("/batches/{batch_id}")
    def get_batch(batch_id: Id, owner: str = Depends(current_owner)):
        return service.batch(owner, batch_id)

    @api.post("/batches/{batch_id}/retry", status_code=202)
    def retry_batch(batch_id: Id, owner: str = Depends(current_owner)):
        return service.retry(owner, batch_id)

    @api.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: Id, owner: str = Depends(current_owner)):
        return service.cancel(owner, batch_id)

    @api.get("/batches/{batch_id}/download")
    def download(batch_id: Id, owner: str = Depends(current_owner)):
        return Response(service.download(owner, batch_id), media_type="text/markdown",
                        headers={"Content-Disposition": 'attachment; filename="review.md"',
                                 "Cache-Control": "private, no-store"})

    app.include_router(api)
    return app
