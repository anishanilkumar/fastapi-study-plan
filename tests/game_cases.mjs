// Runs the page's own scoring (study.html) against shared/game-cases.json.
// Friday's copy of the rules (friday: study/Game.kt) is held to the same file.
//
//   TZ=UTC node tests/game_cases.mjs           check every case, exit 1 on a difference
//   TZ=UTC node tests/game_cases.mjs --write   fill in "expect" from the page (then review the diff)
//
// The page runs in a VM with a DOM that absorbs everything, in demo mode (no
// server), with the clock fixed at noon on each case's "today".
import fs from "node:fs";
import vm from "node:vm";

const root = new URL("..", import.meta.url).pathname;
const html = fs.readFileSync(root + "study.html", "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.indexOf("</script>"));
const file = root + "shared/game-cases.json";
const cases = JSON.parse(fs.readFileSync(file, "utf8"));
const write = process.argv.includes("--write");

// Anything the page asks of the DOM: a function, an object, an empty list, all at once.
function stub() {
  const f = function () { return stub(); };
  return new Proxy(f, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => "";
      if (k === Symbol.iterator) return function* () {};
      if (k === "then") return undefined;
      if (k === "length") return 0;
      return stub();
    },
    set() { return true; },
    apply() { return stub(); },
    construct() { return stub(); },
  });
}

const read = (cid, name) => JSON.parse(fs.readFileSync(`${root}courses/${cid}/${name}`, "utf8"));

function run(c) {
  const course = { id: c.course, ...read(c.course, "course.json") };
  const NOW = new Date(c.today + "T12:00:00").getTime();
  class FixedDate extends Date {
    constructor(...a) { if (a.length) super(...a); else super(NOW); }
    static now() { return NOW; }
  }
  const store = {};
  let api = null;
  const ctx = {
    document: stub(), location: { hash: "", origin: "", protocol: "file:", replace() {} }, history: stub(), navigator: {},
    localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } },
    fetch: () => new Promise(() => {}), setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, matchMedia: () => ({ matches: false }),
    Date: FixedDate, console, addEventListener() {}, scrollTo() {}, scrollY: 0,
  };
  ctx.window = ctx;
  ctx.__studyTest = (a) => { api = a; };
  vm.createContext(ctx);
  const cfg = JSON.stringify({ demo: true, course, store: "case" });
  vm.runInContext(script.replace("/*STUDY_CONFIG*/null", cfg), ctx);
  api.setQuizzes(read(c.course, "quizzes.json"));
  api.setDrills(read(c.course, "drills.json"));
  api.adopt(JSON.parse(JSON.stringify(c.state)));
  const s = api.score(), n = api.nextAction();
  return {
    xp: s.xp, max: s.max, rank: api.ranks[s.rank].t, read: s.read, passed: s.passed, perfect: s.perfect,
    planDone: s.planDone, found: s.found, worst: s.worst, drillsDone: s.drillsDone, extraRight: s.extraRight,
    streak: s.streak, longest: s.longest, today: s.today, due: s.due, seen: s.seen, lastRight: s.lastRight, weak: s.weak,
    badges: s.badges.length,
    next: n ? { kind: n.kind, id: n.kind === "drill" ? n.drill.id : n.lesson ? n.lesson.id : n.item ? n.item.id : null } : null,
  };
}

let bad = 0;
for (const c of cases.cases) {
  const got = run(c);
  if (write) { c.expect = got; continue; }
  const want = JSON.stringify(c.expect), have = JSON.stringify(got);
  if (want !== have) { bad++; console.log(`DIFF ${c.name}\n  want ${want}\n  got  ${have}`); }
}
if (write) { fs.writeFileSync(file, JSON.stringify(cases, null, 1) + "\n"); console.log(`wrote ${cases.cases.length} cases`); }
else console.log(bad ? `${bad} of ${cases.cases.length} cases differ` : `all ${cases.cases.length} cases agree`);
process.exit(bad ? 1 : 0);
