#!/usr/bin/env python3
"""The whole suite. No dependencies, no test runner: `python3 tests/test_capacity.py`.

Every test runs against a scratch directory and asserts it is not the real
store before writing anything.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import capture                                            # noqa: E402
import gate                                               # noqa: E402
import ledger                                             # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print("%s  %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("        got  %r\n        want %r" % (got, want))
        FAILS.append(name)


REAL = capture.ledger_path()
SCRATCH = Path(tempfile.mkdtemp(prefix="claude-capacity-test-"))
LEDGER = SCRATCH / "capacity.jsonl"
assert LEDGER != REAL, "refusing to touch the real ledger"
os.environ["CLAUDE_CAPACITY_STORE"] = str(SCRATCH)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat(timespec="seconds")


def payload(five=None, week=None, five_reset=None, week_reset=None,
            context=None, size=200000, cost=None, model="claude-opus-5"):
    body = {}
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five,
                               "resets_at": five_reset}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week,
                               "resets_at": week_reset}
    if limits:
        body["rate_limits"] = limits
    if context is not None:
        body["context_window"] = {"used_percentage": context,
                                  "context_window_size": size}
    if cost is not None:
        body["cost"] = {"total_cost_usd": cost}
    if model is not None:
        body["model"] = {"id": model, "display_name": model}
    return json.dumps(body)


def write(rows, path=None):
    path = path or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row if isinstance(row, str)
                         else json.dumps(row) + "\n")


print("== the payload becomes a row ==")
epoch = int(datetime(2026, 8, 23, 17, 0, 0, tzinfo=timezone.utc).timestamp())
row = capture.build_row(json.loads(payload(five=32.5, week=72, five_reset=epoch,
                                           week_reset=epoch, context=41.2,
                                           cost=1.2345)), now=NOW)
check("the five-hour percentage survives as a float",
      row["five_hour_pct"], 32.5)
check("an epoch reset becomes an offset-aware stamp",
      ledger._instant(row["five_hour_reset"]),
      datetime(2026, 8, 23, 17, 0, 0, tzinfo=timezone.utc))
check("the context window is captured", row["context_pct"], 41.2)
check("the session cost is captured", row["cost_usd"], 1.2345)
check("the row carries its schema version", row["v"], capture.SCHEMA)

print("\n== what the payload does not send is absent, never zero ==")
bare = capture.build_row(json.loads(payload()), now=NOW)
check("no rate_limits means no window keys at all",
      [k for k in bare if k.endswith("_pct")], [])
check("a missing window is not a window at zero",
      bare.get("five_hour_pct"), None)
half = capture.build_row(json.loads(payload(five=10, five_reset=epoch)), now=NOW)
check("one window present, the other absent",
      ("five_hour_pct" in half, "seven_day_pct" in half), (True, False))

print("\n== rubbish in the payload never raises and never invents ==")
check("a non-numeric percentage is dropped",
      capture._pct("lots"), None)
check("a boolean is not a percentage", capture._pct(True), None)
check("a negative percentage is dropped", capture._pct(-1), None)
check("infinity is dropped", capture._pct(float("inf")), None)
check("a percentage above 100 is capped, not dropped -- a full window must "
      "never vanish", capture._pct(140), 100.0)
check("a naive reset stamp is refused, because its instant is unknowable",
      capture._reset("2026-08-23T17:00:00"), None)
check("a Z-suffixed stamp is accepted",
      ledger._instant(capture._reset("2026-08-23T17:00:00Z")),
      datetime(2026, 8, 23, 17, 0, 0, tzinfo=timezone.utc))
check("a zero epoch is refused", capture._reset(0), None)
check("a list is not a reset", capture._reset([1, 2]), None)
check("a stamp near the year 1 is refused rather than overflowing astimezone",
      capture._reset("0001-01-01T00:00:00Z"), None)
check("and it does not take the render down with it -- the percentage survives",
      capture.render('{"rate_limits":{"five_hour":{"used_percentage":50,'
                     '"resets_at":"0001-01-01T00:00:00Z"}}}', path=LEDGER),
      "5h 50%")
check("a reset further ahead than any real window is refused (epoch)",
      capture._reset(253402300799), None)
check("a reset further ahead than any real window is refused (ISO)",
      capture._reset("9999-12-31T23:59:59Z"), None)
check("while a reset inside the horizon is kept",
      capture._reset(iso(NOW + timedelta(days=7)), now=NOW) is not None, True)
long_model = capture.build_row(json.loads(payload(five=1, five_reset=epoch,
                                                  model="m" * 500)), now=NOW)
check("an absurd model id is capped",
      len(long_model["model_id"]), capture.MODEL_ID_MAX)
check("garbage in place of a payload gives the fallback line, not a traceback",
      capture.render("{not json", path=LEDGER), capture.FALLBACK)
check("a JSON array instead of an object gives the fallback",
      capture.render("[1,2,3]", path=LEDGER), capture.FALLBACK)

print("\n== the row is bounded so concurrent appends cannot interleave ==")
huge = dict(row)
huge["model_id"] = "m" * 400
encoded = capture._encode(huge)
check("an over-long row sheds optional fields rather than being dropped",
      encoded is not None and len(encoded) < capture.MAX_ROW_BYTES, True)
shed = json.loads(encoded.decode("utf-8"))
check("the quota numbers are never what gets shed",
      ("five_hour_pct" in shed, "seven_day_pct" in shed), (True, True))

print("\n== the display line ==")
check("the line reads as one glance",
      capture.line({"five_hour_pct": 32, "seven_day_pct": 72,
                    "context_pct": 41}),
      "5h 32%  7d 72%  ctx 41%")
check("an empty row still returns something printable",
      capture.line({}), capture.FALLBACK)
# NOW is a Sunday. Asserted as the whole string, because the previous version of
# this test asserted that "so" was absent from a line containing a capitalised
# weekday -- true for every locale on earth, and so a test that could not fail.
# It is only "Sun" because the weekday comes from strftime, which follows the
# process locale, and a status line runs in the default one. Measured: under
# de_DE.UTF-8 the same row renders "7d 1% So". That is a property of the
# locale, not a claim this makes.
check("the seven-day segment carries the reset weekday",
      capture.line({"seven_day_pct": 1, "seven_day_reset": iso(NOW)}),
      "7d 1% Sun")

print("\n== the public surface never raises on what its own docstrings invite ==")
# Every one of these raised once. The `now` parameters are the ones a reader hits
# first: they are documented and public, and the obvious thing to pass is a bare
# datetime.now(), which is naive -- while capacity() promises "always a dict,
# never an exception". The rest are shapes a ledger, which this project calls an
# ingest surface, can genuinely hold.
NAIVE = NOW.replace(tzinfo=None)
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 10,
        "five_hour_reset": iso(NOW + timedelta(hours=2))}])
for name, call in (
        ("render(None)", lambda: capture.render(None, path=LEDGER)),
        ("render(a number)", lambda: capture.render(7, path=LEDGER)),
        ("line(not a dict)", lambda: capture.line(None)),
        ("line(percentage as a string)", lambda: capture.line({"five_hour_pct": "40"})),
        ("line(reset as a number)",
         lambda: capture.line({"five_hour_pct": 40, "five_hour_reset": 12345})),
        ("build_row(reset near year 1)",
         lambda: capture.build_row({"rate_limits": {"five_hour": {
             "used_percentage": 50, "resets_at": "0001-01-01T00:00:00Z"}}})),
        ("reading(now=naive)", lambda: ledger.reading(path=LEDGER, now=NAIVE)),
        ("capacity(now=naive)", lambda: gate.capacity(path=LEDGER, now=NAIVE)),
        ("append(not a dict)", lambda: capture.append(None, path=LEDGER))):
    try:
        call()
        raised = None
    except Exception as exc:                                    # noqa: BLE001
        raised = "%s: %s" % (type(exc).__name__, exc)
    check("%s answers instead of raising" % name, raised, None)
check("a naive now is read as UTC rather than refused",
      gate.capacity(path=LEDGER, now=NAIVE)["checked_at"],
      iso(NOW))

print("\n== a store that cannot be written costs the row, never the line ==")
blocked = SCRATCH / "a-file"
blocked.write_text("not a directory")
text = capture.render(payload(five=12, five_reset=epoch),
                      path=blocked / "nested" / "capacity.jsonl")
check("the real line is still returned when the append fails, not the fallback",
      text.startswith("5h 12%"), True)
check("the failure is reported as a word, never raised",
      capture.append({"v": 1, "ts": iso(NOW)},
                     path=blocked / "nested" / "x.jsonl"), "failed")

print("\n== the throttle, and what outranks it ==")
LEDGER.unlink(missing_ok=True)
first = capture.append({"v": 1, "ts": iso(NOW), "five_hour_pct": 10,
                        "five_hour_reset": iso(NOW)}, path=LEDGER)
second = capture.append({"v": 1, "ts": iso(NOW), "five_hour_pct": 10,
                         "five_hour_reset": iso(NOW)}, path=LEDGER)
third = capture.append({"v": 1, "ts": iso(NOW), "five_hour_pct": 14,
                        "five_hour_reset": iso(NOW)}, path=LEDGER)
check("the first row is written", first, "written")
check("an unchanged row inside the minute is throttled", second, "throttled")
check("a percentage that moved outranks the throttle", third, "written")

print("\n== the selection rule: a window, not a row ==")
soon = NOW + timedelta(hours=2)
later = NOW + timedelta(days=2)
past = NOW - timedelta(hours=1)
write([
    # An older row whose seven-day reading is still valid.
    {"v": 1, "ts": iso(NOW - timedelta(minutes=30)),
     "five_hour_pct": 20, "five_hour_reset": iso(past),
     "seven_day_pct": 58, "seven_day_reset": iso(later)},
    # A newer row, but its seven-day window has rolled over.
    {"v": 1, "ts": iso(NOW - timedelta(minutes=1)),
     "five_hour_pct": 5, "five_hour_reset": iso(soon),
     "seven_day_pct": 3, "seven_day_reset": iso(past)},
])
found = ledger.reading(path=LEDGER, now=NOW)
check("the expired seven-day reading in the NEWER row is discarded",
      found["seven_day"]["pct"], 58)
check("while the newer five-hour reading is taken from the same row",
      found["five_hour"]["pct"], 5)

print("\n== many rows share one reset, and the newest of them must win ==")
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=m)),
        "seven_day_pct": pct, "seven_day_reset": iso(later)}
       for m, pct in ((50, 15), (30, 33), (2, 58))])
found = ledger.reading(path=LEDGER, now=NOW)
check("58 per cent, not the 15 that a first-match rule would keep",
      found["seven_day"]["pct"], 58)

print("\n== two renders in the same second, which the throttle allows ==")
# `ts` has one-second resolution, and a percentage that moved by a point or more
# outranks the throttle -- so this pair is what an ordinary capture produces, not
# a hand-edited ledger. With a two-part key and a strict >, the row seen first
# wins, and in an append-only file that is the older, lower reading: a false go.
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=1)),
        "five_hour_pct": pct, "five_hour_reset": iso(soon)}
       for pct in (70, 85)])
found = ledger.reading(path=LEDGER, now=NOW)
check("identical timestamps: the later WRITE wins, not the earlier one",
      found["five_hour"]["pct"], 85)
check("and the gate is not fooled into a go by the stale half of the pair",
      gate.capacity(path=LEDGER, now=NOW, max_pct=80)["verdict"], gate.WAIT)
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=1)),
        "five_hour_pct": pct, "five_hour_reset": iso(soon)}
       for pct in (85, 70)])
check("reversed on disk, the last line still wins -- file order is write order",
      ledger.reading(path=LEDGER, now=NOW)["five_hour"]["pct"], 70)

print("\n== one absurd reset must not outrank every genuine row ==")
# The reset ranks first, so a row claiming to reset in the year 9999 outranks
# everything for as long as it sits in the tail: first serving its own stale
# percentage, then jamming the answer at not-known once it ages past --max-age.
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 3,
        "five_hour_reset": "9999-12-31T23:59:59+00:00"},
       {"v": 1, "ts": iso(NOW), "five_hour_pct": 97,
        "five_hour_reset": iso(soon)}])
found = ledger.reading(path=LEDGER, now=NOW)
check("the genuine 97 per cent wins over the poisoned row's 3 per cent",
      found["five_hour"]["pct"], 97)
check("and the poisoned row is counted as unreadable, not silently dropped",
      found["health"]["unreadable"], 1)
check("so the gate holds the work back",
      gate.capacity(path=LEDGER, now=NOW, max_pct=80)["verdict"], gate.WAIT)

print("\n== a naive stamp sorts oldest, exactly as the docstring says ==")
check("_instant reads a naive stamp as unplaceable, not as UTC",
      ledger._instant("2026-08-23T17:00:00"),
      datetime.min.replace(tzinfo=timezone.utc))
check("which is what capture already does with one in a payload",
      capture._reset("2026-08-23T17:00:00"), None)
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 5,
        "five_hour_reset": "2026-08-23T17:00:00"}])
check("so a naive reset is never answered on with a guessed zone",
      ledger.reading(path=LEDGER, now=NOW), None)

print("\n== the newest cost and model, by time rather than by file position ==")
write([{"v": 1, "ts": iso(NOW), "context_pct": 90, "cost_usd": 9.99,
        "model_id": "new-model", "five_hour_pct": 5,
        "five_hour_reset": iso(soon)},
       {"v": 1, "ts": iso(NOW - timedelta(hours=1)), "context_pct": 10,
        "cost_usd": 0.01, "model_id": "old-model"}])
found = ledger.reading(path=LEDGER, now=NOW)
check("the context percentage comes from the newest row",
      found["context_pct"], 90)
check("and so does the cost, not from whatever came last in the file",
      found["cost_usd"], 9.99)
check("and so does the model id", found["model_id"], "new-model")

print("\n== the offset changes twice a year and string order breaks ==")
write([
    {"v": 1, "ts": "2026-10-25T01:30:00+02:00",
     "seven_day_pct": 10, "seven_day_reset": iso(later)},
    {"v": 1, "ts": "2026-10-25T01:00:00+01:00",
     "seven_day_pct": 90, "seven_day_reset": iso(later)},
])
found = ledger.reading(path=LEDGER, now=NOW)
check("the genuinely later instant wins, though it sorts earlier as a string",
      found["seven_day"]["pct"], 90)

print("\n== a public ledger is an ingest surface ==")
write("".join([
    json.dumps({"v": 1, "ts": iso(NOW), "five_hour_pct": 40,
                "five_hour_reset": iso(soon)}) + "\n",
    "{half a row and then the power went\n",
    "42\n",
    '["a list is not a row"]\n',
    '"a string is not a row"\n',
    "\n",
    json.dumps({"v": 1, "ts": iso(NOW), "seven_day_pct": 60,
                "seven_day_reset": iso(later)}) + "\n",
]))
found = ledger.reading(path=LEDGER, now=NOW)
check("the good rows are still read", (found["five_hour"]["pct"],
                                       found["seven_day"]["pct"]), (40, 60))
check("and the unreadable ones are counted, not hidden",
      found["health"]["unreadable"], 4)
write([{"ts": None, "five_hour_pct": 1, "five_hour_reset": 12345},
       {"ts": 99, "five_hour_pct": "x", "five_hour_reset": iso(soon)}])
check("rows whose types are wrong are skipped without raising",
      ledger.reading(path=LEDGER, now=NOW), None)

print("\n== silence is reported as silence, never as zero ==")
LEDGER.unlink(missing_ok=True)
check("no ledger at all reads as nothing known",
      ledger.reading(path=LEDGER, now=NOW), None)
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 90,
        "five_hour_reset": iso(past)}])
check("a ledger holding only expired windows also reads as nothing known",
      ledger.reading(path=LEDGER, now=NOW), None)

print("\n== the gate ==")
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=2)),
        "five_hour_pct": 32, "five_hour_reset": iso(soon),
        "seven_day_pct": 72, "seven_day_reset": iso(later)}])
answer = gate.capacity(path=LEDGER, now=NOW, max_pct=80)
check("under the limit is a go", answer["verdict"], gate.GO)
check("and it says how long until the window turns over",
      answer["five_hour"]["minutes_until_reset"], 120.0)
answer = gate.capacity(path=LEDGER, now=NOW, max_pct=70)
check("at or past the limit is a wait", answer["verdict"], gate.WAIT)
check("naming the window that decided it",
      "seven-day" in answer["why"], True)
answer = gate.capacity(path=LEDGER, now=NOW, max_pct=80, max_context_pct=50)
check("no context reading means the context rule cannot fire",
      answer["verdict"], gate.GO)
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=2)),
        "five_hour_pct": 32, "five_hour_reset": iso(soon),
        "context_pct": 88}])
answer = gate.capacity(path=LEDGER, now=NOW, max_pct=80, max_context_pct=50)
check("a full context window can hold work back when asked to",
      answer["verdict"], gate.WAIT)

print("\n== fail closed on silence and on staleness ==")
LEDGER.unlink(missing_ok=True)
answer = gate.capacity(path=LEDGER, now=NOW)
check("nothing known is not permission to start",
      answer["verdict"], gate.UNKNOWN)
write([{"v": 1, "ts": iso(NOW - timedelta(hours=6)),
        "five_hour_pct": 2, "five_hour_reset": iso(later)}])
answer = gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30)
check("a six-hour-old reading of 2 per cent is not a go",
      answer["verdict"], gate.UNKNOWN)
check("and it says the status line may not be running",
      "status line" in answer["why"], True)

# When the two windows are not equally stale, the message has to name which one
# it is talking about. It used to report the OLDEST age and call it "the newest
# reading", so a five-hour reading 40 minutes old beside a seven-day one 400
# minutes old was announced as "the newest reading is 400 minutes old".
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=400)),
        "seven_day_pct": 20, "seven_day_reset": iso(later)},
       {"v": 1, "ts": iso(NOW - timedelta(minutes=40)),
        "five_hour_pct": 12, "five_hour_reset": iso(soon)}])
answer = gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30)
check("an unevenly stale ledger names the window it is talking about",
      "seven-day reading is 400 minutes old" in answer["why"], True)
check("and does not describe the oldest reading as the newest one",
      "newest reading" in answer["why"], False)

print("\n== the exit codes, which are the whole interface ==")
env = dict(os.environ, CLAUDE_CAPACITY_STORE=str(SCRATCH))
write([{"v": 1, "ts": iso(datetime.now(timezone.utc)),
        "five_hour_pct": 10,
        "five_hour_reset": iso(datetime.now(timezone.utc)
                               + timedelta(hours=2))}])


def run(*args):
    return subprocess.run([sys.executable, str(HERE.parent / "gate.py")] + list(args),
                          capture_output=True, text=True, env=env)


check("plenty of room exits 0", run("check", "--quiet").returncode, 0)
check("past the limit exits 1",
      run("check", "--max-pct", "5", "--quiet").returncode, 1)
# A bare invocation used to mean `show`, which exits 0 whatever it finds -- so
# `gate.py && ./job` launched the job on an empty ledger, one dropped word away
# from the documented `gate.py check && ./job`. A tool that sells failing closed
# must not have a spelling of itself that always says go.
check("a bare invocation is not a silent go",
      run().returncode != 0, True)
check("it asks which subcommand you meant",
      "check" in run().stderr, True)
LEDGER.unlink(missing_ok=True)
check("and with an empty ledger a bare invocation still does not say go",
      run().returncode != 0, True)
check("no reading exits 1 by default",
      run("check", "--quiet").returncode, 1)
check("unless you deliberately allow the unknown",
      run("check", "--quiet", "--allow-unknown").returncode, 0)
check("register prints the settings entry and writes nothing",
      "statusLine" in run("register").stdout, True)

# The point of `register` is that it prints and does not edit, so the test has to
# watch a real settings.json. The previous version asserted LEDGER.exists() is
# False -- a different file entirely, and one the line above had just deleted, so
# it was true before `register` ran and would have stayed true had `register`
# overwritten the user's settings.
FAKE_HOME = SCRATCH / "home"
(FAKE_HOME / ".claude").mkdir(parents=True, exist_ok=True)
SETTINGS = FAKE_HOME / ".claude" / "settings.json"
UNTOUCHED = '{"statusLine": {"type": "command", "command": "my-own-thing"}}'
SETTINGS.write_text(UNTOUCHED)
home_env = dict(env, HOME=str(FAKE_HOME), USERPROFILE=str(FAKE_HOME))
printed = subprocess.run([sys.executable, str(HERE.parent / "gate.py"), "register"],
                         capture_output=True, text=True, env=home_env)
check("register names the settings file it wants you to edit",
      str(SETTINGS) in printed.stdout, True)
check("register really does not touch settings.json",
      SETTINGS.read_text(), UNTOUCHED)

print("\n== concurrent renders do not tear the file ==")
LEDGER.unlink(missing_ok=True)
script = SCRATCH / "one_render.py"
script.write_text(
    "import sys, json\n"
    "sys.path.insert(0, %r)\n"
    "import capture\n"
    "pct = float(sys.argv[1])\n"
    "reset = int(sys.argv[2])\n"
    "print(capture.render(json.dumps({'rate_limits': {'five_hour': "
    "{'used_percentage': pct, 'resets_at': reset}}, 'model': "
    "{'id': 'claude-opus-5'}})))\n" % str(HERE.parent))
future = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
procs = [subprocess.Popen([sys.executable, str(script), str(i * 7), str(future)],
                          env=env, stdout=subprocess.DEVNULL)
         for i in range(1, 13)]
for proc in procs:
    proc.wait()
raw = LEDGER.read_text().splitlines()
torn = 0
for text in raw:
    if not text.strip():
        continue
    try:
        json.loads(text)
    except ValueError:
        torn += 1
check("twelve simultaneous renders left no torn line", torn, 0)
check("and at least one of them was recorded", len(raw) >= 1, True)

print("\n== rotation keeps the newest and never loses the file ==")
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=n)), "five_hour_pct": 1,
        "five_hour_reset": iso(soon), "pad": "x" * 200} for n in range(400, 0, -1)])
check("a small ledger is left alone",
      ledger.compact(path=LEDGER, max_bytes=10 ** 9), "small-enough")
before = len(LEDGER.read_text().splitlines())
check("a large one is compacted",
      ledger.compact(path=LEDGER, max_bytes=1000, keep=50), "compacted")
after = LEDGER.read_text().splitlines()
check("down to the rows asked for", len(after), 50)
check("keeping the newest, not the oldest",
      json.loads(after[-1])["ts"], iso(NOW - timedelta(minutes=1)))
check("and the ledger is still readable afterwards",
      ledger.reading(path=LEDGER, now=NOW)["five_hour"]["pct"], 1)
check("(it really did start larger)", before > 50, True)

# `rows[-keep:]` at keep=0 is `rows[0:]` -- every row, reported as 'compacted'.
KEEP_LEDGER = SCRATCH / "keep.jsonl"
for asked, expected in ((0, 0), (-5, 0), (10, 10)):
    write(['{"v":1}\n'] * 300, path=KEEP_LEDGER)
    outcome = ledger.compact(path=KEEP_LEDGER, max_bytes=100, keep=asked)
    check("compact(keep=%d) leaves %d row(s), not 300" % (asked, expected),
          (outcome, len(KEEP_LEDGER.read_text().splitlines())),
          ("compacted", expected))

# A failed compaction must not leave a partial copy of the ledger behind. The
# failure most likely to cause one is a full disk, which is when the leak costs
# most, so the temp write is made to fail the way a full disk fails.
FAIL_LEDGER = SCRATCH / "fail.jsonl"
write([{"v": 1, "ts": iso(NOW), "pad": "x" * 200} for _ in range(140)],
      path=FAIL_LEDGER)
before_files = set(p.name for p in SCRATCH.iterdir())
real_dumps = json.dumps


def _no_space(*args, **kwargs):
    raise OSError(28, "No space left on device")


json.dumps = _no_space
try:
    outcome = ledger.compact(path=FAIL_LEDGER, max_bytes=100, keep=50)
finally:
    json.dumps = real_dumps
check("a compaction that cannot write says so", outcome, "failed")
check("the original ledger survives it whole",
      len(FAIL_LEDGER.read_text().splitlines()), 140)
check("and no half-written copy is left behind in the store",
      set(p.name for p in SCRATCH.iterdir()) - before_files, set())

print("\n== a row appended during a compaction is not lost ==")
# The lock lives on an inode, not on a name. compact replaces the ledger by
# rename, so an appender blocked on the lock can wake holding it on a file that
# has just been unlinked -- writing its row into an orphan and returning
# 'written'. compact is slowed here so the race is certain rather than lucky.
RACE_LEDGER = SCRATCH / "race.jsonl"
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 1,
        "five_hour_reset": iso(soon), "pad": "x" * 300} for _ in range(500)],
      path=RACE_LEDGER)
real_rows = ledger._rows


def _slow_rows(path, tail=ledger.TAIL_ROWS):
    got = real_rows(path, tail=tail)
    time.sleep(1.0)
    return got


outcome = {}
ledger._rows = _slow_rows
try:
    worker = threading.Thread(
        target=lambda: outcome.update(
            compact=ledger.compact(path=RACE_LEDGER, max_bytes=100, keep=50)))
    worker.start()
    time.sleep(0.3)                       # let compact take the lock first
    outcome["append"] = capture.append(
        {"v": 1, "ts": iso(NOW), "five_hour_pct": 99,
         "five_hour_reset": iso(soon), "model_id": "survivor"},
        path=RACE_LEDGER)
    worker.join()
finally:
    ledger._rows = real_rows
check("the compaction ran", outcome.get("compact"), "compacted")
check("the append reported success", outcome.get("append"), "written")
check("and 'written' meant it -- the row is in the file that survived",
      "survivor" in RACE_LEDGER.read_text(), True)
check("so the reader can actually see it",
      ledger.reading(path=RACE_LEDGER, now=NOW)["five_hour"]["pct"], 99)

shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
