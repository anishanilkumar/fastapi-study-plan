import json
import os
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as study

ST = "/fastapi/state"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", tmp_path / "none.json")   # defaults
    with TestClient(study.app) as c:
        yield c


def test_empty_then_roundtrip(client):
    assert client.get(ST).json() == {"done": {}, "quiz": {}, "log": {}, "review": {}, "cleared": 0,
                                           "blocks": {}, "later": [], "notes": {}, "marks": {}, "marknotes": {},
                                           "answers": {}, "perday": {}, "deadline": None, "profile": None, "updated_at": None}
    r = client.put(ST, json={"done": {"sat-is": True, "b-setup": False}})
    assert r.status_code == 200
    got = client.get(ST).json()
    assert got["done"] == {"sat-is": True}  # false is dropped
    assert got["updated_at"]


def test_rejects_bad_input(client):
    assert client.put(ST, json={"done": {"<script>": True}}).status_code == 422
    assert client.put(ST, json={"done": {f"x{i}": True for i in range(1001)}}).status_code == 422
    assert client.put(ST, json={"log": {"yesterday": 5}}).status_code == 422
    assert client.put(ST, json={"quiz": {"x": {"best": 9, "of": 0}}}).status_code == 422
    assert client.put(ST, json={"review": {"py-types": "2026-11-08"}}).status_code == 422
    assert client.put(ST, json={"cleared": -1}).status_code == 422
    assert client.put(ST, json={"blocks": {"2026-11-07": 49}}).status_code == 422
    assert client.put(ST, json={"later": [{"t": "", "at": "2026-11-07"}]}).status_code == 422
    assert client.put(ST, json={"notes": {"Bad Id": "x"}}).status_code == 422
    assert client.put(ST, json={"marks": {"py-types": ["x" * 1001]}}).status_code == 422
    assert client.put(ST, json={"marknotes": {"py-types": {"a": "x" * 2001}}}).status_code == 422
    assert client.put(ST, json={"marknotes": {"Bad Id": {"a": "x"}}}).status_code == 422
    assert client.put(ST, json={"answers": {"py-types": {"n": 1, "ok": 1, "last": "2026-11-08", "r": True}}}).status_code == 422
    assert client.put(ST, json={"answers": {"py-types.1": {"n": 1, "ok": 2, "last": "2026-11-08", "r": True}}}).status_code == 422
    assert client.put(ST, json={"perday": {"2026-11-08": {"n": 1, "ok": 5}}}).status_code == 422
    assert client.put(ST, json={"deadline": "2026-11-09T21:00:00"}).status_code == 422   # no offset
    assert client.put(ST, json={"profile": {"target": {"label": "x" * 41, "at": "2026-11-10T12:30:00Z"}}}).status_code == 422
    assert client.put(ST, json={"profile": {"target": {"label": "Exam", "at": "2026-11-10T12:30:00"}}}).status_code == 422


def test_put_keeps_fields_it_leaves_out(client):
    # An older page sends only the fields it knows; notes must survive it.
    client.put(ST, json={"done": {"sat-is": True}, "notes": {"py-types": "mine"}, "marks": {"py-types": ["a"]},
                             "marknotes": {"py-types": {"a": "why"}}})
    client.put(ST, json={"done": {"sat-is": True, "sat-none": True}, "quiz": {}, "log": {}, "review": {}, "cleared": 0})
    got = client.get(ST).json()
    assert got["done"] == {"sat-is": True, "sat-none": True}
    assert got["notes"] == {"py-types": "mine"} and got["marks"] == {"py-types": ["a"]}
    assert got["marknotes"] == {"py-types": {"a": "why"}}


