#!/usr/bin/env python3
"""Read the capacity ledger, and answer with the best reading it actually holds.

The ledger is one JSON object per line, appended by `capture`. This module turns
that file into the current position of each window. It is the half of this tool
with a real idea in it, and the idea is the selection rule below.

THE SELECTION RULE, and why it is not "take the newest row".

Each row carries, for each window, how much is spent and when that window next
resets. Two failures made the obvious rule wrong, both observed in production:

  A row can be half stale. A render can carry a fresh five-hour reading beside a
  seven-day reading that has already rolled over, so taking the newest ROW gives
  you a number for a window that no longer exists. The unit of freshness is the
  WINDOW, not the row.

  Newest is not the same as highest. Many rows share one reset time -- every
  render inside the same five-hour window does. Ranking those by reset alone and
  keeping the first match keeps the OLDEST of them. That reported a weekly window
  at 15% when it was really 58%, which is the difference between "there is plenty
  of room" and "most of the week is gone".

So: consider each window separately; discard any observation whose reset has
already passed, however fresh the row is; and among what remains rank on the
reset first, the observation time second, and the row's position in the file
third. All three parts matter, and the third is not a formality -- `ts` has
one-second resolution while the throttle deliberately permits a second write
inside the same second, so two honest renders often carry the same stamp, and
without a tie-break the reader keeps whichever it happened to see first. In an
append-only file that is the older, lower reading, which is a false go.

Both time halves of that key are parsed before they are compared. Comparing ISO
strings is only chronological while every row carries the same UTC offset, and
the offset changes twice a year -- "10:30+02:00" sorts after "09:45+01:00" while
being fifteen minutes earlier. Parse both sides or compare neither.

An unreadable stamp sorts oldest instead of raising, and a torn line is counted
rather than fatal, because a capacity read that throws takes down whatever asked.

WHAT THIS DOES NOT KNOW: the two windows in WINDOWS below, and no others. They
are the two the status-line payload documents. If a third is ever added, a
payload where that third window is exhausted reads here as a payload with room
in it -- silently, and in the expensive direction. That is the sharpest edge in
this file, and it is a change in someone else's schema away.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

import capture

WINDOWS = ("five_hour", "seven_day")

# Rotation. At one row a minute a busy month is a few megabytes, and every read
# walks the file, so it is compacted rather than allowed to grow without limit.
MAX_BYTES = 4 * 1024 * 1024
KEEP_ROWS = 5000

# How far back a read looks. Enough to cover a seven-day window at the throttled
# rate without walking a year of history on every call.
TAIL_ROWS = 4000


def _instant(value):
    """An ISO stamp as a comparable instant, offset-aware.

    Anything unreadable -- a number, a list, None, a naive stamp, nonsense --
    sorts oldest rather than raising. A reader that throws on one bad row is a
    reader that a single torn line can take out entirely.

    A NAIVE STAMP IS UNREADABLE, not a UTC one. `capture` refuses a naive reset
    in a payload on the grounds that guessing its zone silently moves every
    deadline; reading the same stamp here as UTC would be that same guess, made
    by the other half of the same tool, with up to fourteen hours of error in
    whichever direction the local offset runs. Sorting it oldest keeps the two
    modules saying one thing, and for a reset it means the observation is simply
    not used -- which is the honest outcome for a time nobody can place.
    """
    floor = datetime.min.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return floor
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return floor
    return parsed if parsed.tzinfo else floor


def _rows(path, tail=TAIL_ROWS):
    """Every parseable object in the tail, plus a count of what would not parse.

    A row that is valid JSON but not an object -- a bare number, a list, a
    string -- is skipped as unreadable rather than being handed to code that
    expects `.get`. A public ledger is an ingest surface, and the file can be
    edited by anything with write access to it.
    """
    try:
        text = Path(path).read_text(errors="ignore")
    except OSError:
        return [], 0
    good, bad = [], 0
    lines = text.splitlines()
    for line in lines[-tail:] if tail else lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(parsed, dict):
            bad += 1
            continue
        good.append(parsed)
    return good, bad


def reading(path=None, now=None, tail=TAIL_ROWS):
    """The current position, or None when the ledger holds nothing usable.

    None means silence and must be reported as silence. It is never zero: a
    window with no live observation is not a window with nothing spent, and the
    difference decides whether an agent may start work.
    """
    path = Path(path) if path else capture.ledger_path()
    now = capture.aware(now) or datetime.now(timezone.utc)
    horizon = now + timedelta(days=capture.MAX_RESET_AHEAD_DAYS)
    rows, unreadable = _rows(path, tail)
    if not rows:
        return None

    best = {}
    for index, row in enumerate(rows):
        seen_at = _instant(row.get("ts"))
        for window in WINDOWS:
            pct = capture._pct(row.get(window + "_pct"))
            reset_raw = row.get(window + "_reset")
            if pct is None or not isinstance(reset_raw, str) or not reset_raw.strip():
                continue
            reset = _instant(reset_raw)
            if reset <= now:
                continue                     # that window has already rolled over
            if reset > horizon:
                # Further ahead than any window Claude Code has. `capture`
                # refuses these at the door, but the ledger is an ingest surface
                # and one may already be sitting in it -- and because the reset
                # ranks first, one such row outranks every genuine one until it
                # falls out of the tail: first serving its own stale percentage,
                # then jamming the answer at not-known once it ages out.
                unreadable += 1
                continue
            # THE THIRD ELEMENT IS THE ROW'S POSITION, AND IT IS LOAD-BEARING.
            # `ts` has one-second resolution, and the throttle deliberately
            # allows a second write inside the same second whenever a percentage
            # moved by a point or more -- so two honest renders routinely land
            # with identical stamps. On a strict > with a two-part key, the row
            # seen first wins, and in an append-only file the row seen first is
            # the OLDER write: the reader would serve the lower reading and the
            # gate would say go into a window it had just been told was nearly
            # spent. File order is write order, and compaction preserves it.
            key = (reset, seen_at, index)
            if window not in best or key > best[window]["_key"]:
                best[window] = {"pct": pct, "reset": reset_raw,
                                "measured_at": row.get("ts"), "_key": key}

    # The context window has no reset of its own -- it is a property of the
    # session in front of you, not of a rolling quota -- so the newest reading
    # wins and the validity rule does not apply to it.
    # All three are "whatever was seen most recently", and all three compare
    # parsed timestamps to decide that. Taking the last MATCH in file order
    # instead would be a different question -- newest by position, not by time --
    # and the two disagree on any ledger whose rows are not in time order, which
    # is exactly what the offset change twice a year produces.
    context, context_at = None, None
    cost, cost_at = None, None
    model, model_at = None, None
    newest = None
    for row in rows:
        seen_at = _instant(row.get("ts"))
        if newest is None or seen_at > newest:
            newest = seen_at
        pct = capture._pct(row.get("context_pct"))
        if pct is not None and (context_at is None or seen_at > context_at):
            context, context_at = pct, seen_at
        spent = row.get("cost_usd")
        if isinstance(spent, (int, float)) and not isinstance(spent, bool) \
                and (cost_at is None or seen_at > cost_at):
            cost, cost_at = spent, seen_at
        ident = row.get("model_id")
        if isinstance(ident, str) and (model_at is None or seen_at > model_at):
            model, model_at = ident, seen_at

    out = {"health": {"rows": len(rows), "unreadable": unreadable,
                      "newest": None if newest is None else newest.isoformat()},
           "context_pct": context, "cost_usd": cost, "model_id": model}
    for window in WINDOWS:
        found = best.get(window)
        if not found:
            out[window] = None
            continue
        age = (now - _instant(found["measured_at"])).total_seconds() / 60.0
        out[window] = {"pct": found["pct"], "reset": found["reset"],
                       "measured_at": found["measured_at"],
                       "age_minutes": round(age, 1)}
    if out["five_hour"] is None and out["seven_day"] is None and context is None:
        return None
    return out


def compact(path=None, max_bytes=MAX_BYTES, keep=KEEP_ROWS):
    """Shrink the ledger to its newest rows once it passes a size.

    Returns what happened: 'small-enough', 'compacted', or 'failed'. Nothing is
    deleted while the file is under the threshold, and the rewrite is a write to
    a temporary file followed by a replace, so a crash mid-compaction leaves the
    old ledger whole rather than a half one.

    A row arriving DURING the rewrite is not lost either, but that is `capture`'s
    doing rather than this function's: an appender that was blocked on the lock
    wakes up holding it on the replaced file, notices, and reopens. See
    `capture._open_locked`.
    """
    path = Path(path) if path else capture.ledger_path()
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return "small-enough"
    except OSError:
        return "failed"
    lock, tmp = None, None
    try:
        if fcntl is not None:
            lock = os.open(str(path), os.O_RDONLY)
            fcntl.flock(lock, fcntl.LOCK_EX)
        rows, _ = _rows(path, tail=None)
        # Not `rows[-keep:]`: at keep=0 that is `rows[0:]`, which keeps
        # everything and then reports 'compacted' -- the caller asked for none
        # and got all of them, told it had shrunk the file. A negative keep is
        # the same kind of nonsense from the other end, dropping rows off the
        # front, so both are read as "keep nothing".
        kept = rows[-keep:] if keep > 0 else []
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".capacity-")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for row in kept:
                out.write(json.dumps(row, ensure_ascii=False,
                                     separators=(",", ":")) + "\n")
        os.replace(tmp, str(path))
        tmp = None                   # the rename consumed it; nothing to clean
        return "compacted"
    except (OSError, ValueError):
        return "failed"
    finally:
        if tmp is not None:
            # The likeliest reason the write above failed is a full disk, which
            # is also when a leaked partial copy of the ledger costs the most.
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                os.close(lock)


if __name__ == "__main__":
    print(json.dumps(reading(), indent=2, default=str))
