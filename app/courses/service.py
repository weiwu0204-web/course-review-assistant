"""Course ownership, immutable revisions and batch submission."""

import hashlib
import json
import logging
from pathlib import PurePosixPath
import secrets
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import Database, password_hash, verify_password
from app.infrastructure.object_store import Objects, ObjectStoreError
from migrations.v0001 import batches, courses, jobs, materials, revisions, sessions, users
from migrations.v0002 import generation_details

logger = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(detail)


def missing() -> ServiceError:
    return ServiceError(404, "资源不存在")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def one(conn, query):
    return conn.execute(query).mappings().first()


class ReviewService:
    def __init__(self, db: Database, objects: Objects, *, upload_limit: int = 50 * 1024**2,
                 session_ttl: int = 8 * 60 * 60):
        self.db = db
        self.objects = objects
        self.upload_limit = upload_limit
        self.session_ttl = session_ttl
        # Run the same password function for missing usernames.
        self._dummy_password = password_hash(secrets.token_urlsafe(32))

    def login(self, username: str, password: str) -> str:
        with self.db.engine.connect() as conn:
            account = one(conn, select(users).where(users.c.username == username.lower()))
        valid = verify_password(password, account["password_hash"] if account else self._dummy_password)
        if not valid or not account:
            raise ServiceError(401, "账号或密码错误")
        token = secrets.token_urlsafe(32)
        with self.db.engine.begin() as conn:
            conn.execute(insert(sessions).values(
                digest=digest(token), owner_id=account["id"], expires_at=self.db.now(conn) + self.session_ttl,
            ))
        return token

    def owner(self, token: str | None) -> str:
        if not token or len(token) > 128:
            raise ServiceError(401, "请登录")
        with self.db.engine.connect() as conn:
            row = one(conn, select(sessions.c.owner_id).where(
                sessions.c.digest == digest(token), sessions.c.expires_at > self.db.now(conn),
            ))
        if not row:
            raise ServiceError(401, "会话不存在或已过期")
        return row["owner_id"]

    def logout(self, token: str) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(sessions.delete().where(sessions.c.digest == digest(token)))

    def create_course(self, owner: str, title: str) -> dict:
        title = title.strip()
        if not title:
            raise ServiceError(422, "课程名不能为空")
        item = {"id": uuid4().hex, "title": title}
        with self.db.engine.begin() as conn:
            conn.execute(insert(courses).values(owner_id=owner, **item))
        return item

    def list_courses(self, owner: str) -> list[dict]:
        with self.db.engine.connect() as conn:
            return [dict(row) for row in conn.execute(
                select(courses.c.id, courses.c.title).where(courses.c.owner_id == owner)
                .order_by(courses.c.title, courses.c.id)
            ).mappings()]

    @staticmethod
    def _course(conn, owner: str, course: str):
        row = one(conn, select(courses).where(courses.c.id == course, courses.c.owner_id == owner))
        if not row:
            raise missing()
        return row

    @staticmethod
    def _current_revisions(course: str):
        return select(
            materials.c.id, materials.c.kind, materials.c.revision,
            revisions.c.id.label("revision_id"), revisions.c.name,
            revisions.c.object_key, revisions.c.bytes,
        ).join(revisions, and_(revisions.c.material_id == materials.c.id,
                              revisions.c.number == materials.c.revision)).where(materials.c.course_id == course)

    def list_materials(self, owner: str, course: str) -> list[dict]:
        with self.db.engine.connect() as conn:
            self._course(conn, owner, course)
            return [{k: row[k] for k in ("id", "kind", "revision", "revision_id", "name")} for row in conn.execute(
                self._current_revisions(course).order_by(revisions.c.name, materials.c.id)
            ).mappings()]

    def list_batches(self, owner: str, limit: int) -> list[dict]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(batches.c.id, batches.c.course_title, batches.c.course_id,
                                       batches.c.created_at).where(batches.c.owner_id == owner)
                                .order_by(batches.c.created_at.desc(), batches.c.id.desc()).limit(limit))
            return [dict(row) for row in rows.mappings()]

    def publish(self, owner: str, course: str, *, material: str | None, kind: str | None,
                base: int, filename: str, source: BinaryIO, size: int) -> dict:
        material_id = material or uuid4().hex
        with self.db.engine.connect() as conn:
            self._course(conn, owner, course)
            if material:
                current = one(conn, select(materials).where(
                    materials.c.id == material, materials.c.course_id == course,
                ))
                if not current:
                    raise missing()
                if current["revision"] != base:
                    raise ServiceError(409, "课件已有新版本，请刷新后重试")
                kind = current["kind"]
            elif base != 0:
                raise ServiceError(409, "新资料的基准版本必须为 0")
        filename = PurePosixPath(filename.replace("\\", "/")).name
        filetype = PurePosixPath(filename).suffix.lower()
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ServiceError(422, "无效的文件名")
        extensions = {".pptx", ".pdf"} if kind == "courseware" else {".txt", ".md", ".pdf"}
        if filetype not in extensions:
            raise ServiceError(415, "不支持该文件格式")
        if size <= 0:
            raise ServiceError(422, "文件为空")
        if size > self.upload_limit:
            raise ServiceError(413, "文件大小超限")
        revision_id = uuid4().hex
        object_key = f"materials/{material_id}/{revision_id}"
        self.objects.put(object_key, source, size)
        number = base + 1
        try:
            with self.db.engine.begin() as conn:
                if material:
                    result = conn.execute(update(materials).where(
                        materials.c.id == material, materials.c.course_id == course, materials.c.revision == base,
                    ).values(revision=number))
                    if result.rowcount != 1:
                        raise ServiceError(409, "课件已有新版本，请刷新后重试")
                else:
                    conn.execute(insert(materials).values(
                        id=material_id, course_id=course, kind=kind, revision=number,
                    ))
                conn.execute(insert(revisions).values(
                    id=revision_id, material_id=material_id, number=number,
                    name=filename, object_key=object_key, bytes=size,
                ))
        except ServiceError:
            # A rejected CAS is known not to have published this object.
            # Retain on a DB/connection error, when the commit could be ambiguous.
            try:
                self.objects.remove(object_key)
            except ObjectStoreError:
                logger.warning("Could not clean rejected revision %s", revision_id)
            raise
        return {"id": material_id, "kind": kind, "revision": number, "revision_id": revision_id, "name": filename}

    def revision(self, owner: str, course: str, material: str, number: int):
        with self.db.engine.connect() as conn:
            self._course(conn, owner, course)
            row = one(conn, select(revisions).join(materials).where(
                materials.c.course_id == course, materials.c.id == material, revisions.c.number == number,
            ))
        if not row:
            raise missing()
        return row

    @staticmethod
    def _existing(conn, owner: str, key: str, fingerprint: str):
        row = one(conn, select(batches.c.id, batches.c.request_hash).where(
            batches.c.owner_id == owner, batches.c.request_key == key,
        ))
        if row and row["request_hash"] != fingerprint:
            raise ServiceError(409, "该幂等键已用于其他请求")
        return row["id"] if row else None

    def submit(self, owner: str, course: str, key: str, material_ids: list[str],
               exam_id: str | None, options: dict) -> dict:
        key = digest(key)
        if options.get("enable_repair") is False:
            # Default behavior has the same request identity as preview 001.
            options = {k: v for k, v in options.items() if k != "enable_repair"}
        fingerprint = digest(json.dumps([course, material_ids, exam_id, options], sort_keys=True))
        batch_id = uuid4().hex
        try:
            with self.db.engine.begin() as conn:
                existing = self._existing(conn, owner, key, fingerprint)
                if existing:
                    batch_id = existing
                else:
                    data = self._course(conn, owner, course)
                    ids = material_ids + ([exam_id] if exam_id else [])
                    inputs = {row["id"]: row for row in conn.execute(
                        self._current_revisions(course).where(materials.c.id.in_(ids))
                    ).mappings()}
                    if len(inputs) != len(ids) or any(inputs[x]["kind"] != "courseware" for x in material_ids):
                        raise missing()
                    if exam_id and inputs[exam_id]["kind"] != "exam":
                        raise missing()
                    conn.execute(insert(batches).values(
                        id=batch_id, owner_id=owner, course_id=course, course_title=data["title"],
                        request_key=key, request_hash=fingerprint, options=options, created_at=self.db.now(conn),
                        exam_revision_id=inputs[exam_id]["revision_id"] if exam_id else None,
                    ))
                    conn.execute(insert(jobs), [
                        {"id": uuid4().hex, "batch_id": batch_id, "position": n,
                         "revision_id": inputs[x]["revision_id"], "status": "queued"}
                        for n, x in enumerate(material_ids, 1)
                    ])
        except IntegrityError:
            # Concurrent replays can race the unique (owner, request_key) constraint.
            with self.db.engine.connect() as conn:
                existing = self._existing(conn, owner, key, fingerprint)
            if not existing:
                raise
            batch_id = existing
        return self.batch(owner, batch_id)

    def _batch_rows(self, owner: str, batch_id: str):
        with self.db.engine.connect() as conn:
            item = one(conn, select(batches).where(batches.c.owner_id == owner, batches.c.id == batch_id))
            if item is None:
                raise missing()
            rows = conn.execute(select(
                jobs.c.id, jobs.c.position, jobs.c.status, jobs.c.attempts, jobs.c.error, jobs.c.result_key,
                jobs.c.pages, jobs.c.truncated, revisions.c.number.label("revision"), revisions.c.name,
                generation_details.c.detail.label("diagnostics"),
            ).join(revisions, jobs.c.revision_id == revisions.c.id).outerjoin(
                generation_details, generation_details.c.job_id == jobs.c.id,
            ).where(
                jobs.c.batch_id == batch_id,
            ).order_by(jobs.c.position)).mappings().all()
        return item, rows

    def batch(self, owner: str, batch_id: str) -> dict:
        item, rows = self._batch_rows(owner, batch_id)
        states = {row["status"] for row in rows}
        if states & {"queued", "running"}:
            state = "running" if states != {"queued"} else "queued"
        elif states == {"done"}:
            state = "done"
        elif "cancelled" in states:
            state = "cancelled"
        elif "done" in states:
            state = "partial"
        else:
            state = "failed"
        return {
            "id": item["id"], "course_id": item["course_id"], "status": state,
            "jobs": [{key: row[key] for key in ("id", "position", "status", "attempts", "error",
                                                "revision", "name", "diagnostics")}
                     for row in rows],
        }

    def retry(self, owner: str, batch_id: str) -> dict:
        with self.db.engine.begin() as conn:
            if not one(conn, select(batches.c.id).where(batches.c.owner_id == owner, batches.c.id == batch_id)):
                raise missing()
            conn.execute(update(jobs).where(jobs.c.batch_id == batch_id, jobs.c.status == "failed").values(
                status="queued", claims=0, error=None,
            ))
        return self.batch(owner, batch_id)

    def cancel(self, owner: str, batch_id: str) -> dict:
        with self.db.engine.begin() as conn:
            if not one(conn, select(batches.c.id).where(batches.c.owner_id == owner, batches.c.id == batch_id)):
                raise missing()
            conn.execute(update(jobs).where(
                jobs.c.batch_id == batch_id, jobs.c.status.in_(("queued", "running", "failed")),
            ).values(status="cancelled", lease_token=None, lease_until=None, error=None))
        return self.batch(owner, batch_id)

    def download(self, owner: str, batch_id: str) -> str:
        from app.services.reducer import build_master_doc, extract_headings

        item, rows = self._batch_rows(owner, batch_id)
        if any(row["status"] in {"queued", "running"} for row in rows):
            raise ServiceError(409, "任务尚未结束")
        sections = []
        for row in rows:
            if row["status"] != "done":
                continue
            with self.objects.open(row["result_key"]) as source:
                body = source.read().decode("utf-8")
            headings = extract_headings(body)
            sections.append({
                "idx": row["position"], "title": headings[0] if headings else row["name"], "source": row["name"],
                "pages": row["pages"], "warning": bool(row["truncated"]), "body": body,
            })
        if not sections:
            raise ServiceError(409, "尚无可下载的章节")
        return build_master_doc(item["course_title"], sections)
