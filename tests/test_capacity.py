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
            context=None, size=200000, cost=None, model="claude-opus-5",
            windows=None):
    body = {}
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five,
                               "resets_at": five_reset}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week,
                               "resets_at": week_reset}
    # Anything else the payload carries, named by whoever sent it: `windows` is
    # {name: (percentage, reset)}, and a value that is not a pair is passed
    # through untouched so a test can send a window that is not an object.
    for name, sent in (windows or {}).items():
        limits[name] = ({"used_percentage": sent[0], "resets_at": sent[1]}
                        if isinstance(sent, tuple) else sent)
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
        # build_row reached straight into the payload with .get, so anything
        # that was not a dict came back as AttributeError from inside a module
        # whose first stated property is that it may never raise. Every other
        # entry point in that file already guards this.
        ("build_row(a number)", lambda: capture.build_row(7)),
        ("build_row(a list)", lambda: capture.build_row([1, 2])),
        ("build_row(None)", lambda: capture.build_row(None)),
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
check("a payload that is not an object becomes a row with no readings in it",
      [k for k in capture.build_row(7, now=NOW) if k.endswith("_pct")], [])

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

print("\n== across a window boundary, and why the reset ranks first ==")
# The one shape where the reset and the observation time could pull apart: the
# older row is the end of the window that has just run out, the newer row the
# start of the one that replaced it. The new window's reset is LATER and its
# percentage much LOWER, which is why ranking the reset first is safe here --
# the two orderings agree, because a reset never moves backwards for a window.
# Measured 2026-08-23, enumerating every three-row ledger whose resets are
# non-decreasing: 4,320 ledgers, 0 answers on which reset-first and
# newest-first disagree. Reopen if a window's reset is ever seen to jitter
# between renders; reset-first would then start preferring an older row.
rolled = NOW + timedelta(hours=5)
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=3)), "five_hour_pct": 96,
        "five_hour_reset": iso(NOW + timedelta(minutes=1))},
       {"v": 1, "ts": iso(NOW - timedelta(minutes=1)), "five_hour_pct": 4,
        "five_hour_reset": iso(rolled)}])
found = ledger.reading(path=LEDGER, now=NOW)
check("the new window's 4 per cent answers, not the spent window's 96",
      found["five_hour"]["pct"], 4)
check("and the reset reported is the new window's",
      found["five_hour"]["reset"], iso(rolled))
check("so the gate lets the work start",
      gate.capacity(path=LEDGER, now=NOW, max_pct=80)["verdict"], gate.GO)
# One minute later the spent window's row is not even a candidate.
check("and once the old reset has passed, that row is simply gone",
      ledger.reading(path=LEDGER,
                     now=NOW + timedelta(minutes=2))["five_hour"]["pct"], 4)

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

# Same second, two rows -- which is what an ordinary capture produces, not a
# hand-edited ledger: `ts` has one-second resolution and a percentage that moved
# by a point or more outranks the throttle. The window selection was given a
# file-position tie-break for exactly this; these three were left on a strict >
# of the timestamp alone, which keeps the row seen FIRST, and in an append-only
# file that is the older write.
write([{"v": 1, "ts": iso(NOW), "context_pct": 10, "cost_usd": 0.01,
        "model_id": "old-model", "five_hour_pct": 70,
        "five_hour_reset": iso(soon)},
       {"v": 1, "ts": iso(NOW), "context_pct": 90, "cost_usd": 9.99,
        "model_id": "new-model", "five_hour_pct": 85,
        "five_hour_reset": iso(soon)}])
found = ledger.reading(path=LEDGER, now=NOW)
check("identical timestamps: the context reading is the later WRITE's",
      found["context_pct"], 90)
check("identical timestamps: so is the cost", found["cost_usd"], 9.99)
check("identical timestamps: so is the model id", found["model_id"], "new-model")
check("all four readings settle a tie the same way",
      found["five_hour"]["pct"], 85)

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

