#!/usr/bin/env python3
"""Turn one Claude Code status-line render into one ledger row and one line.

Claude Code runs a status-line command on every render and hands it a JSON
object on stdin. Short of scraping an OAuth credential out of the keychain and
calling an undocumented usage endpoint, that object is the only place a local
process can see how much of each quota window has been spent, and how full the
current context window is. This module reads one of those payloads, writes one
row, and returns one short string to show.

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

HOW MANY WINDOWS THERE ARE IS NOT DECIDED HERE, AND WAS NEVER SAFE TO GUESS.
The documentation lists two, `five_hour` and `seven_day`. Claude Code 2.1.237
sends four: those two plus `seven_day_opus` and `seven_day_sonnet`, undocumented
per-model weekly windows already arriving at status lines
(github.com/anthropics/claude-code/issues/88137, filed 2026-08-20). This module
used to look up the two names it knew and drop everything else, which fails in
the one direction that costs money -- an ignored window that is nearly spent
reads downstream as a window with room, and the gate says go into a wall. So
every key under `rate_limits` is read, under whatever name the payload gave it,
subject only to the sanitising in `_window_name` and the count cap MAX_WINDOWS.
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
# dropped whole.
#
# THE QUOTA WINDOWS ARE NOT IN THIS LIST BECAUSE THEY SHED LAST, after every
# field named here. Only once nothing else is left does `_shed` start on the
# windows, and it takes the LEAST-USED first: the fullest window is the one that
# decides admission, so it is the last number in the row worth losing. A window
# goes as a pair, its percentage and its reset together, because the reader
# needs both and half of one is bytes spent on nothing.
MAX_ROW_BYTES = 512
SHED_ORDER = ("cost_usd", "context_size", "model_id", "context_pct")

# The two windows Claude Code documents. Nothing is filtered on this -- the
# payload decides what windows exist -- it fixes the ORDER things are listed in,
# so the pair every reader recognises comes first in a row, a status line and a
# verdict alike, and it names the two keys older ledgers and older callers
# already depend on.
DOCUMENTED_WINDOWS = ("five_hour", "seven_day")

# Short label and reset format for the status line, for the documented two. A
# window outside this table is still shown; see `_label`.
WINDOW_LABELS = {"five_hour": ("5h", " %H:%M"), "seven_day": ("7d", " %a")}

# How many windows one row may carry. Documented: two. Observed in Claude Code
# 2.1.237: four. Eight is twice what has actually been seen, which leaves room
# for a per-model window for every model family shipping at once while still
# bounding what an arbitrary payload can make this write and store. Past the cap
# the HIGHEST percentages are the ones kept, for the same reason the shed keeps
# them last: the fullest window is the one that decides admission.
MAX_WINDOWS = 8

# A window name arrives from someone else's JSON and leaves as a key in ours, so
# it is bounded and filtered. 32 characters is twice the longest real name
# (`seven_day_sonnet`, 16).
WINDOW_NAME_MAX = 32
_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

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


def _window_name(name):
    """A payload's own key as a row key, or None if it does not qualify.

    THIS IS UNTRUSTED INPUT BECOMING A JSON KEY, and it is the one place in the
    tool where that happens. A key from someone else's payload can be any string
    JSON allows: a megabyte long, full of quotes and control characters, or
    chosen to collide with a field this row already uses. So it is filtered
    rather than trusted, and a name that does not qualify is skipped entirely --
    no cleaning up, no truncating, because a name this tool invented would then
    be recorded as if the payload had sent it.

    The rules, each refusing a specific way it could go wrong:

    - Letters, digits and underscore only, and no leading or trailing
      underscore. Deliberately wider than the lowercase Claude Code actually
      sends: skipping a window is the expensive direction, so the set says no
      only to characters that have no business in a key at all.
    - At most WINDOW_NAME_MAX characters, so one payload cannot decide how big a
      row is.
    - Not `context`, which would produce `context_pct` -- a key the reader
      already gives different treatment, because the session's context window
      has no reset and is not a quota. Two unrelated numbers under one name is
      worse than one missing one.
    - Nothing ending `_pct` or `_reset`, which would produce `x_pct_pct` and
      leave the reader's own naming rule reading two windows out of one.
    """
    if not isinstance(name, str) or not name or len(name) > WINDOW_NAME_MAX:
        return None
    if not _NAME_CHARS.issuperset(name):
        return None
    if name.startswith("_") or name.endswith("_"):
        return None
    if name == "context" or name.endswith("_pct") or name.endswith("_reset"):
        return None
    return name


def _windows(payload, now=None):
    """Every quota window the payload carries: a list of (name, pct, reset).

    THE PAYLOAD DECIDES WHICH WINDOWS EXIST, not this file. Nothing here looks
    up a name it already knew, so `seven_day_opus` and anything else added later
    is recorded the day it starts arriving rather than the day someone notices.

    A window needs a usable percentage to be worth a row at all -- the reader
    cannot use a reset without one, so a reset on its own is bytes in a bounded
    row buying nothing. An unusable RESET is kept, because the percentage still
    says how full the window is even when nobody can say when it turns over.
    """
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    found = []
    for raw, window in limits.items():
        name = _window_name(raw)
        if name is None or not isinstance(window, dict):
            continue
        pct = _pct(window.get("used_percentage"))
        if pct is None:
            continue
        found.append((name, pct, _reset(window.get("resets_at"), now)))
    if len(found) > MAX_WINDOWS:
        # Over the cap, keep the fullest -- and keep them in the payload's own
        # order, so a row's keys do not shuffle as percentages move.
        ranked = sorted(range(len(found)), key=lambda i: (-found[i][1], i))
        keep = set(ranked[:MAX_WINDOWS])
        found = [w for i, w in enumerate(found) if i in keep]
    return found


def _pct_keys(row):
    """Every key in a row that holds a window's percentage.

    Raw and unfiltered, because this decides what can be SHED and what counts as
    movement -- and for both of those, a key that got into the row by some route
    this file did not take still has to be reachable.
    """
    return [key for key in row
            if key.endswith("_pct") and key != "context_pct"]


def order_windows(names):
    """The documented pair first, then the rest by name.

    One ordering rule, used by the row, the status line and the gate's verdict,
    so a reader who learns the order once has learnt it everywhere.
    """
    names = set(names)
    return [n for n in DOCUMENTED_WINDOWS if n in names] \
        + sorted(names.difference(DOCUMENTED_WINDOWS))


def _named_windows(row):
    """The windows a row carries, in no particular order.

    A LEDGER IS AN INGEST SURFACE, so the same filter that guards a payload key
    on the way in guards a row key on the way out: a hand-edited ledger cannot
    put a megabyte-long window name into a gate's output either.
    """
    return [key[:-4] for key in _pct_keys(row)
            if _window_name(key[:-4]) is not None]


def window_names(row):
    """The windows a row carries, named and ordered.

    `ledger` reads thousands of rows a call and orders its answer once at the
    end, so its loop takes `_named_windows` instead. Measured 2026-08-25, a
    4,000-row ledger carrying two windows a row, median of nine reads: 77.5 ms
    ordering every row against 73.7 ms ordering once. That difference is small
    and is not the reason -- ordering four thousand rows to print one list is
    simply the wrong shape, and the milliseconds only confirm it.
    """
    return order_windows(_named_windows(row))


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
    (`_windows`, `_context`, `_cost`, `_model`) each guard their own slice of the
    payload -- so reaching into it with `.get` on the way in was the one
    unguarded step, and it turned `build_row(7)` into an AttributeError out of a
    module whose first property is that it may never raise.

    The window keys are the payload's own names with `_pct` and `_reset` on the
    end, which is why `five_hour` and `seven_day` still land exactly where they
    always did and every ledger written before this reads unchanged.
    """
    if not isinstance(payload, dict):
        payload = {}
    now = aware(now) or datetime.now(timezone.utc).astimezone()
    row = {"v": SCHEMA, "ts": now.isoformat(timespec="seconds")}
    for name, pct, reset in _windows(payload, now):
        row[name + "_pct"] = pct
        if reset is not None:
            row[name + "_reset"] = reset
    ctx_pct, ctx_size = _context(payload)
    for key, value in (("context_pct", ctx_pct),
                       ("context_size", ctx_size),
                       ("cost_usd", _cost(payload)),
                       ("model_id", _model(payload))):
        if value is not None:
            row[key] = value
    return row


