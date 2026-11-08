import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as study


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", tmp_path / "none.json")   # defaults
    with TestClient(study.app) as c:
        yield c


def test_empty_then_roundtrip(client):
    assert client.get("/state").json() == {"done": {}, "quiz": {}, "log": {}, "review": {}, "cleared": 0,
                                           "blocks": {}, "later": [], "notes": {}, "marks": {}, "updated_at": None}
    r = client.put("/state", json={"done": {"sat-is": True, "b-setup": False}})
    assert r.status_code == 200
    got = client.get("/state").json()
    assert got["done"] == {"sat-is": True}  # false is dropped
    assert got["updated_at"]


def test_rejects_bad_input(client):
    assert client.put("/state", json={"done": {"<script>": True}}).status_code == 422
    assert client.put("/state", json={"done": {f"x{i}": True for i in range(1001)}}).status_code == 422
    assert client.put("/state", json={"log": {"yesterday": 5}}).status_code == 422
    assert client.put("/state", json={"quiz": {"x": {"best": 9, "of": 0}}}).status_code == 422
    assert client.put("/state", json={"review": {"py-types": "2026-11-08"}}).status_code == 422
    assert client.put("/state", json={"cleared": -1}).status_code == 422
    assert client.put("/state", json={"blocks": {"2026-11-07": 49}}).status_code == 422
    assert client.put("/state", json={"later": [{"t": "", "at": "2026-11-07"}]}).status_code == 422
    assert client.put("/state", json={"notes": {"Bad Id": "x"}}).status_code == 422
    assert client.put("/state", json={"marks": {"py-types": ["x" * 1001]}}).status_code == 422


def test_put_keeps_fields_it_leaves_out(client):
    # An older page sends only the fields it knows; notes must survive it.
    client.put("/state", json={"done": {"sat-is": True}, "notes": {"py-types": "mine"}, "marks": {"py-types": ["a"]}})
    client.put("/state", json={"done": {"sat-is": True, "sat-none": True}, "quiz": {}, "log": {}, "review": {}, "cleared": 0})
    got = client.get("/state").json()
    assert got["done"] == {"sat-is": True, "sat-none": True}
    assert got["notes"] == {"py-types": "mine"} and got["marks"] == {"py-types": ["a"]}


def test_full_state_roundtrip(client):
    body = {"done": {"sat-is": True, "read-py-types": True, "drill-login-0": True},
            "quiz": {"py-types": {"best": 2, "of": 3}}, "log": {"2026-11-07": 140},
            "review": {"py-types.1": "2026-11-08"}, "cleared": 3,
            "blocks": {"2026-11-07": 2}, "later": [{"t": "Laravel queues?", "at": "2026-11-07"}],
            "notes": {"py-types": "is vs ==: identity"}, "marks": {"py-types": ["None is a singleton"]}}
    assert client.put("/state", json=body).status_code == 200
    got = client.get("/state").json()
    del got["updated_at"]
    assert got == body


def test_old_rows_still_load(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id = 1), done TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("""INSERT INTO state VALUES (1, '{"sat-is": true}', '2026-11-07T10:00:00+00:00')""")
    conn.commit(); conn.close()
    monkeypatch.setattr(study, "DB_PATH", db)
    with TestClient(study.app) as c:
        got = c.get("/state").json()
        assert got["done"] == {"sat-is": True} and got["quiz"] == {} and got["review"] == {}


def test_serves_page_and_assets(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    r = client.get("/")
    assert r.status_code == 200 and "Study plan" in r.text
    assert "/*STUDY_CONFIG*/" not in r.text            # config was injected
    for path in ["/fonts/archivo.woff2", "/quizzes.json", "/drills.json", "/sw.js"]:
        assert client.get(path).status_code == 200, path


def test_config_is_injected_and_escaped(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"title": "</script><b>x", "target": {"label": "Exam", "at": "2026-12-01T09:00:00+01:00"}}))
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", cfg)
    with TestClient(study.app) as c:
        html = c.get("/").text
    assert "\\u003c/script>\\u003cb>x" in html and "</script><b>x" not in html
    assert '"label": "Exam"' in html


def test_naive_target_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        study.Config.model_validate({"target": {"label": "x", "at": "2026-12-01T09:00:00"}})


def test_every_lesson_has_text_and_a_check(client):
    page = study.PAGE.read_text()
    ids = re.findall(r'\{ id: "([a-z0-9-]+)", title:', page)
    quizzes = json.loads((study.HERE / "quizzes.json").read_text())
    assert len(ids) >= 20 and set(ids) == set(quizzes)
    for i in ids:
        assert client.get(f"/lessons/{i}.html").status_code == 200, i
    for lesson, qs in quizzes.items():
        for q, options, answer, why in qs:
            assert 0 <= answer < len(options) and why, lesson
    # every plan item's lesson link points at a real lesson
    assert set(re.findall(r'lesson: "([a-z0-9-]+)"', page)) <= set(ids)


def test_drills_are_well_formed():
    drills = json.loads((study.HERE / "drills.json").read_text())
    assert len({d["id"] for d in drills}) == len(drills)
    for d in drills:
        assert re.fullmatch(r"[a-z0-9]+", d["id"]) and d["code"] and len(d["findings"]) >= 3


def test_demo_mode_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_DEMO", "1")
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", tmp_path / "none.json")
    with TestClient(study.app) as c:
        assert '"demo": true' in c.get("/").text
        assert c.put("/state", json={"done": {"sat-is": True}}).status_code == 403
        assert c.get("/state").json()["done"] == {}
    assert not (tmp_path / "t.db").exists()      # no database created