print("\n== every window the payload sends, not the two that are documented ==")
# Claude Code 2.1.237 sends FOUR windows -- five_hour, seven_day,
# seven_day_opus and seven_day_sonnet -- while the status-line documentation
# lists two (github.com/anthropics/claude-code/issues/88137, filed 2026-08-20).
# A window this tool does not read is a window it cannot gate on, and the
# direction that fails is the expensive one: the weekly Opus allowance is nearly
# spent, both documented windows have room, and the gate says GO into a wall.
four = capture.build_row(json.loads(payload(
    five=32, week=40, five_reset=epoch, week_reset=epoch,
    windows={"seven_day_opus": (96, epoch),
             "seven_day_sonnet": (12, epoch)})), now=NOW)
check("all four windows are recorded, under the payload's own names",
      sorted(k for k in four if k.endswith("_pct")),
      ["five_hour_pct", "seven_day_opus_pct", "seven_day_pct",
       "seven_day_sonnet_pct"])
check("and each one keeps its reset beside it",
      sorted(k for k in four if k.endswith("_reset")),
      ["five_hour_reset", "seven_day_opus_reset", "seven_day_reset",
       "seven_day_sonnet_reset"])
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=2)),
        "five_hour_pct": 32, "five_hour_reset": iso(soon),
        "seven_day_pct": 40, "seven_day_reset": iso(later),
        "seven_day_opus_pct": 96, "seven_day_opus_reset": iso(later),
        "seven_day_sonnet_pct": 12, "seven_day_sonnet_reset": iso(later)}])
answer = gate.capacity(path=LEDGER, now=NOW, max_pct=80)
check("a nearly-spent per-model window holds the work back",
      answer["verdict"], gate.WAIT)
check("and the verdict names the window that decided it",
      "seven-day-opus" in answer["why"], True)