def _shed(row):
    """Drop the one field a too-long row can most afford to lose. True if it did.

    THE ORDER IS THE WHOLE POINT, and it runs from what a reader can live
    without to what decides whether work may start:

    1. SHED_ORDER -- cost, context size, model id, context percentage.
    2. A reset with no percentage beside it. The reader needs both, so such a
       key is already dead weight and goes before any live window.
    3. The windows, LEAST-USED FIRST. A window at 4% says there is room, which
       is a thing the gate can find out again from any other row; a window at
       96% is the one that holds the work back, and losing it is the failure
       this tool exists to prevent. So the fullest window is the last number in
       the row to go. A percentage that will not parse sorts below every real
       one -- it can decide nothing, so it is the first of the windows worth
       losing -- and the name breaks ties, so two rows carrying the same numbers
       always shed the same field.
    """
    for key in SHED_ORDER:
        if key in row:
            del row[key]
            return True
    for key in sorted(row):
        if key.endswith("_reset") and (key[:-6] + "_pct") not in row:
            del row[key]
            return True
    ranked = []
    for key in _pct_keys(row):
        pct = _pct(row[key])
        ranked.append((-1.0 if pct is None else pct, key[:-4]))
    if not ranked:
        return False
    name = min(ranked)[1]
    for key in (name + "_pct", name + "_reset"):
        row.pop(key, None)
    return True


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
        if not _shed(row):
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
    """Has any quota percentage moved by a point or more since the last row?

    EVERY window, not a fixed pair: a per-model weekly allowance that jumps
    while the two documented windows sit still is real movement, and throttling
    it away would hide exactly the number this tool was extended to see.

    The context percentage is deliberately not one of these. It climbs on every
    single message, so counting it would mean nothing was ever throttled.
    """
    for field in _pct_keys(current):
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
    if stamp and fmt:
        try:
            segment += datetime.fromisoformat(stamp).strftime(fmt)
        except (TypeError, ValueError):
            pass
    return segment


