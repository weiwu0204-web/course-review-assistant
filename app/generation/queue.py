"""Job ownership and lease transitions. No extraction or model I/O in a transaction."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select, update

from app.infrastructure.database import Database
from migrations.v0001 import batches, jobs, revisions


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    token: str
    source_key: str
    filename: str
    exam_key: str | None
    exam_name: str | None
    options: dict


class JobQueue:
    def __init__(self, db: Database, *, lease_seconds: int = 60, max_claims: int = 3):
        if lease_seconds < 3 or max_claims < 1:
            raise ValueError("lease_seconds must be at least 3, max_claims positive")
        self.db = db
        self.lease_seconds = lease_seconds
        self.max_claims = max_claims

    @staticmethod
    def _active(claim: Claim, now: int):
        return and_(jobs.c.id == claim.id, jobs.c.status == "running",
                    jobs.c.lease_token == claim.token, jobs.c.lease_until > now)

    def claim(self) -> Claim | None:
        with self.db.engine.begin() as conn:
            if self.db.sqlite:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            now = self.db.now(conn)
            previous = jobs.alias("previous")
            expired = and_(jobs.c.status == "running", jobs.c.lease_until <= now)
            candidate = (
                select(jobs.c.id, jobs.c.batch_id, jobs.c.revision_id, jobs.c.attempts, jobs.c.claims)
                .where(or_(jobs.c.status == "queued", expired),
                       ~exists(select(1).select_from(previous).where(
                           previous.c.batch_id == jobs.c.batch_id, previous.c.position < jobs.c.position,
                           previous.c.status.in_(("queued", "running")),
                       )))
                .order_by(jobs.c.batch_id, jobs.c.position).limit(1)
            )
            if not self.db.sqlite:
                candidate = candidate.with_for_update(skip_locked=True)
            while (row := conn.execute(candidate).mappings().first()) is not None:
                if row["claims"] >= self.max_claims:
                    conn.execute(update(jobs).where(jobs.c.id == row["id"]).values(
                        status="failed", lease_token=None, lease_until=None, error="lease_exhausted",
                    ))
                    continue
                token = uuid4().hex
                conn.execute(update(jobs).where(jobs.c.id == row["id"]).values(
                    status="running", lease_token=token, lease_until=now + self.lease_seconds,
                    attempts=row["attempts"] + 1, claims=row["claims"] + 1, error=None,
                ))
                source = conn.execute(select(revisions.c.name, revisions.c.object_key).where(
                    revisions.c.id == row["revision_id"],
                )).one()
                batch = conn.execute(select(batches.c.options, batches.c.exam_revision_id).where(
                    batches.c.id == row["batch_id"],
                )).one()
                exam = conn.execute(select(revisions.c.name, revisions.c.object_key).where(
                    revisions.c.id == batch.exam_revision_id,
                )).first() if batch.exam_revision_id else None
                return Claim(row["id"], token, source.object_key, source.name,
                             exam.object_key if exam else None, exam.name if exam else None, batch.options)
            return None

    def renew(self, claim: Claim) -> bool:
        with self.db.engine.begin() as conn:
            now = self.db.clock()
            result = conn.execute(update(jobs).where(self._active(claim, now)).values(
                lease_until=now + self.lease_seconds,
            ))
            return result.rowcount == 1

    def finish(self, claim: Claim, *, result_key: str, pages: int, truncated: bool) -> bool:
        with self.db.engine.begin() as conn:
            now = self.db.clock()
            result = conn.execute(update(jobs).where(self._active(claim, now)).values(
                status="done", result_key=result_key, pages=pages, truncated=int(truncated),
                lease_token=None, lease_until=None, error=None,
            ))
            return result.rowcount == 1

    def fail(self, claim: Claim, error: str) -> None:
        with self.db.engine.begin() as conn:
            now = self.db.clock()
            conn.execute(update(jobs).where(self._active(claim, now)).values(
                status="failed", lease_token=None, lease_until=None, error=error,
            ))
