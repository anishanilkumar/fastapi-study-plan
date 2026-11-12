"""Study: self-hosted, gamified courses, each with lessons, checks, quizzes and drills.

Serves one page in two modes (the list of courses, and a course), each
course's lessons, questions and drills from courses/<id>/, and keeps each
learner's progress per course in SQLite.

    GET  /                          every course, with how far along each one is
    GET  /<course>/                 the course page, with config.json and course.json injected
    GET  /<course>/lessons/<id>.html  one lesson's reading text (an HTML fragment)
    GET  /<course>/quizzes.json     the questions for every lesson
    GET  /<course>/drills.json      the drills
    GET  /<course>/course.json      the course itself (plan, parts, ranks), for other clients
    GET  /<course>/state            {"done": ..., "quiz": ..., "log": ..., "review": ..., "cleared": n, "updated_at": ...}
    PUT  /<course>/state            the same without updated_at; replaces the course's state
    GET  /courses.json              course ids and lesson ids, for the service worker
    GET  /sw.js                     the service worker that caches all of it offline
    GET  /mentor/<token>            a read-only list of the learner's courses, when
                                    config.json gives them a mentor_token; 404 otherwise
    GET  /mentor/<token>/<course>   a read-only copy of a course page, with the progress,
                                    questions and drills inlined (no other calls)
    GET  /manifest.webmanifest, /icons/*   so it installs as an app on a phone
    GET, PUT /state                 the first course's state, for a tab still running
                                    the page from before there were courses

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
    profile  the learner's own start date and goal, set on the page (asked at first login);
             null falls back to config: {"start": "2026-11-07", "target": {"label": "Interview", "at": "..."}}

All of it is per course: each course has its own XP, streak, goal and deadline.
A PUT that leaves a field out keeps what's stored for it, so a tab still
running an older page (which doesn't know about notes) can't wipe them.

A course is a folder, courses/<id>/: course.json (title, plan, parts, ranks;
see README), lessons/<lesson>.html, quizzes.json and drills.json. They're all
checked against each other at startup, and a course that doesn't add up stops
the app with the reason.

Several learners: set STUDY_USER_HEADER to a header your auth proxy sets to
the logged-in user's name (and strips from what clients send). Each name
gets its own progress, and its own dates and mentor link from "users" in
config.json. Without it there is one learner, named by config's "owner"
(or nameless). Progress from before learners were named belongs to "owner",
and progress from before there were courses belongs to the first course,
LEGACY_COURSE.

The mentor link is the one route meant to sit outside your login: put only
/mentor/ outside it. The token is the only thing guarding it, so make it long
(at least 32 characters; `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
and change it to revoke the link. Parked thoughts are left out of it.

STUDY_DEMO=1 runs a public demo: no database at all, the page keeps each
visitor's progress in their own browser, and PUT is refused. So visitors
never see or change each other's ticks, and there's nothing to reset.

One row per learner and course holds that state as JSON. The page sends
everything on each save, so there is nothing to merge and the last write wins,
which is what a single learner wants. There is no login: run it on localhost,
or put your own auth in front of it (see README).

Run locally:  uv run --with fastapi --with uvicorn python app.py
"""

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("STUDY_DB", HERE / "study.db"))
CONFIG_PATH = Path(os.environ.get("STUDY_CONFIG", HERE / "config.json"))
COURSES_DIR = Path(os.environ.get("STUDY_COURSES", HERE / "courses"))
PAGE = HERE / "study.html"
# The course this app was before it had courses: its progress rows, its
# browser storage keys and the old /state route all belong to it.
LEGACY_COURSE = "fastapi"