check("the answer lists every window found, not a fixed pair",
      sorted(answer["windows"]),
      ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"])
check("and so does the printed form",
      all(name in gate._human(answer)
          for name in ("five-hour", "seven-day-opus", "seven-day-sonnet")),
      True)
check("the documented two still answer under their old keys as well",
      (answer["five_hour"]["pct"], answer["seven_day"]["pct"]), (32, 40))
check("and those keys cannot drift, being the very same objects",
      answer["five_hour"] is answer["windows"]["five_hour"], True)
found = ledger.reading(path=LEDGER, now=NOW)
check("the reading lists them too", sorted(found["windows"]),
      ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"])
check("the documented pair is listed first, then the rest by name",
      list(found["windows"]),
      ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"])
check("a GO names every live window it checked",
      sorted(w for w in ("five-hour", "seven-day", "seven-day-opus",
                         "seven-day-sonnet")
             if w in gate.capacity(path=LEDGER, now=NOW, max_pct=99)["why"]),
      ["five-hour", "seven-day", "seven-day-opus", "seven-day-sonnet"])
# Asserted on a row with resets written out in full rather than on `four`,
# whose resets are rendered in whatever zone the machine running this is in.
check("and the status line shows all four rather than choosing for you",
      capture.line({"five_hour_pct": 32, "five_hour_reset": iso(NOW),
                    "seven_day_pct": 40, "seven_day_reset": iso(NOW),
                    "seven_day_opus_pct": 96, "seven_day_sonnet_pct": 12,
                    "context_pct": 41}),
      "5h 32% 12:00  7d 40% Sun  7d opus 96%  7d sonnet 12%  ctx 41%")
# A payload with no documented window at all: the whole row used to be thrown
# away, because the decision to append tested for the two names it knew.
LEDGER.unlink(missing_ok=True)
only_opus = capture.render(payload(windows={"seven_day_opus": (91, epoch)}),
                           path=LEDGER)
check("a payload carrying only per-model windows is still recorded",
      json.loads(LEDGER.read_text().splitlines()[-1])["seven_day_opus_pct"], 91)
check("and still shown", only_opus, "7d opus 91%")

print("\n== a ledger written before per-model windows existed ==")
# Written by hand exactly as the old capture wrote it -- the point of the test
# is that this file is not migrated, converted or re-keyed on the way in.
write("".join([
    '{"v":1,"ts":"%s","five_hour_pct":32.5,"five_hour_reset":"%s",'
    '"seven_day_pct":72,"seven_day_reset":"%s","context_pct":41.2,'
    '"context_size":200000,"cost_usd":1.2345,"model_id":"claude-opus-5"}\n'
    % (iso(NOW - timedelta(minutes=2)), iso(soon), iso(later))]))
found = ledger.reading(path=LEDGER, now=NOW)
check("an old two-window ledger reads exactly as it always did",
      (found["five_hour"]["pct"], found["seven_day"]["pct"],
       found["context_pct"], found["cost_usd"], found["model_id"]),
      (32.5, 72, 41.2, 1.2345, "claude-opus-5"))
check("and it holds two windows, not four and not a context one",
      list(found["windows"]), ["five_hour", "seven_day"])
check("context_pct has no reset, so it never becomes a window",
      [w for w in found["windows"] if "context" in w], [])
check("the gate answers on it unchanged",
      gate.capacity(path=LEDGER, now=NOW, max_pct=80)["verdict"], gate.GO)
check("and prints the same two-column layout it always printed",
      gate._human(gate.capacity(path=LEDGER, now=NOW, max_pct=80)).splitlines()[1],
      "  five-hour   32.5%  resets in 120 min  measured 2 min ago")

print("\n== a window name is untrusted input becoming a key ==")
hostile = {
    "": (50, epoch),                          # empty
    "  ": (50, epoch),                        # whitespace
    "a" * 200: (50, epoch),                   # longer than any real name
    "seven_day/../../etc": (50, epoch),       # path-ish
    'quote"key': (50, epoch),                 # would need escaping
    "new\nline": (50, epoch),
    "null\x00byte": (50, epoch),
    "sevén_day": (50, epoch),            # not ASCII
    "_leading": (50, epoch),
    "trailing_": (50, epoch),
    "context": (50, epoch),                   # would collide with context_pct
    "weird_pct": (50, epoch),                 # would become weird_pct_pct
    "weird_reset": (50, epoch),
}
row = capture.build_row(json.loads(payload(five=10, five_reset=epoch,
                                           windows=hostile)), now=NOW)
check("not one hostile name becomes a key",
      sorted(k for k in row if k.endswith("_pct")), ["five_hour_pct"])
check("and the good window beside them is unharmed", row["five_hour_pct"], 10)
check("the row still serialises to one line", capture._encode(row) is not None,
      True)
plausible = capture.build_row(json.loads(payload(
    windows={"seven_day_haiku_45": (10, epoch)})), now=NOW)
check("a plausible undocumented name IS accepted -- this is a filter, not a "
      "whitelist", plausible.get("seven_day_haiku_45_pct"), 10.0)
check("a window whose value is not an object is skipped, not crashed on",
      [k for k in capture.build_row(json.loads(payload(
          windows={"seven_day_opus": 99, "seven_day_sonnet": None})),
          now=NOW) if k.endswith("_pct")], [])
# The reader applies the same filter, because the ledger is an ingest surface.
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 20,
        "five_hour_reset": iso(soon),
        ("z" * 200) + "_pct": 99, ("z" * 200) + "_reset": iso(soon),
        "context_pct": 5, "context_reset": iso(soon)}])
found = ledger.reading(path=LEDGER, now=NOW)
check("a hand-edited ledger cannot put an absurd name in an answer",
      list(found["windows"]), ["five_hour"])
check("nor can it turn the context reading into a quota window",
      found["context_pct"], 5)