def test_full_state_roundtrip(client):
    body = {"done": {"sat-is": True, "read-py-types": True, "drill-login-0": True},
            "quiz": {"py-types": {"best": 2, "of": 3}}, "log": {"2026-11-07": 140},
            "review": {"py-types.1": "2026-11-08"}, "cleared": 3,
            "blocks": {"2026-11-07": 2}, "later": [{"t": "Laravel queues?", "at": "2026-11-07"}],
            "notes": {"py-types": "is vs ==: identity"}, "marks": {"py-types": ["None is a singleton"]},
            "marknotes": {"py-types": {"None is a singleton": "so `is None`, never `== None`"}},
            "answers": {"py-types.4": {"n": 3, "ok": 2, "last": "2026-11-08", "r": True}},
            "perday": {"2026-11-08": {"n": 20, "ok": 15}}, "deadline": "2026-11-09T20:00:00Z",
            "profile": {"start": "2026-11-07", "target": {"label": "Interview", "at": "2026-11-10T12:30:00Z"}}}
    assert client.put(ST, json=body).status_code == 200
    got = client.get(ST).json()
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
        got = c.get(ST).json()
        assert got["done"] == {"sat-is": True} and got["quiz"] == {} and got["review"] == {}


def test_serves_page_and_assets(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    for path in ["/", "/fastapi/"]:
        r = client.get(path)
        assert r.status_code == 200 and "/*STUDY_CONFIG*/" not in r.text, path   # config was injected
    assert '"home": {' in client.get("/").text and '"course": {"id": "fastapi"' in client.get("/fastapi/").text
    assert client.get("/fastapi", follow_redirects=False).headers["location"] == "/fastapi/"
    for path in ["/fonts/archivo.woff2", "/fastapi/quizzes.json", "/fastapi/drills.json", "/sw.js", "/courses.json"]:
        assert client.get(path).status_code == 200, path
    for path in ["/nope/", "/nope/state", "/fastapi/lessons/nope.html", "/fastapi/lessons/..%2Fcourse.json", "/fastapi/lessons/course.json"]:
        assert client.get(path).status_code == 404, path


def test_courses_index_lists_every_lesson(client):
    idx = client.get("/courses.json").json()
    assert list(idx) == list(study.app.state.courses)
    for cid, lessons in idx.items():
        for lid in lessons:
            assert client.get(f"/{cid}/lessons/{lid}.html").status_code == 200, (cid, lid)


def test_courses_are_kept_apart(client):
    client.put("/fastapi/state", json={"done": {"read-py-types": True}})
    other = [c for c in study.app.state.courses if c != "fastapi"]
    for cid in other:
        assert client.get(f"/{cid}/state").json()["done"] == {}
        client.put(f"/{cid}/state", json={"done": {"x-y": True}})
    assert client.get("/fastapi/state").json()["done"] == {"read-py-types": True}
    assert client.get("/state").json()["done"] == {"read-py-types": True}   # the old route is the first course
    home = client.get("/").text
    assert '"read": 1' in home


def test_config_is_injected_and_escaped(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"title": "</script><b>x", "target": {"label": "Exam", "at": "2026-12-01T09:00:00+01:00"}}))
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", cfg)
    with TestClient(study.app) as c:
        html = c.get("/fastapi/").text
    assert "\\u003c/script>\\u003cb>x" in html and "</script><b>x" not in html
    assert '"label": "Exam"' in html


def test_naive_target_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        study.Config.model_validate({"target": {"label": "x", "at": "2026-12-01T09:00:00"}})


def test_every_course_adds_up():
    # load_course checks lessons, questions, plan links and drills against
    # each other; this is the same check, so a broken course fails here first.
    courses = study.load_courses()
    assert "fastapi" in courses and len(courses["fastapi"].lessons) >= 20
    for c in courses.values():
        assert c.drills, c.id


def test_a_broken_course_stops_the_app(tmp_path):
    import shutil
    d = tmp_path / "fastapi"
    shutil.copytree(study.HERE / "courses" / "fastapi", d)
    q = json.loads((d / "quizzes.json").read_text())
    q.pop("py-types")
    (d / "quizzes.json").write_text(json.dumps(q))
    with pytest.raises(ValueError, match="py-types"):
        study.load_course(d)
    bad = tmp_path / "Fonts"
    shutil.copytree(study.HERE / "courses" / "fastapi", bad)
    with pytest.raises(ValueError, match="course ids"):
        study.load_course(bad)


