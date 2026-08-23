#!/usr/bin/env python3
"""Answer one question: is there capacity to start this work?

This is the admission half of the tool. Everything else exists to make this
answer trustworthy. A scheduler, a cron line, an overnight agent or a wrapper
script asks, gets a yes or a no, and acts.

    python3 gate.py check --max-pct 80 && ./run-the-expensive-thing

FAIL CLOSED ON SILENCE. If the ledger has no live reading, or the newest one is
older than the age you allow, the answer is "do not start" and the exit code is
non-zero. Not knowing how much is left is not the same as knowing there is
plenty, and the whole point of asking is to avoid spending a window you cannot
see. Pass --allow-unknown if you would rather start anyway; that is a decision to
make deliberately, not a default to inherit.

WHICH MEANS THE DEFAULT --max-age IS TOO TIGHT FOR AN OVERNIGHT JOB. Only a
status-line render writes to the ledger, and a status line only renders while a
session is open. At 2am the last reading is usually hours old, so the 30-minute
default answers UNKNOWN and the job does not run -- correctly, by this file's own
rule, but not usefully. Widen --max-age to something that spans the gap since you
last had a session open, and read the README section on the crontab line.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import capture
import ledger

DEFAULT_MAX_PCT = 80.0
DEFAULT_MAX_AGE_MINUTES = 30.0

GO = "GO"
WAIT = "WAIT"
UNKNOWN = "UNKNOWN"


def _minutes_until(stamp, now):
    moment = ledger._instant(stamp)
    if moment == datetime.min.replace(tzinfo=timezone.utc):
        return None
    return round((moment - now).total_seconds() / 60.0, 1)


def capacity(path=None, now=None, max_pct=DEFAULT_MAX_PCT,
             max_age_minutes=DEFAULT_MAX_AGE_MINUTES, max_context_pct=None):
    """The structured answer. Always a dict, never an exception.

    `verdict` is GO, WAIT or UNKNOWN. `why` says which rule decided it, in words
    a human reading a log will understand without opening this file.
    """
    now = capture.aware(now) or datetime.now(timezone.utc)
    found = ledger.reading(path=path, now=now)
    out = {"verdict": UNKNOWN, "why": "", "checked_at": now.isoformat(),
           "five_hour": None, "seven_day": None, "context_pct": None,
           "cost_usd": None, "model_id": None, "health": None,
           "max_pct": max_pct, "max_age_minutes": max_age_minutes}
    if not found:
        out["why"] = ("the ledger holds no usable reading -- nothing has been "
                      "captured, or every window in it has already reset")
        return out

    for key in ("context_pct", "cost_usd", "model_id", "health"):
        out[key] = found.get(key)
    for window in ledger.WINDOWS:
        got = found.get(window)
        if not got:
            continue
        out[window] = dict(got)
        out[window]["minutes_until_reset"] = _minutes_until(got["reset"], now)

    live = [w for w in ledger.WINDOWS if out[w]]
    if not live:
        out["why"] = ("no window has a live reading -- every reset in the "
                      "ledger has already passed")
        return out

    stale = [w for w in live if out[w]["age_minutes"] > max_age_minutes]
    if stale:
        # Name the window and its own age. The earlier wording called this "the
        # newest reading", which was false whenever the two windows were not
        # equally stale: with a five-hour reading 40 minutes old beside a
        # seven-day one 400 minutes old, it announced that the newest reading
        # was 400 minutes old. This line is read in a log at 3am by someone
        # working out why nothing ran, so it says which window is out of date.
        window = max(stale, key=lambda w: out[w]["age_minutes"])
        out["why"] = ("the %s reading is %.0f minutes old, past the %.0f "
                      "allowed -- the status line may not be running, so this "
                      "reports not-known rather than guessing"
                      % (window.replace("_", "-"), out[window]["age_minutes"],
                         max_age_minutes))
        return out

    over = [w for w in live if out[w]["pct"] >= max_pct]
    if over:
        window = max(over, key=lambda w: out[w]["pct"])
        out["verdict"] = WAIT
        minutes = out[window]["minutes_until_reset"]
        out["why"] = ("%s is at %.0f%%, at or past the %.0f%% limit%s"
                      % (window.replace("_", "-"), out[window]["pct"], max_pct,
                         "" if minutes is None
                         else "; it resets in %.0f minutes" % minutes))
        return out

    if max_context_pct is not None and out["context_pct"] is not None \
            and out["context_pct"] >= max_context_pct:
        out["verdict"] = WAIT
        out["why"] = ("the context window is %.0f%% full, at or past the %.0f%% "
                      "limit" % (out["context_pct"], max_context_pct))
        return out

    out["verdict"] = GO
    out["why"] = ("every live window is under %.0f%%: %s" % (
        max_pct, ", ".join("%s at %.0f%%" % (w.replace("_", "-"), out[w]["pct"])
                           for w in live)))
    return out


def _human(answer):
    lines = ["%s -- %s" % (answer["verdict"], answer["why"])]
    for window in ledger.WINDOWS:
        got = answer.get(window)
        if not got:
            lines.append("  %-10s no live reading" % window.replace("_", "-"))
            continue
        minutes = got.get("minutes_until_reset")
        lines.append("  %-10s %5.1f%%  resets in %s  measured %.0f min ago"
                     % (window.replace("_", "-"), got["pct"],
                        "unknown" if minutes is None else "%.0f min" % minutes,
                        got["age_minutes"]))
    if answer.get("context_pct") is not None:
        lines.append("  %-10s %5.1f%%" % ("context", answer["context_pct"]))
    health = answer.get("health") or {}
    if health.get("unreadable"):
        lines.append("  %-10s %d unreadable row(s) skipped"
                     % ("ledger", health["unreadable"]))
    return "\n".join(lines)


REGISTER_TEXT = """Add this to your Claude Code settings so every render is captured.

  File:  {settings}
  Entry:

  "statusLine": {{
    "type": "command",
    "command": "{python} {script}"
  }}

