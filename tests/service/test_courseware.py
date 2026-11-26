import pytest
from fastapi.testclient import TestClient

from app.api.service_routes import create_service_app
from app.courses.service import ReviewService
from app.infrastructure.database import Database
from app.infrastructure.object_store import LocalObjects, ObjectStoreError


@pytest.fixture
def setup(tmp_path):
    url = f"sqlite:///{(tmp_path / 'state.db').as_posix()}"
    db = Database(url)
    db.initialize()
    db.create_user("alice", "alice-test-password")
    db.create_user("bob", "bob-test-password")
    store = LocalObjects(tmp_path / "objects")
    service = ReviewService(db, store)
    second = ReviewService(Database(url), LocalObjects(tmp_path / "objects"))
    with TestClient(create_service_app(service)) as a, TestClient(create_service_app(second)) as b:
        yield a, b, service, store
    db.close()
    second.db.close()


def login(client, username):
    res = client.post("/api/v1/sessions", json={
        "username": username, "password": f"{username}-test-password",
    })
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def create_course(client, headers):
    res = client.post("/api/v1/courses", headers=headers, json={"title": "测试课程"})
    assert res.status_code == 201
    return res.json()["id"]


def publish(client, headers, course, content, base, material=None):
    collection = f"/api/v1/courses/{course}/materials"
    route = f"{collection}/{material}/revisions" if material else collection
    return client.post(route, headers=headers, data={"base_revision": base},
                       files={"file": ("lecture.pptx", content)})


def submit(client, headers, course, materials, key="key-1", **options):
    return client.post(f"/api/v1/courses/{course}/batches",
                       headers={**headers, "Idempotency-Key": key},
                       json={"materials": materials, **options})


def test_course_update_pins_inputs_and_cross_instance_reads(setup):
    first, second, service, _ = setup
    headers = login(first, "alice")
    course = create_course(first, headers)
    v1 = publish(first, headers, course, b"version one", 0)
    assert v1.status_code == 201
    mat = v1.json()["id"]
    a = submit(first, headers, course, [mat], "batch-a")
    assert a.status_code == 202
    v2 = publish(second, headers, course, b"version two", 1, mat)
    assert v2.status_code == 201
    b = submit(second, headers, course, [mat], "batch-b")
    assert b.status_code == 202
    old = second.get(f"/api/v1/batches/{a.json()['id']}", headers=headers)
    assert old.status_code == 200
    assert old.json()["jobs"][0]["revision"] == 1
    assert b.json()["jobs"][0]["revision"] == 2
    assert "object_key" not in old.text
    owner = service.owner(headers["Authorization"].split()[1])
    for number, expected in ((1, b"version one"), (2, b"version two")):
        with service.objects.open(service.revision(owner, course, mat, number)["object_key"]) as stream:
            assert stream.read() == expected


def test_publication_failure_or_conflict_keeps_current_version(setup, monkeypatch):
    client, _, _, store = setup
    headers = login(client, "alice")
    course = create_course(client, headers)
    mat = publish(client, headers, course, b"one", 0).json()["id"]
    assert publish(client, headers, course, b"stale", 0, mat).status_code == 409

    def fail(*args, **kwargs):
        raise ObjectStoreError("unavailable")

    monkeypatch.setattr(store, "put", fail)
    assert publish(client, headers, course, b"two", 1, mat).status_code == 503
    now = client.get(f"/api/v1/courses/{course}/materials", headers=headers)
    assert now.json()[0]["revision"] == 1


def test_owner_required_for_materials_tasks_and_downloads(setup):
    a, b, _, _ = setup
    alice, bob = login(a, "alice"), login(b, "bob")
    course = create_course(a, alice)
    mat = publish(a, alice, course, b"one", 0).json()["id"]
    batch = submit(a, alice, course, [mat]).json()["id"]
    assert a.get("/api/v1/courses").status_code == 401
    assert b.get("/api/v1/courses", headers=bob).json() == []
    assert b.get(f"/api/v1/courses/{course}/materials", headers=bob).status_code == 404
    assert publish(b, bob, course, b"two", 1, mat).status_code == 404
    assert submit(b, bob, course, [mat]).status_code == 404
    assert b.get(f"/api/v1/batches/{batch}", headers=bob).status_code == 404
    assert b.get(f"/api/v1/batches/{batch}/download", headers=bob).status_code == 404
    assert b.post(f"/api/v1/batches/{batch}/retry", headers=bob).status_code == 404
    assert a.get("/api/settings/status", headers=alice).status_code == 404


def test_idempotency_is_per_owner_and_preserves_original_revision(setup):
    a, b, _, _ = setup
    alice, bob = login(a, "alice"), login(b, "bob")
    course = create_course(a, alice)
    mat = publish(a, alice, course, b"one", 0).json()["id"]
    first = submit(a, alice, course, [mat]).json()
    assert publish(a, alice, course, b"two", 1, mat).status_code == 201
    repeat = submit(b, alice, course, [mat]).json()
    assert repeat["id"] == first["id"]
    assert repeat["jobs"][0]["revision"] == 1
    changed = submit(a, alice, course, [mat], template="重点总结")
    assert changed.status_code == 409
    other_course = create_course(b, bob)
    other_mat = publish(b, bob, other_course, b"other", 0).json()["id"]
    assert submit(b, bob, other_course, [other_mat]).status_code == 202
