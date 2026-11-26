from pathlib import Path

from sqlalchemy import update

from app.generation.queue import JobQueue
from app.worker.runner import Generated, Worker
from migrations.v0001 import jobs
from test_courseware import create_course, login, publish, setup, submit


def test_worker_reads_pinned_versions_and_retries_only_failed_sections(setup):
    a, b, service, store = setup
    owner = login(a, "alice")
    course = create_course(a, owner)
    first = publish(a, owner, course, b"old chapter", 0).json()["id"]
    second = publish(a, owner, course, b"second chapter", 0).json()["id"]
    exam = a.post(f"/api/v1/courses/{course}/materials", headers=owner, data={"kind": "exam"},
                  files={"file": ("exam.txt", b"old exam")}).json()["id"]
    batch = submit(a, owner, course, [first, second], exam=exam, exam_mode="predict").json()["id"]
    assert publish(a, owner, course, b"new chapter", 1, first).status_code == 201
    assert a.post(f"/api/v1/courses/{course}/materials/{exam}/revisions", headers=owner,
                  data={"base_revision": 1}, files={"file": ("exam.txt", b"new exam")}).status_code == 201
    assert a.get(f"/api/v1/batches/{batch}/download", headers=owner).status_code == 409

    seen = []

    def generate(source: Path, exam_source: Path | None, options: dict, _workdir: Path):
        raw = source.read_text()
        seen.append(raw)
        assert exam_source is not None and exam_source.read_bytes() == b"old exam"
        assert options["exam_mode"] == "predict"
        if raw == "second chapter" and seen.count(raw) == 1:
            raise RuntimeError("synthetic failure")
        return Generated(f"# {raw}\n\n{raw}", pages=1)

    worker = Worker(JobQueue(service.db), store, generate)
    assert worker.run_one()
    assert worker.run_one()
    assert not worker.run_one()
    assert b.get(f"/api/v1/batches/{batch}", headers=owner).json()["status"] == "partial"
    assert b.post(f"/api/v1/batches/{batch}/retry", headers=owner).status_code == 202
    assert worker.run_one()
    assert not worker.run_one()
    assert seen == ["old chapter", "second chapter", "second chapter"]
    state = b.get(f"/api/v1/batches/{batch}", headers=owner).json()
    assert state["status"] == "done"
    assert [job["attempts"] for job in state["jobs"]] == [1, 2]
    doc = b.get(f"/api/v1/batches/{batch}/download", headers=owner)
    assert doc.status_code == 200
    assert doc.text.index("第 1 节：old chapter") < doc.text.index("第 2 节：second chapter")
    assert "new chapter" not in doc.text


def test_expired_worker_cannot_publish_and_cancel_stays_cancelled(setup):
    a, b, service, store = setup
    owner = login(a, "alice")
    course = create_course(a, owner)
    mat = publish(a, owner, course, b"first chapter", 0).json()["id"]
    batch = submit(a, owner, course, [mat]).json()["id"]
    queue = JobQueue(service.db)
    first = queue.claim()
    assert first is not None
    assert queue.claim() is None
    with service.db.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == first.id).values(lease_until=0))
    worker = Worker(queue, store, lambda *_: Generated("# replacement", pages=1))
    assert worker.run_one()
    assert not queue.finish(first, result_key="results/stale", pages=1, truncated=False)
    assert "replacement" in b.get(f"/api/v1/batches/{batch}/download", headers=owner).text

    another = submit(a, owner, course, [mat], key="cancel").json()["id"]
    other_claim = queue.claim()
    assert other_claim is not None
    assert b.post(f"/api/v1/batches/{another}/cancel", headers=owner).status_code == 200
    assert not queue.renew(other_claim)
    assert not queue.finish(other_claim, result_key="results/stale", pages=1, truncated=False)
    assert b.get(f"/api/v1/batches/{another}", headers=owner).json()["status"] == "cancelled"
