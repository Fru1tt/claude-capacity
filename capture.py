#!/usr/bin/env python3
"""Turn one Claude Code status-line render into one ledger row and one line.

Claude Code runs a status-line command on every render and hands it a JSON
object on stdin. Short of scraping an OAuth credential out of the keychain and
calling an undocumented usage endpoint, that object is the only place a local
process can see how much of the five-hour and seven-day windows has been spent,
and how full the current context window is. This module reads one of those
payloads, writes one row, and returns one short string to show.

Three properties this file has to keep, each preventing a specific failure:

1. IT MAY NEVER RAISE. A status line runs inside the interface. An exception
   here is a dead status line rather than an error message, so every failure
   degrades to one plain string.
2. THE LINE IS BUILT BEFORE THE ROW IS WRITTEN. A store that cannot be written
   must cost the recording, never the display.
3. THE APPEND IS ONE BOUNDED WRITE UNDER AN ADVISORY LOCK. Several sessions
   render at once, and the lock covers the throttle check and the append
   together, so two renders cannot both decide they are the one to write.
   EXCEPT ON WINDOWS, where `fcntl` does not exist and there is no advisory lock
   to take. The write itself is still atomic there, so lines do not tear; what
   is lost is only that pairing, and its cost is a duplicate row.

What the payload does and does not carry, both halves checked 2026-08-23:

- WHAT IS HERE, from Claude Code's published status-line documentation: each
  window carries `used_percentage` (0 to 100) and `resets_at` (unix epoch
  seconds), and its example payload shows nothing else inside a window.
- WHAT IS NOT, from the agent toolkit's own type definitions
  (`@anthropic-ai/claude-agent-sdk`, checked at 0.3.241): there is no provider
  status here and no overage flag. Those are real -- `status`, `overageStatus`
  and `isUsingOverage` on `SDKRateLimitInfo` -- but the types carry them on
  `SDKRateLimitEvent`, a message on the CLI's streaming output, which is a
  channel a status line never receives.

Any tool claiming to read a provider status from here is reading something it
was not given.

Only two windows are known here, because only two are documented. If a third is
ever added, this records the two it understands and says nothing about the rest
-- see WINDOWS in `ledger`, which is where that limit bites.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:            # Windows has no advisory locks; see `append`
    fcntl = None

SCHEMA = 1
FALLBACK = "capacity: unavailable"
THROTTLE_SECONDS = 60
MODEL_ID_MAX = 64

# A row is capped so it stays one small write. What actually keeps two
# concurrent appends from interleaving halves of a line is the single write to a
# descriptor opened O_APPEND, which a local filesystem makes atomic against other
# writers -- measured at 80 simultaneous appends with the lock disabled entirely:
# 80 lines, 0 torn. The cap is belt to that braces: it bounds how much a single
# short write could leave behind, and it keeps the ledger's rows a predictable
# size. A row too long sheds these fields, in this order, rather than being
# dropped whole -- the quota numbers are the point of the row and are never shed.
MAX_ROW_BYTES = 512
SHED_ORDER = ("cost_usd", "context_size", "model_id", "context_pct")

# The longest window Claude Code documents is seven days. A reset beyond this is
# not a long window, it is a broken field -- and a broken one that lasts, because
# the reader ranks on the reset first, so a single absurd value outranks every
# genuine row until it falls out of the tail. Refused at the door instead.
MAX_RESET_AHEAD_DAYS = 30


def store_dir():
    """Where the ledger lives. CLAUDE_CAPACITY_STORE overrides everything.

    Hand-rolled rather than taking a dependency: a status line runs on every
    render, and an import that is not in the standard library is a cost paid
    thousands of times a day for about fifteen lines of path logic.
    """
    override = os.environ.get("CLAUDE_CAPACITY_STORE")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-capacity"
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return Path(base) / "claude-capacity" if base else Path.home() / "claude-capacity"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "claude-capacity"


def ledger_path():
    return store_dir() / "capacity.jsonl"


def aware(moment):
    """A datetime that can be compared with the ones in here.

    Every `now` parameter in this project is documented and public, and the
    obvious thing to pass is `datetime.now()`, which is naive. Comparing that
    with an offset-aware stamp raises TypeError -- so a naive one is read as
    UTC here rather than being allowed to take down a caller that was using the
    parameter exactly as its docstring invites. Note that this is the reverse of
    what `_reset` does to a naive stamp arriving in a PAYLOAD, and deliberately:
    a payload stamp is data from elsewhere whose zone is genuinely unknowable,
    while `now` is the caller's own clock and reading it as UTC is at worst a
    fixed offset the caller chose.
    """
    if not isinstance(moment, datetime):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _pct(value):
    """A percentage as a float, or None. A value above 100 is CAPPED, not dropped.

    Claude Code documents this field as 0 to 100 and builds it as the base
    plan's utilisation times one hundred, so above 100 should not occur. It is
    capped rather than rejected because rejecting it would make a full window
    vanish from the ledger and let a reader fall back to an older, lower
    reading -- silence in the one direction where silence is dangerous.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value) if value <= 100 else 100.0