# A window whose reset cannot be read is a window with NO LIVE READING, which is
# said out loud. Dropping it from the answer instead would leave a window at 96%
# missing from the list -- absent in the direction that reads as room.
write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 20,
        "five_hour_reset": iso(soon),
        "seven_day_opus_pct": 96, "seven_day_opus_reset": "not a time"}])
found = ledger.reading(path=LEDGER, now=NOW)
check("a window with an unreadable reset is still named",
      list(found["windows"]), ["five_hour", "seven_day_opus"])
check("but it has no live reading, so it decides nothing",
      found["windows"]["seven_day_opus"], None)
answer = gate.capacity(path=LEDGER, now=NOW, max_pct=80)
check("the gate answers on the window it can read", answer["verdict"], gate.GO)
check("and prints the other one as unread rather than hiding it",
      [l.split() for l in gate._human(answer).splitlines()
       if l.strip().startswith("seven-day-opus")],
      [["seven-day-opus", "no", "live", "reading"]])

print("\n== how many windows one payload may decide ==")
many = dict(("w%02d" % n, (float(n), epoch)) for n in range(1, 21))
row = capture.build_row(json.loads(payload(windows=many)), now=NOW)
check("a payload cannot decide how many windows a row carries",
      len(capture.window_names(row)), capture.MAX_WINDOWS)
check("and what survives the cap is the fullest, never the first sent",
      sorted(round(row[k]) for k in row if k.endswith("_pct")),
      list(range(21 - capture.MAX_WINDOWS, 21)))
check("the tail may hold more names than any one payload sent",
      ledger.MAX_WINDOW_NAMES > capture.MAX_WINDOWS, True)
# Past the reader's cap too, and the excess is reported rather than dropped in
# silence -- a window nobody mentions is the failure this whole change is about.
write([dict([("v", 1), ("ts", iso(NOW))] +
            [("w%03d_pct" % n, 1) for n in range(200)] +
            [("w%03d_reset" % n, iso(soon)) for n in range(200)])])
found = ledger.reading(path=LEDGER, now=NOW)
check("one read accumulates no more names than its cap",
      len(found["windows"]), ledger.MAX_WINDOW_NAMES)
check("and says how many it had to refuse",
      found["health"]["windows_ignored"], 200 - ledger.MAX_WINDOW_NAMES)
check("which the printed answer states out loud",
      "past the cap" in gate._human(gate.capacity(path=LEDGER, now=NOW)), True)

print("\n== windows shed last, and the fullest window sheds last of all ==")
# A row over MAX_ROW_BYTES sheds rather than being dropped whole. Cost, context
# and model go first; only then do the windows go, least-used first, because the
# highest percentage is the one that decides admission.
crowded = capture.build_row(json.loads(payload(
    five=3, week=7, five_reset=epoch, week_reset=epoch, context=50, cost=1.5,
    model="claude-opus-5-with-a-deliberately-long-identifier",
    windows=dict(("seven_day_model_number_%02d" % n, (float(n * 11), epoch))
                 for n in range(1, 7)))), now=NOW)
check("the crowded row really is over the cap before shedding",
      len(json.dumps(crowded)) > capture.MAX_ROW_BYTES, True)
check("and it is under the per-row window cap, so this tests shedding alone",
      len(capture.window_names(crowded)) <= capture.MAX_WINDOWS, True)
shed = json.loads(capture._encode(crowded).decode("utf-8"))
check("cost, context and model go before any window does",
      [k for k in ("cost_usd", "context_size", "model_id", "context_pct")
       if k in shed], [])
check("and the row that comes out fits",
      len(capture._encode(crowded)) < capture.MAX_ROW_BYTES, True)
survivors = sorted(shed[k] for k in shed if k.endswith("_pct"))
offered = sorted(crowded[k] for k in crowded
                 if k.endswith("_pct") and k != "context_pct")
check("whatever room is left goes to the highest percentages, in order",
      survivors, offered[-len(survivors):])
