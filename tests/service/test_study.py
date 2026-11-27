from io import BytesIO
import json
from uuid import uuid4

from pptx import Presentation
from sqlalchemy import select

from migrations.v0001 import schema_revisions
from migrations.v0002 import applied
from test_courseware import create_course, login, publish, setup, submit


def pptx(text: str) -> bytes:
    data = BytesIO()
    ppt = Presentation()
    ppt.slides.add_slide(ppt.slide_layouts[0]).shapes.title.text = text
    ppt.save(data)
    return data.getvalue()


def events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/event-stream"
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_versioned_library_and_history_are_isolated(setup):
    a, b, service, _ = setup
    alice, bob = login(a, "alice"), login(b, "bob")
    course = create_course(a, alice)
    material = publish(a, alice, course, pptx("Cache version alpha"), 0).json()["id"]
    collection = f"/api/v1/courses/{course}/knowledge-bases"
    r = a.post(collection, headers=alice, json={"materials": [material]})
    assert r.status_code == 201
    v1 = r.json()
    assert v1["sources"][0]["revision"] == 1
    assert publish(b, alice, course, pptx("Cache version beta"), 1, material).status_code == 201
    v2 = b.post(collection, headers=alice, json={"materials": [material]}).json()
    assert v2["sources"][0]["revision"] == 2

    old = f"/api/v1/knowledge-bases/{v1['id']}"
    new = f"/api/v1/knowledge-bases/{v2['id']}"
    assert b.get(old, headers=bob).status_code == 404
    assert b.get(old + "/history", headers=bob).status_code == 404
    assert b.post(old + "/ask", headers=bob, json={"question": "Cache"}).status_code == 404
    assert b.post(collection, headers=bob, json={"materials": [material]}).status_code == 404

    old_events = events(b.post(old + "/ask", headers=alice, json={"question": "Cache"}))
    new_events = events(b.post(new + "/ask", headers=alice, json={"question": "Cache"}))
    old_hit = next(e for e in old_events if e["type"] == "sources")["hits"][0]
    new_hit = next(e for e in new_events if e["type"] == "sources")["hits"][0]
    assert old_hit["page"] == 1 and "alpha" in old_hit["excerpt"]
    assert new_hit["page"] == 1 and "beta" in new_hit["excerpt"]
    assert old_events[-1]["type"] == "done"
    assert a.get(old + "/history", headers=alice).json()[0]["question"] == "Cache"
    assert b.get("/api/v1/stats", headers=bob).json()["qa_count"] == 0
    assert a.get("/api/v1/stats", headers=alice).json()["qa_count"] == 2

    empty = events(a.post(old + "/ask", headers=alice, json={"question": "ZzzzNoTokenOverlap"}))
    assert not any(x["type"] == "delta" for x in empty)
    assert empty[-1]["empty"]


def test_append_schema_and_owner_history(setup):
    a, _, service, _ = setup
    with service.db.engine.connect() as conn:
        assert conn.scalars(select(schema_revisions.c.version)).all() == [1]
        assert conn.scalars(select(applied.c.version)).all() == [2]
    alice, bob = login(a, "alice"), login(a, "bob")
    course = create_course(a, alice)
    material = publish(a, alice, course, pptx("Cache basics"), 0).json()["id"]
    batch = submit(a, alice, course, [material], uuid4().hex).json()
    assert a.get("/api/v1/batches", headers=alice).json()[0]["id"] == batch["id"]
    assert a.get("/api/v1/batches", headers=bob).json() == []