def _local_iso(moment, now):
    """An instant as a local-offset ISO string, or None if it is unusable.

    Two ways it is unusable, and both are reached by real payloads. `astimezone`
    raises OverflowError on a stamp within one UTC offset of the year 1 or the
    year 9999, so it is guarded rather than left to escape a module that may
    never raise. And a reset further ahead than any real window is refused: see
    MAX_RESET_AHEAD_DAYS.
    """
    try:
        if moment > now + timedelta(days=MAX_RESET_AHEAD_DAYS):
            return None
        return moment.astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def _reset(value, now=None):
    """Epoch seconds first, which is what the payload documents. An ISO string
    is tolerated so a schema change degrades to working rather than to None on
    the one field the whole reset clock depends on.

    A naive stamp is refused: without an offset there is no way to know which
    instant it names, and guessing the local zone silently moves every deadline.
    """
    if isinstance(value, bool):
        return None
    now = now or datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        try:
            moment = datetime.fromtimestamp(value, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
        return _local_iso(moment, now)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"   # 3.9's fromisoformat does not take Z
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return _local_iso(parsed, now)
    return None


def _window(payload, name, now=None):
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None, None
    window = limits.get(name)
    if not isinstance(window, dict):
        return None, None
    return _pct(window.get("used_percentage")), _reset(window.get("resets_at"), now)


def _context(payload):
    ctx = payload.get("context_window")
    if not isinstance(ctx, dict):
        return None, None
    size = ctx.get("context_window_size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        size = None
    return _pct(ctx.get("used_percentage")), size


def _cost(payload):
    cost = payload.get("cost")
    if not isinstance(cost, dict):
        return None
    value = cost.get("total_cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 4)


def _model(payload):
    model = payload.get("model")
    if not isinstance(model, dict):
        return None
    ident = model.get("id")
    if not isinstance(ident, str) or not ident.strip():
        return None
    # Capped because it lands in a bounded row and nothing validates its length
    # on the way in.
    return ident.strip()[:MODEL_ID_MAX]


def build_row(payload, now=None):
    """One payload becomes one row. Absent fields are absent, never zero --
    a window Claude Code did not send is not a window at nought per cent.

    A payload that is not an object is read as an EMPTY one, which yields a row
    carrying only its schema and its stamp. `render` already refuses a non-dict
    before it gets here, but this function is public and the readers below it
    (`_window`, `_context`, `_cost`, `_model`) each guard their own slice of the
    payload -- so reaching into it with `.get` on the way in was the one
    unguarded step, and it turned `build_row(7)` into an AttributeError out of a
    module whose first property is that it may never raise.
    """
    if not isinstance(payload, dict):
        payload = {}
    now = aware(now) or datetime.now(timezone.utc).astimezone()
    five_pct, five_reset = _window(payload, "five_hour", now)
    week_pct, week_reset = _window(payload, "seven_day", now)
    ctx_pct, ctx_size = _context(payload)
    row = {"v": SCHEMA, "ts": now.isoformat(timespec="seconds")}
    for key, value in (("five_hour_pct", five_pct),
                       ("five_hour_reset", five_reset),
                       ("seven_day_pct", week_pct),
                       ("seven_day_reset", week_reset),
                       ("context_pct", ctx_pct),
                       ("context_size", ctx_size),
                       ("cost_usd", _cost(payload)),
                       ("model_id", _model(payload))):
        if value is not None:
            row[key] = value
    return row


def _encode(row):
    row = dict(row)
    while True:
        try:
            text = json.dumps(row, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False)
        except (TypeError, ValueError):
            return None
        line_bytes = (text + "\n").encode("utf-8")
        if len(line_bytes) < MAX_ROW_BYTES:
            return line_bytes
        for key in SHED_ORDER:
            if key in row:
                del row[key]
                break
        else:
            return None


def _last_row(path):
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            tail = handle.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return {}
    for text in reversed(tail):
        text = text.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue                      # a torn tail line is not the answer
        if isinstance(parsed, dict):
            return parsed
    return {}


def _moved(previous, current):
    for field in ("five_hour_pct", "seven_day_pct"):
        old, new = _pct(previous.get(field)), _pct(current.get(field))
        if old is not None and new is not None and abs(new - old) >= 1:
            return True
    return False


def _release(handle):
    """Unlock and close, swallowing everything. Only ever called on the way out."""
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(handle)
    except OSError:
        pass


def _is_file_at(handle, path):
    """Does this descriptor still refer to the file sitting at `path`?"""
    try:
        held, there = os.fstat(handle), os.stat(str(path))
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (there.st_dev, there.st_ino)


APPEND_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND


def _open_locked(path, flags=APPEND_FLAGS):
    """A descriptor open on `path` under `flags`, holding the exclusive lock.

    A LOCK LIVES ON AN INODE, NOT ON A NAME. `compact` replaces the ledger by
    rename, so an appender that blocked on the lock can wake up holding it on a
    file that has just been unlinked -- and then write its row into an orphan
    nobody will ever read, and report 'written'. The throttle makes it worse by
    reading the tail back by PATH, so the decision and the write would be about
    two different files.

    So after locking, the descriptor is checked against the path and reopened
    once if they have parted company. Once, not in a loop: a second replacement
    inside one lock wait is not worth an unbounded retry, and a fixed number of
    attempts cannot spin.

    THIS IS THE ONLY COPY OF THAT RULE ON PURPOSE. `ledger.compact` waits for
    the same lock on the same file and had its own opening code without the
    check, so the fix lived on one side of the pair only: a compaction that
    waited would wake holding an exclusive lock on a renamed-away file and then
    read, rewrite and replace the live ledger with no lock on it at all, while a
    real appender held that lock and believed it had the file to itself. Hence
    `flags`, so the reader can take the lock through this function rather than
    keeping a second, drifting copy of the rule.
    """
    for attempt in (1, 2):
        try:
            handle = os.open(str(path), flags, 0o600)
        except OSError:
            return None
        if fcntl is None:
            return handle            # Windows: there is no advisory lock to take
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            _release(handle)
            return None
        if attempt == 2 or _is_file_at(handle, path):
            return handle
        _release(handle)             # replaced while we waited; take the new one
    return None


def append(row, path=None):
    """Append one row. Returns the word for what happened -- 'written',
    'throttled', 'too-long', 'short' or 'failed' -- and never raises.

    THE LOCK COVERS THE THROTTLE CHECK AND THE WRITE TOGETHER. Reading the
    file's age and then appending are two steps, and several sessions render at
    once; without one lock around both, two of them read the same age, both
    decide the ledger is stale, and both append.

    A render happens constantly, so a row is written at most once a minute --
    unless a percentage has moved by a point or more, which is real movement and
    outranks the throttle.
    """
    if not isinstance(row, dict):
        # Same promise as the rest of this file: a word back, never a traceback.
        # `_encode` would raise on a non-dict before it ever reached the store.
        return "failed"
    path = Path(path) if path else ledger_path()
    line_bytes = _encode(row)
    if line_bytes is None:
        return "too-long"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "failed"
    handle = _open_locked(path)
    if handle is None:
        return "failed"
    try:
        # Measured on the open handle, and AFTER the lock, so the age being
        # judged is the one that exists now rather than the one before whoever
        # else was waiting. An empty file is never throttled: O_CREAT has just
        # made it, so its age is zero seconds and the throttle would otherwise
        # refuse the first row a new ledger ever gets -- for ever.
        try:
            stat = os.fstat(handle)
            empty = stat.st_size == 0
            age = datetime.now().timestamp() - stat.st_mtime
        except OSError:
            empty, age = True, None
        if not empty and age is not None and age < THROTTLE_SECONDS \
                and not _moved(_last_row(path), row):
            return "throttled"
        written = os.write(handle, line_bytes)
        # A short write leaves a torn row. It cannot be taken back, so it is
        # named here and counted by the reader rather than reported as success.
        return "written" if written == len(line_bytes) else "short"
    except OSError:
        return "failed"
    finally:
        _release(handle)


def _segment(row, key, label, fmt):
    """One window's piece of the line, or None if the row cannot supply it.

    Every guard here is TypeError as well as ValueError. The row this is handed
    at render time is one this module just built, but `line` is public and a
    ledger is an ingest surface -- a percentage that arrived as a string and a
    reset that arrived as a number are both shapes the file can hold, and
    neither is worth a traceback in a status line.
    """
    pct = row.get(key + "_pct")
    try:
        segment = "%s %d%%" % (label, round(pct))
    except TypeError:
        return None
    stamp = row.get(key + "_reset")
    if stamp:
        try:
            segment += datetime.fromisoformat(stamp).strftime(fmt)
        except (TypeError, ValueError):
            pass
    return segment


def line(row):
    """The one glanceable string, built from the row that was recorded, so what
    is shown and what is stored cannot drift apart."""
    if not isinstance(row, dict):
        return FALLBACK
    parts = []
    for key, label, fmt in (("five_hour", "5h", " %H:%M"),
                            ("seven_day", "7d", " %a")):
        if key + "_pct" in row:
            segment = _segment(row, key, label, fmt)
            if segment is not None:
                parts.append(segment)
    if "context_pct" in row:
        try:
            parts.append("ctx %d%%" % round(row["context_pct"]))
        except TypeError:
            pass
    return "  ".join(parts) if parts else FALLBACK


def render(raw, path=None):
    """One payload in, one line out, and one row recorded on the way.

    The line is assembled BEFORE the append, so a store that cannot be written
    degrades to shown-but-not-recorded rather than taking the display down with
    it.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # TypeError as well as ValueError: `json.loads(None)` is what a caller
        # gets for reading an empty pipe into a variable that was never set, and
        # this function promises a string back whatever it is handed.
        return FALLBACK
    if not isinstance(payload, dict):
        return FALLBACK
    row = build_row(payload)
    text = line(row)
    if any(key in row for key in ("five_hour_pct", "seven_day_pct", "context_pct")):
        append(row, path)
    return text


if __name__ == "__main__":
    try:
        sys.stdout.write(render(sys.stdin.read()))
    except Exception:                     # noqa: BLE001 -- never a dead line
        sys.stdout.write(FALLBACK)