def test_old_config_dates_belong_to_the_first_course():
    cfg = study.Config.model_validate({"start": "2026-11-07", "users": {"a": {"target": {"label": "Exam", "at": "2026-12-01T09:00:00+01:00"}}}})
    assert str(cfg.dates("fastapi").start) == "2026-11-07"
    assert cfg.users["a"].dates("fastapi").target.label == "Exam" and cfg.users["a"].dates("mqtt").target is None
    with pytest.raises(ValueError):
        study.Config.model_validate({"start": "2026-11-07", "courses": {"fastapi": {}}})
    with pytest.raises(ValueError):
        study.Config.model_validate({"stat": "2026-11-07"})      # a typo is an error, not ignored


def test_demo_mode_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_DEMO", "1")
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", tmp_path / "none.json")
    with TestClient(study.app) as c:
        assert '"demo": true' in c.get("/fastapi/").text
        assert c.put(ST, json={"done": {"sat-is": True}}).status_code == 403
        assert c.get(ST).json()["done"] == {}
    assert not (tmp_path / "t.db").exists()      # no database created


TOKEN = "t" * 40


@pytest.fixture
def mentor(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mentor_token": TOKEN}))
    monkeypatch.setattr(study, "CONFIG_PATH", cfg)
    with TestClient(study.app) as c:
        yield c


def test_mentor_page_is_read_only_and_leaves_out_parked(mentor):
    mentor.put(ST, json={"done": {"read-py-types": True}, "notes": {"py-types": "mine"},
                               "later": [{"t": "private thought", "at": "2026-11-08"}]})
    r = mentor.get(f"/mentor/{TOKEN}/fastapi/")
    assert r.status_code == 200 and r.headers["referrer-policy"] == "no-referrer"
    assert '"read-py-types": true' in r.text and '"mine"' in r.text
    assert "private thought" not in r.text
    assert f'url("/mentor/{TOKEN}/fonts/archivo.woff2")' in r.text
    assert mentor.get(f"/mentor/{TOKEN}/fonts/archivo.woff2").status_code == 200
    assert mentor.put(f"/mentor/{TOKEN}/fastapi/", json={}).status_code == 405


def test_mentor_token_checked_and_never_leaked(mentor):
    assert mentor.get("/mentor/" + "x" * 40).status_code == 404
    assert mentor.get("/mentor/" + "x" * 40 + "/fonts/archivo.woff2").status_code == 404
    assert mentor.get(f"/mentor/{TOKEN}/fonts/..%2Fapp.py").status_code == 404
    # The learner's page carries the link (it's behind the login); the token never
    # appears as config, and with no token configured there is no mentor page.
    assert f'"mentor_path": "/mentor/{TOKEN}"' in mentor.get("/fastapi/").text
    assert '"mentor_token"' not in mentor.get("/fastapi/").text


def test_no_mentor_page_without_a_token(client):
    assert client.get("/mentor/" + "t" * 40).status_code == 404
    assert '"mentor_path"' not in client.get("/fastapi/").text


A_TOKEN, B_TOKEN = "a" * 40, "b" * 40


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Two learners behind a proxy that names them; anish owns the old progress."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id = 1), done TEXT NOT NULL, updated_at TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}')")
    conn.execute("""INSERT INTO state VALUES (1, '{"sat-is": true}', '2026-11-07T10:00:00+00:00', '{}')""")
    conn.commit(); conn.close()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"owner": "anish", "users": {
        "anish": {"target": {"label": "Interview", "at": "2026-11-10T13:30:00+01:00"}, "mentor_token": A_TOKEN},
        "friend": {"mentor_token": B_TOKEN}}}))
    monkeypatch.setattr(study, "DB_PATH", db)
    monkeypatch.setattr(study, "CONFIG_PATH", cfg)
    monkeypatch.setenv("STUDY_USER_HEADER", "X-Study-User")
    with TestClient(study.app) as c:
        yield c


def as_(user):
    return {"X-Study-User": user}


def test_learners_are_kept_apart(two):
    assert two.get(ST, headers=as_("anish")).json()["done"] == {"sat-is": True}   # the old progress is the owner's
    assert two.get(ST, headers=as_("friend")).json()["done"] == {}
    two.put(ST, headers=as_("friend"), json={"done": {"sat-none": True}, "notes": {"py-types": "hers"}})
    assert two.get(ST, headers=as_("anish")).json()["done"] == {"sat-is": True}
    assert two.get(ST, headers=as_("anish")).json()["notes"] == {}
    assert two.get(ST, headers=as_("friend")).json()["notes"] == {"py-types": "hers"}


