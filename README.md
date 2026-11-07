# fastapi-study-plan

A self-hosted, gamified course in Python and FastAPI for PHP developers,
built to get you ready for a backend interview or a live code review in
about three days.

It's one small FastAPI app (the course is a working example of what it
teaches) serving one page:

- **Plan**: a three-day checklist from Python basics to async, FastAPI,
  SQLAlchemy and pytest, with a "next up" button that always points at the
  next thing to do.
- **Syllabus**: 27 lessons (about 9 hours of reading) with code,
  "From PHP" comparisons, common traps, a short exercise and links to the
  official docs.
- **Checks**: three questions after every lesson, with the reason behind
  each answer. Questions you miss come back the next day for review.
- **Drills**: six timed code reviews of realistic snippets with planted bugs,
  and a ranked answer key.
- **Record**: XP, ranks, a daily goal, a streak and badges. All of it is
  computed from what you've actually done, so unticking takes XP back.

Progress is saved in SQLite on the server and in the browser, so it survives
a lost connection, and a service worker caches the lessons for offline
reading.

## Run it

With [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/anishanilkumar/fastapi-study-plan
cd fastapi-study-plan
uv run --with fastapi --with uvicorn python app.py
```

Open http://127.0.0.1:8781. Progress goes to `study.db` next to the app.

With Docker:

```sh
docker build -t study-plan .
docker run -p 8781:8781 -v study-plan-data:/data study-plan
```

## Make it yours

Copy `config.example.json` to `config.json` (gitignored) and edit it:

| Key | What it does |
|---|---|
| `title` | The page heading |
| `start` | Day 1 of the plan (`YYYY-MM-DD`), so days show as dates. Leave it out for "Day 1, Day 2…" |
| `target` | What you're counting down to: `{"label": "Interview", "at": "2026-01-13T14:00:00+01:00"}`. The time needs a UTC offset. Leave it out for no countdown |
| `overrides` | Replace a plan item's text by id, e.g. your own project for the `eve-deepdive` item |

Restart after editing; an invalid config stops the app at startup with the
reason. Environment variables: `STUDY_DB` (SQLite path), `STUDY_CONFIG`
(config path), `STUDY_HOST` and `STUDY_PORT` (default `127.0.0.1:8781`),
`STUDY_DEMO` (see below).

### Public demo mode

`STUDY_DEMO=1` turns an instance into a public demo: no database at all,
each visitor's progress stays in their own browser, and `PUT /state` is
refused. Visitors never see each other's ticks and there's nothing to reset.

## Security

There is **no login**. The app keeps one learner's progress and binds to
localhost by default. To put it on the internet, run it behind a reverse
proxy that handles authentication (oauth2-proxy, Authelia, basic auth in
nginx, Tailscale, …). `PUT /state` only accepts small, validated data, but
without auth anyone with the URL could change your progress.

## How it's built

```
app.py              FastAPI: the page (with config injected), state API, static files
study-plan.html     the whole UI: plain HTML, CSS and JavaScript, no build step
lessons/*.html      one reading per lesson (HTML fragments)
quizzes.json        three questions per lesson: [question, options, answer index, why]
drills.json         the code-review drills and their ranked findings
sw.js               the service worker for offline use
test_app.py         pytest suite
```

The state API is described at the top of `app.py`. A few things are worth
reading as examples of the lessons themselves: plain `def` handlers because
`sqlite3` blocks, a fresh connection per request, Pydantic validation of
everything the page sends, a lifespan handler that migrates old databases,
and an aware-datetime check on the config.

### Adding content

- **A lesson**: add `lessons/<id>.html`, three questions under the same id in
  `quizzes.json`, and an entry in `PARTS` in `study-plan.html`. The tests
  check that all three agree.
- **A drill**: append to `drills.json`. Put the worst finding first; it's the
  one worth the most XP.

### Tests

```sh
uv run --with fastapi --with uvicorn --with httpx --with pytest pytest -q
```

## Deploying to your own server

`deploy.sh` copies the app over SSH and restarts a systemd unit. Copy
`.deploy.env.example` to `.deploy.env` (gitignored) and fill it in.

## Licence

MIT for the code and the course content. The fonts in `fonts/` are under
the SIL Open Font License; see `fonts/README.md`.