check("so the fullest window is still there when the dust settles",
      shed.get("seven_day_model_number_06_pct"), 66.0)
check("and its reset went with it, not without it",
      "seven_day_model_number_06_reset" in shed, True)
# The rule is the percentage, not the pedigree: five-hour at 3% is shed while a
# per-model window at 66% stays, because 66% is what would hold work back.
check("a documented window at 3% goes before an undocumented one at 66%",
      ("five_hour_pct" in shed, "seven_day_model_number_06_pct" in shed),
      (False, True))
check("a shed window loses both halves or neither",
      sorted(k[:-4] for k in shed if k.endswith("_pct"))
      == sorted(k[:-6] for k in shed if k.endswith("_reset")), True)
# Down to the bone: twenty-nine windows in one row, most of which cannot fit.
tiny = dict([("v", 1), ("ts", iso(NOW))]
            + [("w%02d_pct" % n, float(n)) for n in range(1, 30)]
            + [("w%02d_reset" % n, iso(soon)) for n in range(1, 30)])
kept = json.loads(capture._encode(tiny).decode("utf-8"))
check("when almost everything must go, the highest percentage is what stays",
      ("w29_pct" in kept, "w01_pct" in kept), (True, False))
check("and a row with nothing left to shed is refused rather than truncated",
      capture._encode({"v": 1, "ts": iso(NOW), "note": "x" * 600}), None)

print("\n== a per-model window moving outranks the throttle ==")
# The throttle used to look at the two documented percentages only, so an Opus
# allowance climbing while those two sat still was a movement nothing recorded.
LEDGER.unlink(missing_ok=True)
base = {"v": 1, "ts": iso(NOW), "five_hour_pct": 10,
        "five_hour_reset": iso(soon), "seven_day_opus_pct": 40,
        "seven_day_opus_reset": iso(later)}
check("the first row is written", capture.append(dict(base), path=LEDGER),
      "written")
check("an unchanged row inside the minute is still throttled",
      capture.append(dict(base), path=LEDGER), "throttled")
moved = dict(base, seven_day_opus_pct=46)
check("but a per-model window that moved is recorded",
      capture.append(moved, path=LEDGER), "written")

print("\n== the context rule needs a context reading it can date ==")
# The context number never carried a measurement time, so --max-context was
# applied to a reading of unbounded age: here a nine-hour-old 95% beside a
# one-minute-old window reading. The staleness rule covered the two quota
# windows and nothing else, and nothing reported the context reading's age
# either, so no caller could have noticed.
write([{"v": 1, "ts": iso(NOW - timedelta(hours=9)), "context_pct": 95},
       {"v": 1, "ts": iso(NOW - timedelta(minutes=1)), "five_hour_pct": 5,
        "five_hour_reset": iso(soon)}])
found = ledger.reading(path=LEDGER, now=NOW)
check("the reading says when the context number was measured",
      found["context_measured_at"], iso(NOW - timedelta(hours=9)))
check("and how old that makes it", found["context_age_minutes"], 540.0)
answer = gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30,
                       max_context_pct=50)
check("a nine-hour-old context number decides nothing, in either direction",
      answer["verdict"], gate.UNKNOWN)
check("and the reason says it is the context reading that is out of date",
      "context" in answer["why"] and "540" in answer["why"], True)
check("the age is reported whether or not anyone gated on it",
      gate.capacity(path=LEDGER, now=NOW)["context_age_minutes"], 540.0)
check("and a stale context reading only stops a caller who asked about it",
      gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30)["verdict"],
      gate.GO)
write([{"v": 1, "ts": iso(NOW - timedelta(minutes=2)), "context_pct": 95,
        "five_hour_pct": 5, "five_hour_reset": iso(soon)}])
check("while a fresh context reading still holds the work back",
      gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30,
                    max_context_pct=50)["verdict"], gate.WAIT)

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

