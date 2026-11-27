import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.service_routes import create_service_app
from app.courses.service import ReviewService
from app.generation.queue import JobQueue
from app.infrastructure.database import Database
from app.infrastructure.object_store import LocalObjects
from migrations.v0001 import jobs

from test_courseware import create_course, publish, submit


@pytest.mark.skipif(not os.getenv("PPT_TEST_MYSQL_URL"), reason="requires isolated MySQL")
def test_concurrent_claim_and_lease_recovery_on_mysql(tmp_path):
    # Dedicated integration DB only; random IDs let the test run repeatedly.
    db = Database(os.environ["PPT_TEST_MYSQL_URL"])
    assert not db.sqlite
    db.initialize()
    queue = JobQueue(db, lease_seconds=10)
    name = "it-" + uuid4().hex
    db.create_user(name, "local-test-password")
    service = ReviewService(db, LocalObjects(tmp_path / "objects"))
    with TestClient(create_service_app(service)) as client:
        auth = client.post("/api/v1/sessions", json={"username": name, "password": "local-test-password"})
        headers = {"Authorization": f"Bearer {auth.json()['token']}"}
        cid = create_course(client, headers)
        mid = publish(client, headers, cid, b"sample", 0).json()["id"]
        batch = submit(client, headers, cid, [mid]).json()
        job_id = batch["jobs"][0]["id"]
        start = Barrier(2)

        def claim():
            start.wait(timeout=3)
            return queue.claim()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            claims = [item for future in futures if (item := future.result(timeout=5)) is not None]
        assert len(claims) == 1 and claims[0].id == job_id
        first = claims[0]
        with db.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == first.id).values(lease_until=0))
        new = queue.claim()
        assert new and new.id == first.id and new.token != first.token
        assert not queue.finish(first, result_key="results/stale", pages=1, truncated=False)
        assert queue.finish(new, result_key="results/current", pages=1, truncated=False)
        assert client.get(f"/api/v1/batches/{batch['id']}", headers=headers).json()["status"] == "done"
    db.close()
