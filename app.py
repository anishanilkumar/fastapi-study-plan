"""Study plan: a self-hosted Python + FastAPI course for PHP developers.

Serves one page, the lesson texts, the checks and drills, and keeps one
learner's progress in SQLite.

    GET  /                    the page, with config.json injected
    GET  /lessons/<id>.html   one lesson's reading text (an HTML fragment)
    GET  /quizzes.json        the check questions for every lesson
    GET  /drills.json         the code-review drills
    GET  /sw.js               the service worker that caches all of it offline
    GET  /mentor/<token>      a read-only copy of the page for a mentor, with the
                              progress, questions and drills inlined (no other calls),
                              when config.json sets mentor_token; 404 otherwise
    GET  /manifest.webmanifest, /icons/*   so it installs as an app on a phone
    GET  /state               {"done": ..., "quiz": ..., "log": ..., "review": ..., "cleared": n, "updated_at": ...}
    PUT  /state               the same without updated_at; replaces the whole state

    done     ticked ids: plan items, "read-<lesson>", "drill-<id>-<n>" findings
    quiz     best check score per lesson: {"py-types": {"best": 2, "of": 3}}
    log      XP earned per local day, for the streak and daily goal
    review   missed questions and when they're due again: {"py-types.1": "2026-11-08"}
    cleared  how many missed questions were later answered right
    blocks   focus blocks finished per local day: {"2026-11-07": 3}
    later    thoughts parked during a block: [{"t": "look up Laravel queues", "at": "2026-11-07"}]
    notes    the learner's own notes per lesson: {"py-types": "..."}
    marks    highlighted passages per lesson, as the text that was selected
    marknotes a note written on a highlight, keyed by its text: {"py-types": {"None is a singleton": "..."}}
    answers  every answer per question, for the analysis: {"py-types.4": {"n": 3, "ok": 2, "last": "2026-11-08", "r": true}}
    perday   answers per local day: {"2026-11-08": {"n": 20, "ok": 15}}
    deadline when everything should be done, set on the page; null falls back to config's finish_by

A PUT that leaves a field out keeps what's stored for it, so a tab still
running an older page (which doesn't know about notes) can't wipe them.

The mentor link is the one route meant to sit outside your login: put only
/mentor/ outside it. The token is the only thing guarding it, so make it long
(at least 32 characters; `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
and change it to revoke the link. Parked thoughts are left out of it.

STUDY_DEMO=1 runs a public demo: no database at all, the page keeps each
visitor's progress in their own browser, and PUT /state is refused. So
visitors never see or change each other's ticks, and there's nothing to reset.

One row holds the whole state as JSON. The page sends everything on each save,
so there is nothing to merge and the last write wins, which is what a single
learner wants. There is no login: run it on localhost, or put your own auth
in front of it (see README).

Run locally:  uv run --with fastapi --with uvicorn python app.py
"""

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from typing import Annotated
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("STUDY_DB", HERE / "study.db"))
CONFIG_PATH = Path(os.environ.get("STUDY_CONFIG", HERE / "config.json"))
PAGE = HERE / "study-plan.html"

# Ids look like "sat-mutable". Anything else is not from the page.
MAX_ITEMS = 1000
ID = re.compile(r"^[a-z0-9-]{1,64}$")
REVIEW_KEY = re.compile(r"^[a-z0-9-]{1,64}\.\d{1,2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---- Config: the learner's own details, kept out of the code ----

class Target(BaseModel):
    label: str = Field(max_length=40)        # "Interview"
    at: datetime                             # with an offset: "2026-01-13T14:00:00+01:00"

    @field_validator("at")
    @classmethod
    def aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("target.at needs a UTC offset, e.g. +01:00")
        return v


class Config(BaseModel):
    title: str = Field("Python and FastAPI, from the basics", max_length=120)
    start: date | None = None                # day 1 of the plan; None shows "Day 1"
    target: Target | None = None             # the date you're working towards
    finish_by: datetime | None = None        # default deadline for all the work; the page can change it
    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)  # plan item id -> {"t"/"why": html}
    # Turns on the read-only page at /mentor/<token>. Never sent to a page.
    mentor_token: str | None = Field(None, min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


    @field_validator("finish_by")
    @classmethod
    def finish_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("finish_by needs a UTC offset, e.g. +01:00")
        return v


def load_config() -> Config:
    # A missing config is fine (all defaults); a broken one fails loudly at startup.
    if not CONFIG_PATH.exists():
        return Config()
    return Config.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))


# ---- State ----

class Ticks(BaseModel):
    done: dict[str, bool] = Field(default_factory=dict)

    @field_validator("done")
    @classmethod
    def ids_look_right(cls, v: dict[str, bool]) -> dict[str, bool]:
        if len(v) > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} items")
        for k in v:
            if not ID.match(k):
                raise ValueError(f"bad item id: {k!r}")
        # Only ticked items are stored; false is the same as absent.
        return {k: True for k, on in v.items() if on}