print("\n== a reading stamped in the future is not a fresh reading ==")
# Freshness was bounded from above only, so a row from a machine whose clock
# runs ahead has a NEGATIVE age, passes every staleness test, and goes on
# deciding for as long as it sits in the tail. capture already refuses a reset
# that runs further ahead than any real window; nothing guarded the row's own
# observation time.
write([{"v": 1, "ts": iso(NOW + timedelta(hours=3)), "five_hour_pct": 2,
        "five_hour_reset": iso(NOW + timedelta(hours=4))}])
answer = gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30)
check("a row stamped three hours ahead is not permission to start",
      answer["verdict"], gate.UNKNOWN)
check("and the reason says the stamp is ahead, not that it is old",
      "future" in answer["why"], True)
check("it does not blame the status line for a clock problem",
      "status line" in answer["why"], False)
check("a skew inside the age you allow is still usable",
      gate.capacity(path=LEDGER, max_age_minutes=30,
                    now=NOW + timedelta(hours=3) - timedelta(minutes=5)
                    )["verdict"], gate.GO)
# Both windows out of true, one each way: the message names the worse of them.
write([{"v": 1, "ts": iso(NOW + timedelta(minutes=500)),
        "seven_day_pct": 20, "seven_day_reset": iso(later)},
       {"v": 1, "ts": iso(NOW - timedelta(minutes=40)),
        "five_hour_pct": 12, "five_hour_reset": iso(soon)}])
answer = gate.capacity(path=LEDGER, now=NOW, max_age_minutes=30)
check("the furthest-out reading is the one named, whichever way it is out",
      "seven-day" in answer["why"] and "500" in answer["why"], True)

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
# The tail cannot see history that compaction has already thrown away, so a
# TAIL_ROWS above KEEP_ROWS is a promise the file cannot keep. This is the
# invariant behind the corrected comment on TAIL_ROWS: 4000 rows is 2.8 days at
# one row a minute, not the seven days it used to claim, and raising it to
# 10,080 without raising KEEP_ROWS would have bought nothing at all.
check("the tail never asks for more history than compaction keeps",
      ledger.TAIL_ROWS <= ledger.KEEP_ROWS, True)
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
# The guarantee under test is the lock's, and Windows has no lock by the
# module's own stated design -- the first full CI run proved the row really is
# lost there -- so on a lockless platform there is nothing here to test.
if ledger.fcntl is not None:
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
        time.sleep(0.3)                   # let compact take the lock first
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

