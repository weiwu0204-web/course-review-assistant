from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.service_routes import create_service_app
from app.courses.service import ReviewService, ServiceError
from app.infrastructure.database import Database
from app.infrastructure.object_store import LocalObjects


def test_session_revoked_across_api_instances(tmp_path):
    url = f"sqlite:///{(tmp_path / 'state.db').as_posix()}"
    db = Database(url)
    second_db = Database(url)
    db.initialize()
    db.create_user("alice", "alice-test-password")
    objects = LocalObjects(tmp_path / "objects")
    with (TestClient(create_service_app(ReviewService(db, objects))) as a,
          TestClient(create_service_app(ReviewService(second_db, objects))) as b):
        token = a.post("/api/v1/sessions", json={"username": "alice", "password": "alice-test-password"})
        assert token.status_code == 201
        headers = {"Authorization": f"Bearer {token.json()['token']}"}
        assert b.get("/api/v1/courses", headers=headers).status_code == 200
        assert a.delete("/api/v1/sessions/current", headers=headers).status_code == 204
        assert b.get("/api/v1/courses", headers=headers).status_code == 401
    db.close()
    second_db.close()


@pytest.mark.parametrize("mysql", [False, True])
def test_competing_revisions_publish_exactly_one(tmp_path, monkeypatch, mysql):
    import os

    url = os.getenv("PPT_TEST_MYSQL_URL") if mysql else f"sqlite:///{(tmp_path / 'state.db').as_posix()}"
    if not url:
        pytest.skip("requires isolated MySQL")
    db = Database(url)
    db.initialize()
    store = LocalObjects(tmp_path / "objects")
    service = ReviewService(db, store)
    owner = db.create_user("it-" + uuid4().hex, "test-only-password")
    course = service.create_course(owner, "test")["id"]
    initial = service.publish(owner, course, material=None, kind="courseware", base=0,
                              filename="initial.pptx", source=BytesIO(b"one"), size=3)
    start = Barrier(2)
    real_put = store.put

    def simultaneous(*args):
        start.wait(timeout=5)
        real_put(*args)

    monkeypatch.setattr(store, "put", simultaneous)

    def publish(content):
        try:
            return service.publish(owner, course, material=initial["id"], kind=None, base=1,
                                   filename="update.pptx", source=BytesIO(content), size=len(content))["revision"]
        except ServiceError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(publish, b"first")
        two = pool.submit(publish, b"other")
        assert sorted((one.result(timeout=10), two.result(timeout=10))) == [2, 409]
    assert len([p for p in store.root.rglob("*") if p.is_file()]) == 2
    db.close()
