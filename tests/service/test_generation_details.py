import json

from sqlalchemy import update

from test_courseware import create_course, login, publish, setup, submit

from app.generation.queue import JobQueue
from app.courses.service import digest
from app.worker.runner import Generated, Worker
from migrations.v0001 import batches


def test_section_diagnostics_are_attached_to_the_current_result(setup):
    a, b, service, store = setup
    auth, other = login(a, "alice"), login(b, "bob")
    course = create_course(a, auth)
    mat = publish(a, auth, course, b"sample", 0).json()["id"]
    submitted = submit(a, auth, course, [mat], enable_repair=True)
    assert submitted.status_code == 202
    batch = f"/api/v1/batches/{submitted.json()['id']}"

    def generate(_source, _exam, options, _work):
        assert options["enable_repair"]
        return Generated("# Complete", pages=1, coverage={"percent": 98, "term_count": 6},
                         repair={"rolled_back": False, "final_percent": 98})

    assert Worker(JobQueue(service.db), store, generate).run_one()
    job = b.get(batch, headers=auth).json()["jobs"][0]
    assert job["diagnostics"]["coverage"]["percent"] == 98
    assert job["diagnostics"]["repair"]["final_percent"] == 98
    assert b.get(batch, headers=other).status_code == 404


def test_idempotency_still_matches_a_preview_001_batch(setup):
    a, b, service, _ = setup
    auth = login(a, "alice")
    course = create_course(a, auth)
    mat = publish(a, auth, course, b"sample", 0).json()["id"]
    batch = submit(a, auth, course, [mat]).json()
    old_options = {"exam_mode": "direct", "template": "复习指南", "style": "详细", "use_vision": False}
    old_hash = digest(json.dumps([course, [mat], None, old_options], sort_keys=True))
    with service.db.engine.begin() as conn:
        conn.execute(update(batches).where(batches.c.id == batch["id"]).values(
            request_hash=old_hash, options=old_options,
        ))
    repeat = submit(b, auth, course, [mat])
    assert repeat.status_code == 202
    assert repeat.json()["id"] == batch["id"]
    assert submit(b, auth, course, [mat], enable_repair=True).status_code == 409