print("\n== a compaction that waited for the lock rechecks what it holds ==")
# The other half of the same inode problem. `capture._open_locked` was taught to
# notice that the file it locked is no longer the file at the path; `compact`
# was not, so a second compaction that had been waiting woke up holding an
# exclusive lock on a file that had just been renamed away -- and then did its
# read, rewrite and replace with no lock on the live ledger at all, while a real
# appender held that lock and believed it had the file to itself.
#
# Staged rather than raced: this test holds the locks itself, so the loss is
# certain rather than lucky. Skipped where there are no advisory locks to take.
if ledger.fcntl is not None:
    import fcntl                                            # noqa: E402
    LOCK_LEDGER = SCRATCH / "lockrace.jsonl"
    write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 1,
            "five_hour_reset": iso(soon), "pad": "x" * 300} for _ in range(500)],
          path=LOCK_LEDGER)

    def _dawdling_rows(path, tail=ledger.TAIL_ROWS):
        got = real_rows(path, tail=tail)
        time.sleep(0.6)          # read, then dawdle before the replace
        return got

    held_old = os.open(str(LOCK_LEDGER), os.O_RDONLY)
    fcntl.flock(held_old, fcntl.LOCK_EX)     # the ledger everyone starts on
    staged = {}
    ledger._rows = _dawdling_rows
    try:
        worker = threading.Thread(
            target=lambda: staged.update(
                compact=ledger.compact(path=LOCK_LEDGER, max_bytes=100, keep=50)))
        worker.start()
        time.sleep(0.3)                      # it is now blocked on that lock
        # Someone else's compaction lands: a brand-new file at the same path.
        REPLACEMENT = SCRATCH / "replacement.jsonl"
        write([{"v": 1, "ts": iso(NOW), "five_hour_pct": 1,
                "five_hour_reset": iso(soon)} for _ in range(50)],
              path=REPLACEMENT)
        os.replace(str(REPLACEMENT), str(LOCK_LEDGER))
        # An appender takes the lock on the file that is actually there now.
        held_new = os.open(str(LOCK_LEDGER), os.O_WRONLY | os.O_APPEND)
        fcntl.flock(held_new, fcntl.LOCK_EX)
        os.close(held_old)                   # the waiting compaction wakes up
        time.sleep(0.2)
        os.write(held_new, (json.dumps(
            {"v": 1, "ts": iso(NOW), "five_hour_pct": 99,
             "five_hour_reset": iso(soon), "model_id": "late-arrival"}) + "\n"
        ).encode("utf-8"))
        os.close(held_new)                   # and only now is the lock free
        worker.join()
    finally:
        ledger._rows = real_rows
    check("the waiting compaction still ran", staged.get("compact"), "compacted")
    check("and the row written under the lock it should have waited for survives",
          "late-arrival" in LOCK_LEDGER.read_text(), True)
    check("so the reader sees the 99 per cent, not a go on stale rows",
          ledger.reading(path=LOCK_LEDGER, now=NOW)["five_hour"]["pct"], 99)

print("\n== a protected hour refuses the job that would still hold it ==")
# --protect 08:00: wake to a five-hour window that is yours. A job started
# inside the five hours before the protected time would still hold its window
# when that time arrives, so the gate waits. Earlier than that, or once the
# hour has passed, the ordinary rules decide. The clock used is the one `now`
# carries, so these are deterministic on any machine in any timezone.
PROTECT_LEDGER = SCRATCH / "protect.jsonl"
NOW_P = datetime(2026, 8, 25, 2, 0, 0, tzinfo=timezone.utc)
write([{"v": 1, "ts": iso(NOW_P), "five_hour_pct": 10.0,
        "five_hour_reset": iso(NOW_P + timedelta(minutes=90)),
        "seven_day_pct": 20.0,
        "seven_day_reset": iso(NOW_P + timedelta(days=5))}],
      path=PROTECT_LEDGER)


def _protected_verdict(hour, minute):
    return gate.capacity(path=PROTECT_LEDGER,
                         now=NOW_P.replace(hour=hour, minute=minute),
                         max_age_minutes=1440, protect=(8, 0))


check("six hours ahead of the protected time, the job may run",
      _protected_verdict(2, 0)["verdict"], gate.GO)
check("four and a half hours ahead, its window would still be held at eight",
      _protected_verdict(3, 30)["verdict"], gate.WAIT)
check("and the reason says so in words",
      "protected" in _protected_verdict(3, 30)["why"], True)
check("one minute ahead is still a refusal",
      _protected_verdict(7, 59)["verdict"], gate.WAIT)
check("once the protected hour has passed, the ordinary rules decide",
      _protected_verdict(9, 0)["verdict"], gate.GO)
check("an empty ledger inside the protected span says WAIT, not UNKNOWN",
      gate.capacity(path=SCRATCH / "no-such-protect.jsonl",
                    now=NOW_P.replace(hour=5), protect=(8, 0))["verdict"],
      gate.WAIT)
try:
    gate._protect_arg("nope")
    refused = "accepted"
except Exception as err:                                    # noqa: BLE001
    refused = type(err).__name__
check("a time that is not a time is refused loudly at the flag",
      refused, "ArgumentTypeError")
check("while a real one parses", gate._protect_arg("08:00"), (8, 0))

def _exit_code(call):
    try:
        return call()
    except SystemExit as err:
        return err.code