def _label(name):
    """A window's short name for the status line.

    The documented two keep the labels they have always had. Anything else is
    shown under a name derived from its own: a window whose name extends a
    documented one -- `seven_day_opus` -- borrows that window's short label and
    keeps the rest, giving "7d opus", which is both short enough for a status
    line and unmistakably a different number from "7d". A name resembling
    nothing known is printed as it arrived, with its underscores opened out.
    """
    for prefix, (short, _fmt) in WINDOW_LABELS.items():
        if name == prefix:
            return short
        if name.startswith(prefix + "_"):
            return short + " " + name[len(prefix) + 1:].replace("_", " ")
    return name.replace("_", " ")


def line(row):
    """The one glanceable string, built from the row that was recorded, so what
    is shown and what is stored cannot drift apart.

    EVERY window in the row is shown. A window the line leaves out is a number
    nobody looks at, which is the whole failure this tool exists to prevent --
    so the line grows by a few characters rather than choosing for the reader.
    Only the documented two carry a reset in the line; the rest are a percentage
    alone, which keeps the string short, and `gate show` has the reset times.
    """
    if not isinstance(row, dict):
        return FALLBACK
    parts = []
    for name in window_names(row):
        _short, fmt = WINDOW_LABELS.get(name, (None, ""))
        segment = _segment(row, name, _label(name), fmt)
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
    # Any percentage at all is worth a row, which is not the same test as "one
    # of the two windows I know". A payload carrying only per-model windows was
    # a payload this recorded nothing for.
    if any(key.endswith("_pct") for key in row):
        append(row, path)
    return text


if __name__ == "__main__":
    try:
        sys.stdout.write(render(sys.stdin.read()))
    except Exception:                     # noqa: BLE001 -- never a dead line
        sys.stdout.write(FALLBACK)
