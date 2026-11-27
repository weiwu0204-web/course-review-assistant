"""HTTP entrypoint for the service. Desktop settings and local file routes are separate."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.api import study_routes
from app.api.study_routes import MaterialsInput
from app.api.types import Id
from app.courses.service import ReviewService, ServiceError
from app.infrastructure.object_store import ObjectStoreError
from app.study.service import StudyService

logger = logging.getLogger(__name__)
Nonnegative = Annotated[int, Field(ge=0, lt=2**31 - 1)]
SERVICE_STATIC = Path(__file__).resolve().parents[1] / "static" / "service"


class Login(BaseModel):
    username: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class CourseInput(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]


class BatchInput(MaterialsInput):
    exam: Id | None = None
    exam_mode: Literal["direct", "predict"] = "direct"
    template: Literal["复习指南", "重点总结", "习题生成"] = "复习指南"
    style: Literal["精简", "详细", "表格化"] = "详细"
    use_vision: bool = False
    enable_repair: bool = False


def create_service_app(service: ReviewService, *, study: StudyService | None = None,
                       dispose_on_shutdown: bool = False, repair_allowed: bool = True) -> FastAPI:
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
    async def request_rules(request, call_next):
        # Uvicorn is also used directly during preview. A reverse proxy should enforce
        # its own request-size and transfer-time limits before a public rollout.
        length = request.headers.get("content-length")
        content_type = request.headers.get("content-type", "").lower()
        if request.method == "POST" and content_type.startswith("multipart/form-data"):
            if length is None:
                return JSONResponse({"detail": "上传需要 Content-Length"}, status_code=411)
            if not length.isdecimal() or int(length) > service.upload_limit + 64 * 1024:
                return JSONResponse({"detail": "上传大小超限"}, status_code=413)
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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

    @app.get("/", include_in_schema=False)
    def workspace():
        return FileResponse(SERVICE_STATIC / "index.html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; img-src 'self'; form-action 'self'; "
                                       "base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

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

    @api.get("/capabilities")
    def capabilities(_owner: str = Depends(current_owner)):
        return {"auto_repair": repair_allowed}

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
        if body.enable_repair and not repair_allowed:
            raise ServiceError(422, "当前服务未启用自动补漏")
        return service.submit(
            owner, course, key, body.materials, body.exam, body.model_dump(exclude={"materials", "exam"}),
        )

    @api.get("/batches")
    def list_batches(limit: Annotated[int, Query(ge=1, le=100)] = 20, owner: str = Depends(current_owner)):
        return service.list_batches(owner, limit)

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

    study_routes.register(api, study or StudyService(service), current_owner)
    app.include_router(api)
    app.mount("/assets", StaticFiles(directory=SERVICE_STATIC), name="service-assets")
    return app