Nothing was written -- this only prints. Edit the file yourself, or merge the
entry with whatever statusLine you already have.

The ledger will be written to:
  {store}

Then check that it works:
  {python} {gate} show
"""


def register_text():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    return REGISTER_TEXT.format(
        settings=os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
        python=sys.executable or "python3",
        script=os.path.join(here, "capture.py"),
        gate=os.path.join(here, "gate.py"),
        store=capture.ledger_path())


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-capacity",
        description="Is there capacity to start this work?")
    subs = parser.add_subparsers(dest="command")

    check = subs.add_parser("check", help="exit 0 to go, 1 to wait")
    check.add_argument("--max-pct", type=float, default=DEFAULT_MAX_PCT,
                       help="wait once either window is at this per cent "
                            "(default: %(default)s)")
    check.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_MINUTES,
                       dest="max_age",
                       help="treat a reading older than this many minutes as "
                            "not known (default: %(default)s)")
    check.add_argument("--max-context", type=float, default=None,
                       dest="max_context",
                       help="also wait once the context window is this full")
    check.add_argument("--allow-unknown", action="store_true",
                       help="exit 0 when capacity cannot be established")
    check.add_argument("--json", action="store_true", help="print the full answer")
    check.add_argument("--quiet", action="store_true", help="print nothing")

    show = subs.add_parser("show", help="print the current position")
    show.add_argument("--json", action="store_true")

    subs.add_parser("register", help="print the Claude Code settings entry")
    subs.add_parser("compact", help="shrink the ledger to its newest rows")

    args = parser.parse_args(argv)
    if args.command is None:
        # Deliberately NOT a default of `show`. `show` exits 0 whatever it finds,
        # because it is a display command -- so while a bare invocation meant
        # `show`, `python3 gate.py && ./job` launched the job on a completely
        # empty ledger. A tool whose whole promise is that not knowing is never
        # permission to start must not have a spelling of itself that always
        # says go, and the two spellings differed by one easily dropped word.
        parser.print_help(sys.stderr)
        sys.stderr.write("\nSay which: `check` gates and exits 0 to go or 1 to "
                         "wait; `show` prints the current position.\n")
        return 2
    if args.command == "show":
        answer = capacity()
        if getattr(args, "json", False):
            print(json.dumps(answer, indent=2, default=str))
        else:
            print(_human(answer))
        return 0
    if args.command == "register":
        print(register_text())
        return 0
    if args.command == "compact":
        print(ledger.compact())
        return 0

    answer = capacity(max_pct=args.max_pct, max_age_minutes=args.max_age,
                      max_context_pct=args.max_context)
    if not args.quiet:
        if args.json:
            print(json.dumps(answer, indent=2, default=str))
        else:
            print(_human(answer))
    if answer["verdict"] == GO:
        return 0
    if answer["verdict"] == UNKNOWN and args.allow_unknown:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