# == slack makes the protected hour honest instead of paranoid ==
# A window still open at eight that dies minutes later steals no morning.
# --protect-slack N tolerates a window running up to N minutes past the
# protected time, so the refusal is about overhang, not existence. Slack
# zero is exactly the old rule.
check("a window running 30 minutes past eight is fine inside a 120 slack",
      gate.capacity(path=PROTECT_LEDGER, now=NOW_P.replace(hour=3, minute=30),
                    max_age_minutes=1440, protect=(8, 0),
                    protect_slack=120)["verdict"], gate.GO)
check("one running two and a half hours past is not",
      gate.capacity(path=PROTECT_LEDGER, now=NOW_P.replace(hour=5, minute=30),
                    max_age_minutes=1440, protect=(8, 0),
                    protect_slack=120)["verdict"], gate.WAIT)
check("and the reason names the overhang",
      "past" in gate.capacity(path=PROTECT_LEDGER,
                              now=NOW_P.replace(hour=5, minute=30),
                              max_age_minutes=1440, protect=(8, 0),
                              protect_slack=120)["why"], True)
check("overhang exactly equal to the slack is tolerated",
      gate.capacity(path=PROTECT_LEDGER, now=NOW_P.replace(hour=3, minute=30),
                    max_age_minutes=1440, protect=(8, 0),
                    protect_slack=30)["verdict"], gate.GO)
check("slack without a protected hour is a loud usage error",
      _exit_code(lambda: gate.main(["check", "--protect-slack", "60"])), 2)
check("a slack that swallows the whole window is refused the same way",
      _exit_code(lambda: gate.main(["check", "--protect", "08:00",
                                    "--protect-slack", "300"])), 2)

# == the recorder trims the file it just grew past the cap ==
# `compact` exists, but nothing schedules it: a ledger written by months of
# renders grows until someone remembers a maintenance command. The recorder is
# the one thing guaranteed to be running when the file is big, so the append
# that lands past the cap is the one that trims -- after its own lock is gone,
# because the trim takes the same lock and two of them in one thread would
# wait on each other for ever.
TRIM_LEDGER = SCRATCH / "trim.jsonl"
filler = json.dumps({"seven_day_pct": 10.0, "noise": "x" * 60},
                    separators=(",", ":")) + "\n"
with open(TRIM_LEDGER, "w", encoding="utf-8") as out:
    out.write(filler * (ledger.MAX_BYTES // len(filler) + 2))
past = datetime.now().timestamp() - 3600
os.utime(TRIM_LEDGER, (past, past))          # an old file is never throttled
check("the ledger handed to the recorder is past the cap",
      TRIM_LEDGER.stat().st_size > ledger.MAX_BYTES, True)
check("the append that finds it there still reports written",
      capture.append({"five_hour_pct": 55.0, "marker": "the-row-that-trimmed"},
                     path=TRIM_LEDGER), "written")
check("and leaves the file back under the cap",
      TRIM_LEDGER.stat().st_size <= ledger.MAX_BYTES, True)
check("with the row it just wrote as the newest survivor",
      "the-row-that-trimmed" in
      TRIM_LEDGER.read_text(encoding="utf-8").splitlines()[-1], True)

# == a trim that cannot happen is a bigger file, not a dead status line ==
# The recorder's first law is that it never raises. The trim runs inside it,
# so a trim that blows up -- a broken ledger module, a disk that vanished
# between the write and the stat -- must cost only the trim, never the row
# that was already written and never the render.
real_compact = ledger.compact
def _exploding_compact(**kwargs):
    raise OSError("disk gone")
ledger.compact = _exploding_compact
try:
    try:
        got = capture.append({"five_hour_pct": 66.0}, path=TRIM_LEDGER)
    except Exception as err:
        got = "raised: %r" % (err,)
finally:
    ledger.compact = real_compact
check("a failing trim never reaches the caller", got, "written")

shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