# Ids look like "sat-mutable". Anything else is not from the page.
MAX_ITEMS = 1000
ID = re.compile(r"^[a-z0-9-]{1,64}$")
REVIEW_KEY = re.compile(r"^[a-z0-9-]{1,64}\.\d{1,2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COURSE_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Paths a course id would hide.
RESERVED = {"fonts", "icons", "mentor", "state", "oauth2", "courses"}


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


USER = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")
DATE_FIELDS = ("start", "target", "finish_by", "overrides")


class Dates(BaseModel):
    """One learner's dates and texts for one course."""

    model_config = ConfigDict(extra="forbid")

    start: date | None = None                # day 1 of the plan; None shows "Day 1"
    target: Target | None = None             # the date you're working towards
    finish_by: datetime | None = None        # default deadline for all the work; the page can change it
    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)  # plan item id -> {"t"/"why": html}

    @field_validator("finish_by")
    @classmethod
    def finish_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("finish_by needs a UTC offset, e.g. +01:00")
        return v


class Personal(BaseModel):
    """One learner: their dates per course, and their mentor link."""

    model_config = ConfigDict(extra="forbid")

    courses: dict[str, Dates] = Field(default_factory=dict)
    # Turns on the read-only pages at /mentor/<token>. Never sent to a page.
    mentor_token: str | None = Field(None, min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="before")
    @classmethod
    def dates_from_before_courses(cls, v: Any) -> Any:
        # A config from before courses has the dates at this level: they're
        # the first course's. Moved, not copied, so they can't be set twice.
        if isinstance(v, dict) and any(k in v for k in DATE_FIELDS):
            v = dict(v)
            old = {k: v.pop(k) for k in DATE_FIELDS if k in v}
            courses = dict(v.get("courses") or {})
            if LEGACY_COURSE in courses:
                raise ValueError(f"dates both at the top and under courses.{LEGACY_COURSE}: keep one")
            courses[LEGACY_COURSE] = old
            v["courses"] = courses
        return v

    def dates(self, course: str) -> Dates:
        return self.courses.get(course) or Dates()


class Config(Personal):
    """The top-level personal fields are for a learner with no entry in `users`."""

    title: str = Field("Study", max_length=120)    # the heading of the list of courses
    owner: str | None = Field(None, pattern=USER.pattern)    # the one learner, when no header names them
    users: dict[str, Personal] = Field(default_factory=dict)

    @field_validator("users")
    @classmethod
    def user_names(cls, v: dict[str, Personal]) -> dict[str, Personal]:
        if any(not USER.match(k) for k in v):
            raise ValueError("user names are letters, digits and ._@-")
        tokens = [p.mentor_token for p in v.values() if p.mentor_token]
        if len(tokens) != len(set(tokens)):
            raise ValueError("each learner needs their own mentor_token")
        return v

    def personal(self, user: str) -> Personal:
        if user in self.users:
            return self.users[user]
        return Personal(**self.model_dump(include=set(Personal.model_fields)))

    def mentor_user(self, token: str) -> str | None:
        """Whose mentor link this is, if anyone's. Every token is compared, in constant time."""
        found = None
        pairs = [(u, p.mentor_token) for u, p in self.users.items()] + [(self.owner or "", self.mentor_token)]
        for user, want in pairs:
            if want and secrets.compare_digest(token.encode(), want.encode()) and found is None:
                found = user
        return found


def load_config(courses: dict[str, "Course"] | None = None) -> Config:
    # A missing config is fine (all defaults); a broken one fails loudly at startup.
    if not CONFIG_PATH.exists():
        return Config()
    cfg = Config.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))
    if courses is not None:
        for name, p in [("", cfg), *cfg.users.items()]:
            unknown = set(p.courses) - set(courses)
            if unknown:
                raise ValueError(f"config{' for ' + name if name else ''}: no such course {sorted(unknown)}")
    return cfg


# ---- Courses: everything a course is, from its folder ----

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanItem(Strict):
    id: str = Field(pattern=ID.pattern)
    t: str                                   # html
    why: str | None = None                   # html
    lesson: str | None = None
    min: float | None = Field(None, ge=0)    # minutes, when the estimate isn't the default
    src: tuple[str, str] | None = None       # [text, url] of the docs