def test_no_name_no_progress(two):
    assert two.get(ST).status_code == 401
    assert two.put(ST, json={"done": {}}).status_code == 401
    assert two.get(ST, headers=as_("not a name!")).status_code == 401


def test_each_learner_gets_their_own_page_and_mentor_link(two):
    mine, hers = two.get("/fastapi/", headers=as_("anish")).text, two.get("/fastapi/", headers=as_("friend")).text
    assert f"/mentor/{A_TOKEN}" in mine and B_TOKEN not in mine and '"target": {"label": "Interview"' in mine
    assert f"/mentor/{B_TOKEN}" in hers and A_TOKEN not in hers and '"target": null' in hers
    assert '"store": "study-plan"' in mine and '"store": "study-plan-friend"' in hers
    two.put(ST, headers=as_("friend"), json={"done": {"sat-none": True}})
    assert '"sat-none": true' in two.get(f"/mentor/{B_TOKEN}/fastapi/").text
    assert '"sat-none": true' not in two.get(f"/mentor/{A_TOKEN}/fastapi/").text
    assert '"sat-is": true' in two.get(f"/mentor/{A_TOKEN}/fastapi/").text


def test_learners_from_before_courses_move_to_the_first_course(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE learners (user TEXT PRIMARY KEY, done TEXT NOT NULL, progress TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("""INSERT INTO learners VALUES ('anish', '{"sat-is": true}', '{"notes": {"py-types": "mine"}}', '2026-11-09T10:00:00+00:00')""")
    conn.execute("""INSERT INTO learners VALUES ('rohan', '{"b-setup": true}', '{}', '2026-11-09T11:00:00+00:00')""")
    conn.commit(); conn.close()
    monkeypatch.setattr(study, "DB_PATH", db)
    monkeypatch.setattr(study, "CONFIG_PATH", tmp_path / "none.json")
    monkeypatch.setenv("STUDY_USER_HEADER", "X-Study-User")
    with TestClient(study.app) as c:
        got = c.get(ST, headers=as_("anish")).json()
        assert got["done"] == {"sat-is": True} and got["notes"] == {"py-types": "mine"}
        assert c.get(ST, headers=as_("rohan")).json()["done"] == {"b-setup": True}
        c.put(ST, headers=as_("rohan"), json={"done": {"b-setup": True, "b-syntax": True}})
    with TestClient(study.app) as c:          # a second start copies nothing over it
        assert c.get(ST, headers=as_("rohan")).json()["done"] == {"b-setup": True, "b-syntax": True}


def test_mentor_home_lists_courses_under_the_token(mentor):
    r = mentor.get(f"/mentor/{TOKEN}")
    assert r.status_code == 200 and f'"href": "/mentor/{TOKEN}/fastapi/"' in r.text
    assert f'"home_href": "/mentor/{TOKEN}"' in r.text
    assert mentor.get(f"/mentor/{TOKEN}/nope/").status_code == 404


def test_config_naming_a_missing_course_stops_the_app(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"courses": {"nope": {"start": "2026-11-07"}}}))
    monkeypatch.setattr(study, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(study, "CONFIG_PATH", cfg)
    with pytest.raises(ValueError, match="no such course"):
        with TestClient(study.app):
            pass


def test_course_file_for_other_clients(client):
    got = client.get("/mqtt/course.json").json()
    assert got["id"] == "mqtt" and got["parts"] and got["ranks"][0]["xp"] == 0
    assert client.get("/nope/course.json").status_code == 404


def test_shared_game_cases_hold_for_the_page():
    # The page's own scoring, run headless against the cases Friday's copy is held to.
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    r = subprocess.run(["node", "tests/game_cases.mjs"], cwd=study.HERE, capture_output=True, text=True,
                       env={**os.environ, "TZ": "UTC"})
    assert r.returncode == 0, r.stdout + r.stderr
