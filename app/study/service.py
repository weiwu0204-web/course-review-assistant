"""Private knowledge base snapshots and Q&A history for the service."""

from collections import Counter
from io import BytesIO
import json
import logging
from pathlib import Path
from shutil import copyfileobj
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.courses.service import ReviewService, ServiceError, missing, one
from app.infrastructure.object_store import ObjectStoreError
from app.services.extractor import ExtractionError, extract_any
from app.services.kb import _deserialize, _serialize, from_extracted
from app.services.qa import answer_stream
from migrations.v0001 import batches, materials
from migrations.v0002 import answers, knowledge_bases

logger = logging.getLogger(__name__)
MAX_KB_INPUT = 100 * 1024**2
MAX_KB_JSON = 16 * 1024**2


class StudyService:
    def __init__(self, review: ReviewService, settings: Settings | None = None):
        self.review = review
        self.db = review.db
        self.objects = review.objects
        self.settings = settings

    @staticmethod
    def _public(row):
        return {k: row[k] for k in ("id", "course_id", "sources", "counts", "created_at")}

    def list_bases(self, owner: str, course: str) -> list[dict]:
        with self.db.engine.connect() as conn:
            self.review._course(conn, owner, course)
            rows = conn.execute(select(knowledge_bases).where(
                knowledge_bases.c.owner_id == owner, knowledge_bases.c.course_id == course,
            ).order_by(knowledge_bases.c.created_at.desc(), knowledge_bases.c.id.desc()).limit(50)).mappings()
            return [self._public(row) for row in rows]

    def create(self, owner: str, course: str, material_ids: list[str]) -> dict:
        with self.db.engine.connect() as conn:
            course_record = self.review._course(conn, owner, course)
            references_query = self.review._current_revisions(course).where(
                materials.c.id.in_(material_ids), materials.c.kind == "courseware",
            )
            rows = {row["id"]: row for row in conn.execute(references_query).mappings()}
        if len(rows) != len(material_ids):
            raise missing()
        entries = [rows[item] for item in material_ids]
        if sum(row["bytes"] for row in entries) > MAX_KB_INPUT:
            raise ServiceError(413, "建库课件总大小超限")
        kb_id = uuid4().hex
        names = Counter(row["name"] for row in entries)
        used_names = set()
        references = []
        extracted = []
        failures = []
        with TemporaryDirectory(prefix="ppt-study-") as temp:
            root = Path(temp)
            for n, row in enumerate(entries, 1):
                suffix = Path(row["name"]).suffix.lower()
                filename = f"{n}{suffix}"
                display = f"{n}. {row['name']}" if names[row["name"]] > 1 else row["name"]
                while display in used_names:
                    display = f"{n}. {display}"
                used_names.add(display)
                path = root / filename
                with self.objects.open(row["object_key"]) as original, path.open("wb") as target:
                    copyfileobj(original, target, length=1024 * 1024)
                try:
                    result = extract_any(path, image_dir=root / f"images-{n}")
                except ExtractionError:
                    failures.append(f"{display}：无法提取文字")
                    continue
                extracted.append(result.model_copy(update={"filename": display}))
                references.append({"material_id": row["id"], "revision": row["revision"], "name": display})
        try:
            kb = from_extracted(kb_id, course_record["title"], extracted, failures)
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from None
        raw = json.dumps(_serialize(kb), ensure_ascii=False).encode()
        if len(raw) > MAX_KB_JSON:
            raise ServiceError(413, "建库文本量超限")
        object_key = f"knowledge/{kb_id}"
        self.objects.put(object_key, BytesIO(raw), len(raw))
        with self.db.engine.begin() as conn:
            row = {
                "id": kb_id, "course_id": course, "owner_id": owner, "object_key": object_key,
                "sources": references, "counts": {"files": kb.n_files, "pages": kb.n_pages,
                                                  "failures": failures},
                "created_at": self.db.now(conn),
            }
            conn.execute(insert(knowledge_bases).values(**row))
        return self._public(row)

    def _base(self, owner: str, kb_id: str):
        with self.db.engine.connect() as conn:
            row = one(conn, select(knowledge_bases).where(
                knowledge_bases.c.id == kb_id, knowledge_bases.c.owner_id == owner,
            ))
        if not row:
            raise missing()
        return row

    def detail(self, owner: str, kb_id: str) -> dict:
        return self._public(self._base(owner, kb_id))

    def load(self, owner: str, kb_id: str):
        row = self._base(owner, kb_id)
        try:
            with self.objects.open(row["object_key"]) as source:
                raw = source.read(MAX_KB_JSON + 1)
                if len(raw) > MAX_KB_JSON:
                    raise ValueError("index too large")
                return _deserialize(json.loads(raw))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ObjectStoreError("could not load study index") from exc

    def ask(self, kb, question: str, topk: int, scope: str | None):
        def remember(kb_id: str, record: dict):
            try:
                with self.db.engine.begin() as conn:
                    conn.execute(insert(answers).values(
                        id=uuid4().hex, kb_id=kb_id, created_at=self.db.now(conn), record=record,
                    ))
            except SQLAlchemyError:
                logger.warning("Could not persist study history for %s", kb_id)

        return answer_stream(question, kb, topk=topk, scope=scope, settings_override=self.settings,
                             history_sink=remember, demo=self.settings is None)

    def history(self, owner: str, kb_id: str, limit: int) -> list[dict]:
        self._base(owner, kb_id)
        with self.db.engine.connect() as conn:
            return conn.scalars(select(answers.c.record).where(answers.c.kb_id == kb_id)
                                .order_by(answers.c.created_at.desc(), answers.c.id.desc()).limit(limit)).all()

    def stats(self, owner: str) -> dict:
        with self.db.engine.connect() as conn:
            count = conn.scalar(select(func.count()).select_from(answers.join(knowledge_bases))
                                .where(knowledge_bases.c.owner_id == owner))
            return {
                "qa_count": count,
                "batch_count": conn.scalar(select(func.count()).select_from(batches)
                                           .where(batches.c.owner_id == owner)),
                "kb_count": conn.scalar(select(func.count()).select_from(knowledge_bases)
                                        .where(knowledge_bases.c.owner_id == owner)),
            }