class Group(Strict):
    label: str | None = None
    items: list[PlanItem]


class Day(Strict):
    id: str = Field(pattern=ID.pattern)
    n: int = Field(ge=0, le=60)              # days from day 1
    part: str | None = None                  # "evening": a second block on the same day
    title: str
    target_title: bool = False               # the title names the goal when there is one: "The day itself: interview at 14:00"
    groups: list[Group]
    checks: list[PlanItem]


class Lesson(Strict):
    id: str = Field(pattern=ID.pattern)
    title: str
    min: int = Field(ge=1, le=240)           # reading time
    day: str | None                          # the plan day that uses it; null is an extra


class Part(Strict):
    n: int = Field(ge=1, le=20)
    title: str
    short: str | None = None                 # for the quiz chips
    blurb: str
    lessons: list[Lesson] = Field(min_length=1)


class Need(Strict):
    """One thing a rank needs; exactly one of the kinds below."""

    label: str | None = None
    read: int | None = None
    parts: list[int] | Literal["all"] | None = None
    drills: int | Literal["all"] | None = None
    spare: int | None = None                 # with drills "all": the worst bug may be missed in this many
    worst: int | None = None
    answered: int | None = None
    right: float | None = Field(None, ge=0, le=1)
    noweak: bool | None = None
    nodue: bool | None = None

    @model_validator(mode="after")
    def one_kind(self) -> "Need":
        kinds = [k for k in ("read", "parts", "drills", "worst", "answered", "noweak", "nodue") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(f"a rank requirement is one of read, parts, drills, worst, answered, noweak, nodue; got {kinds}")
        return self


class Rank(Strict):
    xp: int = Field(ge=0)
    t: str
    need: list[Need]


class Shift(Strict):
    label: str
    html: str


class Map(Strict):
    label: str
    title: str
    from_: str = Field(alias="from")
    to: str
    rows: list[tuple[str, str]]


class CourseFile(Strict):
    title: str = Field(max_length=120)
    short: str = Field(max_length=24)        # the course switcher and the cards
    order: int = 100                         # place on the home page
    blurb: str                               # the card on the home page
    lede: str                                # under the title on the course page
    goal: int = Field(300, ge=10)            # XP a day
    shift: Shift | None = None               # the one idea to hold on to, on the plan
    map: Map | None = None                   # a table from what you know to what's new
    plan_foot: str | None = None
    plan: list[Day]
    parts: list[Part]
    ranks: list[Rank] = Field(min_length=1)


@dataclass
class Course:
    id: str
    dir: Path
    file: CourseFile
    data: dict           # course.json as the page gets it
    quizzes: dict
    drills: list

    @property
    def lessons(self) -> list[Lesson]:
        return [l for p in self.file.parts for l in p.lessons]


DRILL_KEYS = {"id", "after", "level", "title", "minutes", "context", "code", "findings", "optional"}


def load_course(d: Path) -> Course:
    """One course, checked: every reference in it points at something real."""
    cid = d.name
    where = f"courses/{cid}"
    if not COURSE_ID.match(cid) or cid in RESERVED:
        raise ValueError(f"{where}: course ids are lower-case letters, digits and -, and not one of {sorted(RESERVED)}")
    raw = json.loads((d / "course.json").read_text(encoding="utf-8"))
    cf = CourseFile.model_validate(raw)
    quizzes = json.loads((d / "quizzes.json").read_text(encoding="utf-8"))
    drills = json.loads((d / "drills.json").read_text(encoding="utf-8")) if (d / "drills.json").exists() else []
    lessons = [l for p in cf.parts for l in p.lessons]
    ids = [l.id for l in lessons]
    days = {day.id for day in cf.plan}
    if len(set(ids)) != len(ids):
        raise ValueError(f"{where}: a lesson id is used twice")
    if len({p.n for p in cf.parts}) != len(cf.parts):
        raise ValueError(f"{where}: two parts with the same n")
    items = [i for day in cf.plan for g in day.groups for i in g.items] + [i for day in cf.plan for i in day.checks]
    if len({i.id for i in items}) != len(items) or len(days) != len(cf.plan):
        raise ValueError(f"{where}: a plan day or item id is used twice")
    for l in lessons:
        if not (d / "lessons" / f"{l.id}.html").is_file():
            raise ValueError(f"{where}: no lessons/{l.id}.html")
        if l.day is not None and l.day not in days:
            raise ValueError(f"{where}: lesson {l.id} is on day {l.day!r}, which isn't in the plan")
    for i in items:
        if i.lesson and i.lesson not in ids:
            raise ValueError(f"{where}: plan item {i.id} links to lesson {i.lesson!r}, which isn't in the syllabus")
    if set(quizzes) != set(ids):
        raise ValueError(f"{where}: quizzes.json and the syllabus disagree: {sorted(set(quizzes) ^ set(ids))}")
    for lid, qs in quizzes.items():
        if len(qs) < 3:
            raise ValueError(f"{where}: {lid} needs at least three questions (the first three are its check)")
        for q in qs:
            if not (len(q) == 4 and isinstance(q[1], list) and 2 <= len(q[1]) <= 4 and len(set(q[1])) == len(q[1])
                    and isinstance(q[2], int) and 0 <= q[2] < len(q[1]) and q[0] and q[3]):
                raise ValueError(f"{where}: a question in {lid} isn't [question, options (2-4, all different), answer index, why]: {str(q)[:80]}")
    seen = set()
    for dr in drills:
        if set(dr) - DRILL_KEYS or not re.fullmatch(r"[a-z0-9]+", dr.get("id", "")) or dr["id"] in seen:
            raise ValueError(f"{where}: drill {dr.get('id')!r}: a bad or repeated id, or an unknown key")
        seen.add(dr["id"])
        if dr.get("after") and dr["after"] not in ids:
            raise ValueError(f"{where}: drill {dr['id']} comes after lesson {dr['after']!r}, which isn't in the syllabus")
        if dr.get("level") not in (1, 2, 3, 4) or not dr.get("code") or len(dr.get("findings", [])) < 3:
            raise ValueError(f"{where}: drill {dr['id']} needs a level 1-4, code and at least three findings")
    for r in cf.ranks:
        for n in r.need:
            if isinstance(n.parts, list) and set(n.parts) - {p.n for p in cf.parts}:
                raise ValueError(f"{where}: rank {r.t} needs a part that doesn't exist")
    if cf.ranks[0].xp != 0 or cf.ranks[0].need:
        raise ValueError(f"{where}: the first rank is where everyone starts: 0 XP, no needs")
    data = {"id": cid, **cf.model_dump(mode="json", by_alias=True, exclude_none=True)}
    return Course(cid, d, cf, data, quizzes, drills)


def load_courses() -> dict[str, Course]:
    found = [load_course(d) for d in sorted(COURSES_DIR.iterdir()) if (d / "course.json").is_file()]
    if not found:
        raise ValueError(f"no courses in {COURSES_DIR}")
    found.sort(key=lambda c: (c.file.order, c.id))
    return {c.id: c for c in found}


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


class Profile(BaseModel):
    """What the learner set on the page. Present means it wins over config, even when empty."""

    start: date | None = None
    target: Target | None = None


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
    profile: Profile | None = None

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


def migrate(conn: sqlite3.Connection, owner: str) -> None:
    """Bring any older database up to one row per learner and course."""
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
    # One row per learner. The old one-row table is copied in once, as
    # the owner's, and then left alone (a copy to go back to).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learners ("
        " user TEXT PRIMARY KEY,"
        " done TEXT NOT NULL,"
        " progress TEXT NOT NULL,"
        " updated_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT COUNT(*) FROM learners").fetchone()[0] == 0:
        conn.execute("INSERT INTO learners SELECT ?, done, progress, updated_at FROM state WHERE id = 1", (owner,))
    # One row per learner and course. Everything before courses was the
    # first course, so learners is copied in once as that, and left alone too.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS progress ("
        " user TEXT NOT NULL,"
        " course TEXT NOT NULL,"
        " done TEXT NOT NULL,"
        " progress TEXT NOT NULL,"
        " updated_at TEXT NOT NULL,"
        " PRIMARY KEY (user, course))"
    )
    if conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 0:
        conn.execute("INSERT INTO progress SELECT user, ?, done, progress, updated_at FROM learners", (LEGACY_COURSE,))
    if owner and not conn.execute("SELECT 1 FROM progress WHERE user = ?", (owner,)).fetchone():
        # Named an owner after the copy: the nameless progress is theirs.
        conn.execute("UPDATE progress SET user = ? WHERE user = ''", (owner,))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.courses = load_courses()
    app.state.config = load_config(app.state.courses)
    app.state.demo = os.environ.get("STUDY_DEMO") == "1"
    if app.state.demo:
        yield               # no database in demo mode
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        migrate(conn, app.state.config.owner or "")
    yield


