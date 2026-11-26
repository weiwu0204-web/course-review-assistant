"""Authenticated knowledge base and SSE routes for the service."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.api.types import Id
from app.courses.service import ServiceError
from app.services.qa import encode_sse
from app.study.service import StudyService

logger = logging.getLogger(__name__)


class MaterialsInput(BaseModel):
    materials: Annotated[list[Id], Field(min_length=1, max_length=20)]

    @field_validator("materials")
    @classmethod
    def unique_materials(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("同一课件不可重复提交")
        return value


class Ask(BaseModel):
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    topk: Annotated[int, Field(ge=1, le=20)] = 5
    scope: Annotated[str, Field(max_length=280)] | None = None


def register(router: APIRouter, study: StudyService, current_owner) -> None:
    @router.post("/courses/{course}/knowledge-bases", status_code=201, tags=["study"])
    def build(course: Id, body: MaterialsInput, owner: str = Depends(current_owner)):
        return study.create(owner, course, body.materials)

    @router.get("/courses/{course}/knowledge-bases", tags=["study"])
    def list_bases(course: Id, owner: str = Depends(current_owner)):
        return study.list_bases(owner, course)

    @router.get("/knowledge-bases/{kb_id}", tags=["study"])
    def detail(kb_id: Id, owner: str = Depends(current_owner)):
        return study.detail(owner, kb_id)

    @router.get("/knowledge-bases/{kb_id}/history", tags=["study"])
    def history(kb_id: Id, limit: Annotated[int, Query(ge=1, le=100)] = 25, owner: str = Depends(current_owner)):
        return study.history(owner, kb_id, limit)

    @router.post("/knowledge-bases/{kb_id}/ask", tags=["study"])
    def ask(kb_id: Id, body: Ask, owner: str = Depends(current_owner)):
        question = body.question.strip()
        if not question:
            raise ServiceError(422, "问题不能为空")
        kb = study.load(owner, kb_id)
        if body.scope and body.scope not in kb.files:
            raise ServiceError(422, "课件不在当前知识库内")

        def stream():
            try:
                for event in study.ask(kb, question, body.topk, body.scope):
                    yield encode_sse(event)
            except Exception as exc:
                logger.warning("Study stream failed (%s)", type(exc).__name__)
                yield encode_sse({"type": "error", "message": "服务暂不可用，请稍后重试"})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.get("/stats", tags=["study"])
    def stats(owner: str = Depends(current_owner)):
        return study.stats(owner)