class QuizScore(BaseModel):
    best: int = Field(ge=0, le=20)
    of: int = Field(ge=1, le=20)


class Parked(BaseModel):
    t: str = Field(min_length=1, max_length=500)
    at: str = Field(pattern=DATE.pattern)


Mark = Annotated[str, Field(min_length=1, max_length=1000)]
MarkNote = Annotated[str, Field(min_length=1, max_length=2000)]


class Tally(BaseModel):
    n: int = Field(ge=0, le=100_000)         # times answered
    ok: int = Field(ge=0, le=100_000)        # times right
    last: str = Field(pattern=DATE.pattern)  # day of the last answer
    r: bool                                  # was the last answer right


class DayTally(BaseModel):
    n: int = Field(ge=0, le=100_000)
    ok: int = Field(ge=0, le=100_000)


class Progress(BaseModel):
    """The game layer and the notebook: kept apart from `done` so older rows still load."""

    quiz: dict[str, QuizScore] = Field(default_factory=dict)
    log: dict[str, int] = Field(default_factory=dict)
    review: dict[str, str] = Field(default_factory=dict)
    cleared: int = Field(0, ge=0, le=100_000)
    blocks: dict[str, int] = Field(default_factory=dict)
    later: list[Parked] = Field(default_factory=list, max_length=300)
    notes: dict[str, Annotated[str, Field(max_length=20_000)]] = Field(default_factory=dict)
    marks: dict[str, list[Mark]] = Field(default_factory=dict)
    marknotes: dict[str, dict[Mark, MarkNote]] = Field(default_factory=dict)
    answers: dict[str, Tally] = Field(default_factory=dict)
    perday: dict[str, DayTally] = Field(default_factory=dict)
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def deadline_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("deadline needs a UTC offset")
        return v

    @field_validator("quiz")
    @classmethod
    def quiz_ids(cls, v: dict[str, QuizScore]) -> dict[str, QuizScore]:
        if len(v) > 200 or any(not ID.match(k) for k in v):
            raise ValueError("bad quiz ids")
        return v

    @field_validator("log")
    @classmethod
    def log_days(cls, v: dict[str, int]) -> dict[str, int]:
        if len(v) > 400 or any(not DATE.match(k) or abs(n) > 100_000 for k, n in v.items()):
            raise ValueError("log keys are YYYY-MM-DD, values small integers")
        return v

    @field_validator("review")
    @classmethod
    def review_keys(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 500 or any(not REVIEW_KEY.match(k) or not DATE.match(d) for k, d in v.items()):
            raise ValueError("review keys are <lesson>.<n>, values YYYY-MM-DD")
        return v

    @field_validator("blocks")
    @classmethod
    def block_days(cls, v: dict[str, int]) -> dict[str, int]:
        # 48 is a 24-hour day of 30-minute cycles: more is not from the page.
        if len(v) > 400 or any(not DATE.match(k) or not 0 <= n <= 48 for k, n in v.items()):
            raise ValueError("blocks keys are YYYY-MM-DD, values 0-48")
        return v

    @field_validator("answers")
    @classmethod
    def answer_keys(cls, v: dict[str, Tally]) -> dict[str, Tally]:
        if len(v) > 2000 or any(not REVIEW_KEY.match(k) or t.ok > t.n for k, t in v.items()):
            raise ValueError("answers keys are <lesson>.<n>, with ok <= n")
        return v

    @field_validator("perday")
    @classmethod
    def perday_keys(cls, v: dict[str, DayTally]) -> dict[str, DayTally]:
        if len(v) > 400 or any(not DATE.match(k) or t.ok > t.n for k, t in v.items()):
            raise ValueError("perday keys are YYYY-MM-DD, with ok <= n")
        return v

    @field_validator("notes", "marks", "marknotes")
    @classmethod
    def lesson_keys(cls, v: dict) -> dict:
        if len(v) > 200 or any(not ID.match(k) for k in v):
            raise ValueError("bad lesson ids")
        if any(isinstance(x, (list, dict)) and len(x) > 200 for x in v.values()):
            raise ValueError("at most 200 highlights per lesson")
        return v


class State(Ticks, Progress):
    pass


class StateOut(State):
    updated_at: str | None = None


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    # A fresh connection per call: sqlite3 connections must not be shared
    # across threads, and plain-def handlers run in FastAPI's threadpool.
    # `with conn` alone only commits or rolls back; it never closes.
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = load_config()
    app.state.demo = os.environ.get("STUDY_DEMO") == "1"
    if app.state.demo:
        yield               # no database in demo mode
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent, so once is enough
        conn.execute(
            "CREATE TABLE IF NOT EXISTS state ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " done TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        # Added with the quizzes and XP. A column, not a new table, so the
        # one-row design stays; ALTER only if this database predates it.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(state)")}
        if "progress" not in cols:
            conn.execute("ALTER TABLE state ADD COLUMN progress TEXT NOT NULL DEFAULT '{}'")
    yield


app = FastAPI(title="Study plan", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/fonts", StaticFiles(directory=HERE / "fonts"), name="fonts")
# Lesson texts: HTML fragments the page fetches into its reading view.
app.mount("/lessons", StaticFiles(directory=HERE / "lessons"), name="lessons")
app.mount("/icons", StaticFiles(directory=HERE / "icons"), name="icons")

NO_CACHE = {"Cache-Control": "no-cache"}  # revalidate each visit, so a deploy shows at once


def render_page(extra: dict) -> str:
    cfg = json.dumps({**app.state.config.model_dump(mode="json", exclude={"mentor_token"}), **extra})
    # "<" escaped so a title containing "</script>" can't end the script early.
    return PAGE.read_text(encoding="utf-8").replace("/*STUDY_CONFIG*/null", cfg.replace("<", "\\u003c"), 1)


@app.get("/", include_in_schema=False)
def page() -> HTMLResponse:
    extra: dict = {"demo": app.state.demo}
    if app.state.config.mentor_token and not app.state.demo:
        extra["mentor_path"] = "/mentor/" + app.state.config.mentor_token   # shown on Record, to copy
    return HTMLResponse(render_page(extra), headers=NO_CACHE)


def check_mentor(token: str) -> None:
    want = app.state.config.mentor_token
    if app.state.demo or not want or not secrets.compare_digest(token.encode(), want.encode()):
        raise HTTPException(404)


# The token is in the URL, so: no referrer to the docs links, nothing cached.
MENTOR_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@app.get("/mentor/{token}", include_in_schema=False)
def mentor_page(token: str) -> HTMLResponse:
    check_mentor(token)
    data = {
        "state": read_state().model_dump(mode="json", exclude={"later"}),
        "quizzes": json.loads((HERE / "quizzes.json").read_text(encoding="utf-8")),
        "drills": json.loads((HERE / "drills.json").read_text(encoding="utf-8")),
    }
    html = render_page({"demo": False, "mentor": data}).replace('url("/fonts/', f'url("/mentor/{token}/fonts/')
    return HTMLResponse(html, headers=MENTOR_HEADERS)


@app.get("/mentor/{token}/fonts/{name}", include_in_schema=False)
def mentor_font(token: str, name: str) -> FileResponse:
    check_mentor(token)
    path = HERE / "fonts" / name
    if not re.fullmatch(r"[a-z0-9-]+\.woff2", name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="font/woff2", headers=MENTOR_HEADERS)


@app.get("/quizzes.json", include_in_schema=False)
def quizzes() -> FileResponse:
    return FileResponse(HERE / "quizzes.json", headers=NO_CACHE)


@app.get("/drills.json", include_in_schema=False)
def drills() -> FileResponse:
    return FileResponse(HERE / "drills.json", headers=NO_CACHE)


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(HERE / "manifest.webmanifest", media_type="application/manifest+json", headers=NO_CACHE)


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(HERE / "sw.js", media_type="text/javascript", headers=NO_CACHE)


# Plain def, not async def: sqlite3 blocks, so these run in the threadpool
# instead of stalling the event loop.
@app.get("/state", response_model=StateOut)
def get_state() -> StateOut:
    if app.state.demo:
        return StateOut()
    return read_state()


def read_state() -> StateOut:
    with connect() as conn:
        row = conn.execute("SELECT done, progress, updated_at FROM state WHERE id = 1").fetchone()
    if row is None:
        return StateOut()
    return StateOut(done=json.loads(row[0]), **json.loads(row[1]), updated_at=row[2])


@app.put("/state", response_model=StateOut)
def put_state(body: State) -> StateOut:
    if app.state.demo:
        raise HTTPException(403, "demo: progress stays in your browser")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:  # commits on success, rolls back on error
        # Fields the body left out keep their stored value (see the top).
        missing = set(State.model_fields) - body.model_fields_set
        if missing:
            row = conn.execute("SELECT done, progress FROM state WHERE id = 1").fetchone()
            if row is not None:
                stored = State(done=json.loads(row[0]), **json.loads(row[1]))
                body = body.model_copy(update={k: getattr(stored, k) for k in missing})
        progress = Progress(**body.model_dump(exclude={"done"}))
        conn.execute(
            "INSERT INTO state (id, done, progress, updated_at) VALUES (1, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET done = excluded.done,"
            " progress = excluded.progress, updated_at = excluded.updated_at",
            (json.dumps(body.done), progress.model_dump_json(), now),
        )
    return StateOut(**body.model_dump(), updated_at=now)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("STUDY_HOST", "127.0.0.1"),
        port=int(os.environ.get("STUDY_PORT", "8781")),
    )