def learner(request: Request) -> str:
    """Whose progress this request is about."""
    header = os.environ.get("STUDY_USER_HEADER")
    if not header:
        return app.state.config.owner or ""
    user = request.headers.get(header, "")
    if not USER.match(user):
        # The proxy always sets it; without it, nobody can be told apart.
        raise HTTPException(401, "no learner name from the proxy")
    return user


def course_of(cid: str) -> Course:
    c = app.state.courses.get(cid)
    if c is None:
        raise HTTPException(404, "no such course")
    return c


app = FastAPI(title="Study", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/fonts", StaticFiles(directory=HERE / "fonts"), name="fonts")
app.mount("/icons", StaticFiles(directory=HERE / "icons"), name="icons")

NO_CACHE = {"Cache-Control": "no-cache"}  # revalidate each visit, so a deploy shows at once
# A page per learner: never shared by a cache between them.
PER_LEARNER = {**NO_CACHE, "Vary": "Cookie"}
# The token is in the URL, so: no referrer to the docs links, nothing cached.
MENTOR_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def inject(cfg: dict) -> str:
    # "<" escaped so a title containing "</script>" can't end the script early.
    text = json.dumps(cfg).replace("<", "\\u003c")
    return PAGE.read_text(encoding="utf-8").replace("/*STUDY_CONFIG*/null", text, 1)


def store_key(user: str, course: str) -> str:
    """The browser's local copy, per learner and course; the first course keeps the keys it had."""
    cfg = app.state.config
    key = "study-plan-demo" if app.state.demo else "study-plan" if user == (cfg.owner or "") else "study-plan-" + user
    return key if course == LEGACY_COURSE else key + "@" + course


def summary(user: str | None, c: Course, href: str) -> dict:
    """A course's card on the home page: its size, and how far this learner is."""
    lessons = c.lessons
    counted = [d for d in c.drills if not d.get("optional")]
    out = {"id": c.id, "title": c.file.title, "short": c.file.short, "blurb": c.file.blurb, "href": href,
           "lessons": len(lessons), "drills": len(counted), "hours": round(sum(l.min for l in lessons) / 30) / 2,
           "read": 0, "passed": 0, "drills_done": 0, "updated_at": None}
    if user is None:
        return out
    st = read_state(user, c.id)
    out["read"] = sum(1 for l in lessons if st.done.get("read-" + l.id))
    out["passed"] = sum(1 for l in lessons if (q := st.quiz.get(l.id)) and q.best >= q.of - 1)
    out["drills_done"] = sum(1 for d in counted if any(k.startswith(f"drill-{d['id']}-") for k in st.done))
    out["updated_at"] = st.updated_at
    return out


def home_page(user: str | None, base: str, extra: dict) -> str:
    courses = app.state.courses.values()
    return inject({"title": app.state.config.title, "home_href": base or "/",
                   "home": {"courses": [summary(user, c, f"{base}/{c.id}/") for c in courses],
                            "legacy": f"{base}/{LEGACY_COURSE}/" if LEGACY_COURSE in app.state.courses else None},
                   **extra})


def course_page(user: str, c: Course, base: str, extra: dict) -> str:
    me = app.state.config.personal(user).dates(c.id).model_dump(mode="json")
    links = [{"id": x.id, "short": x.file.short, "href": f"{base}/{x.id}/"} for x in app.state.courses.values()]
    return inject({"title": app.state.config.title, "course": c.data, "courses": links, "home_href": base or "/",
                   **me, "store": store_key(user, c.id), **extra})


@app.get("/", include_in_schema=False)
def home(request: Request) -> HTMLResponse:
    if app.state.demo:
        return HTMLResponse(home_page(None, "", {"demo": True}), headers=NO_CACHE)
    return HTMLResponse(home_page(learner(request), "", {"demo": False}), headers=PER_LEARNER)


@app.get("/courses.json", include_in_schema=False)
def courses_index() -> JSONResponse:
    # What the service worker saves for offline use.
    return JSONResponse({c.id: [l.id for l in c.lessons] for c in app.state.courses.values()}, headers=NO_CACHE)


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(HERE / "manifest.webmanifest", media_type="application/manifest+json", headers=NO_CACHE)


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(HERE / "sw.js", media_type="text/javascript", headers=NO_CACHE)


# ---- Mentor: read-only, outside the login, guarded by the token ----

def check_mentor(token: str) -> str:
    user = None if app.state.demo else app.state.config.mentor_user(token)
    if user is None:
        raise HTTPException(404)
    return user


def mentor_fonts(html: str, token: str) -> str:
    # The fonts sit behind the login; the mentor gets them under the token.
    return html.replace('url("/fonts/', f'url("/mentor/{token}/fonts/')


@app.get("/mentor/{token}", include_in_schema=False)
def mentor_home(token: str) -> HTMLResponse:
    user = check_mentor(token)
    html = home_page(user, f"/mentor/{token}", {"demo": False, "mentor": True})
    return HTMLResponse(mentor_fonts(html, token), headers=MENTOR_HEADERS)


@app.get("/mentor/{token}/fonts/{name}", include_in_schema=False)
def mentor_font(token: str, name: str) -> FileResponse:
    check_mentor(token)
    path = HERE / "fonts" / name
    if not re.fullmatch(r"[a-z0-9-]+\.woff2", name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="font/woff2", headers=MENTOR_HEADERS)


@app.get("/mentor/{token}/{cid}", include_in_schema=False)
def mentor_course_bare(token: str, cid: str) -> RedirectResponse:
    check_mentor(token)
    course_of(cid)
    return RedirectResponse(f"/mentor/{token}/{cid}/", 308)


@app.get("/mentor/{token}/{cid}/", include_in_schema=False)
def mentor_page(token: str, cid: str) -> HTMLResponse:
    user = check_mentor(token)
    c = course_of(cid)
    data = {
        "state": read_state(user, c.id).model_dump(mode="json", exclude={"later"}),
        "quizzes": c.quizzes,
        "drills": c.drills,
    }
    html = course_page(user, c, f"/mentor/{token}", {"demo": False, "mentor": data})
    return HTMLResponse(mentor_fonts(html, token), headers=MENTOR_HEADERS)


# ---- State. Plain def, not async def: sqlite3 blocks, so these run in the
# threadpool instead of stalling the event loop. ----

def read_state(user: str, course: str) -> StateOut:
    with connect() as conn:
        row = conn.execute("SELECT done, progress, updated_at FROM progress WHERE user = ? AND course = ?", (user, course)).fetchone()
    if row is None:
        return StateOut()
    return StateOut(done=json.loads(row[0]), **json.loads(row[1]), updated_at=row[2])


def get_state(request: Request, course: str) -> StateOut:
    if app.state.demo:
        return StateOut()
    return read_state(learner(request), course)


def put_state(body: State, request: Request, course: str) -> StateOut:
    if app.state.demo:
        raise HTTPException(403, "demo: progress stays in your browser")
    user = learner(request)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:  # commits on success, rolls back on error
        # Fields the body left out keep their stored value (see the top).
        missing = set(State.model_fields) - body.model_fields_set
        if missing:
            row = conn.execute("SELECT done, progress FROM progress WHERE user = ? AND course = ?", (user, course)).fetchone()
            if row is not None:
                stored = State(done=json.loads(row[0]), **json.loads(row[1]))
                body = body.model_copy(update={k: getattr(stored, k) for k in missing})
        progress = Progress(**body.model_dump(exclude={"done"}))
        conn.execute(
            "INSERT INTO progress (user, course, done, progress, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (user, course) DO UPDATE SET done = excluded.done,"
            " progress = excluded.progress, updated_at = excluded.updated_at",
            (user, course, json.dumps(body.done), progress.model_dump_json(), now),
        )
    return StateOut(**body.model_dump(), updated_at=now)


# The page from before there were courses talks to /state. Kept so a tab left
# open across the deploy can still save; it was the first course.
@app.get("/state", response_model=StateOut, include_in_schema=False)
def legacy_get(request: Request) -> StateOut:
    return get_state(request, LEGACY_COURSE)


@app.put("/state", response_model=StateOut, include_in_schema=False)
def legacy_put(body: State, request: Request) -> StateOut:
    return put_state(body, request, LEGACY_COURSE)


# ---- A course: its page, files and state. Last, so the fixed paths above win. ----

@app.get("/{cid}", include_in_schema=False)
def course_bare(cid: str) -> RedirectResponse:
    course_of(cid)
    return RedirectResponse(f"/{cid}/", 308)


@app.get("/{cid}/", include_in_schema=False)
def page(cid: str, request: Request) -> HTMLResponse:
    c = course_of(cid)
    if app.state.demo:
        return HTMLResponse(course_page("", c, "", {"demo": True}), headers=NO_CACHE)
    user = learner(request)
    extra: dict = {"demo": False}
    token = app.state.config.personal(user).mentor_token
    if token:
        extra["mentor_path"] = "/mentor/" + token   # shown in the masthead and on Record, to copy
    return HTMLResponse(course_page(user, c, "", extra), headers=PER_LEARNER)


@app.get("/{cid}/course.json", include_in_schema=False)
def course_file(cid: str) -> JSONResponse:
    # What the page gets injected, for clients that aren't the page (Friday).
    return JSONResponse(course_of(cid).data, headers=NO_CACHE)


@app.get("/{cid}/quizzes.json", include_in_schema=False)
def quizzes(cid: str) -> FileResponse:
    return FileResponse(course_of(cid).dir / "quizzes.json", headers=NO_CACHE)


@app.get("/{cid}/drills.json", include_in_schema=False)
def drills(cid: str) -> JSONResponse:
    return JSONResponse(course_of(cid).drills, headers=NO_CACHE)


@app.get("/{cid}/lessons/{name}", include_in_schema=False)
def lesson(cid: str, name: str) -> FileResponse:
    c = course_of(cid)
    m = re.fullmatch(r"([a-z0-9-]+)\.html", name)
    if not m or m[1] not in c.quizzes:
        raise HTTPException(404)
    return FileResponse(c.dir / "lessons" / name, media_type="text/html; charset=utf-8")


@app.get("/{cid}/state", response_model=StateOut)
def course_get(cid: str, request: Request) -> StateOut:
    return get_state(request, course_of(cid).id)


@app.put("/{cid}/state", response_model=StateOut)
def course_put(cid: str, body: State, request: Request) -> StateOut:
    return put_state(body, request, course_of(cid).id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("STUDY_HOST", "127.0.0.1"),
        port=int(os.environ.get("STUDY_PORT", "8781")),
    )
