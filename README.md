# claude-capacity

Gate your Claude Code automation on how much quota is left.

If you run agents, cron jobs or overnight builds against a Claude subscription,
something has to decide whether to launch. This reads the quota numbers Claude
Code already hands your status line, keeps them in a small ledger, and answers
with an exit code.

```console
$ python3 gate.py check --max-pct 80
GO -- every live window is under 80%: five-hour at 32%, seven-day at 72%
  five-hour   32.0%  resets in 120 min  measured 0 min ago
  seven-day   72.0%  resets in 4320 min  measured 0 min ago
$ echo $?
0
```

With `--quiet` it prints nothing, which is the form to put in front of a job:

```console
$ python3 gate.py check --max-pct 80 --quiet && ./run-the-expensive-thing
```

Python 3.9 or later. No dependencies. It is admission control, not a dashboard —
there are good status-line displays already and this does not try to be one.

## Install

**1.** Clone it and print the settings entry:

```console
$ git clone https://github.com/Fru1tt/claude-capacity
$ cd claude-capacity
$ python3 gate.py register
```

**2.** Paste that entry into `~/.claude/settings.json` yourself. `register` only
prints — merge it with whatever status line you already have.

**3.** Restart Claude Code and use a session for a moment. Nothing is recorded
until a status line actually renders, and the quota fields only appear after the
first API response in that session.

**4.** Check it:

```console
$ python3 gate.py show
```

Before step 3, `show` reports `UNKNOWN — the ledger holds no usable reading`.
That is it working: it has nothing yet and says so rather than inventing a number.

The ledger goes to the usual place for your platform
(`~/Library/Application Support/claude-capacity` on macOS, `XDG_DATA_HOME` on
Linux, `APPDATA` on Windows). `CLAUDE_CAPACITY_STORE` overrides it.

## Use

```console
$ python3 gate.py check --max-pct 80        # exit 0 to go, 1 to wait
$ python3 gate.py check --max-context 70    # also wait on a nearly full context window
$ python3 gate.py check --json              # the full answer, for a script to parse
$ python3 gate.py show                      # where you stand right now
$ python3 gate.py compact                   # shrink an old ledger
```

`check` is the only subcommand whose exit code carries an answer. `show` is a
display command and exits 0 whatever it finds, so never put it in front of `&&`.
Running `gate.py` with no subcommand asks which you meant and exits 2.

In a crontab:

```cron
0 2 * * * cd ~/claude-capacity && python3 gate.py check --quiet --max-age 1440 && ~/bin/nightly-agent
```

**Read `--max-age` before copying that line.** The ledger is written only when a
status line renders, and that only happens while you have a session open. At 2am
the newest reading is usually hours old, so the default of 30 minutes answers
`UNKNOWN`, exits 1, and the job silently never runs. The `1440` above accepts a
reading up to a day old, which suits a machine used most days. Pick the number
that matches how often you actually sit at it, and remember that a wider window
means acting on an older reading — it was true when measured, not necessarily now.

`--max-pct 80` is a starting point, not a recommendation. Let it run a couple of
weeks and set the limit below the point where you have actually run out mid-session.

## How it picks a reading

Each render records what both windows looked like at that moment. Reading them
back is not simply "take the newest row", for three reasons worth knowing:

**A row can be half stale.** One render carries a reading for each window, and
the five-hour reading can still be current while the seven-day reading beside it
has already reset. So each window is judged separately, and any reading whose
reset has passed is discarded however recent its row.

**Many rows share one reset.** Every render inside the same window reports the
same reset time, so ranking on the reset alone and keeping the first match gives
the oldest of them, understating what has been spent.

**Timestamps collide.** A row's timestamp has one-second resolution while the
throttle allows a second write inside the same second when a percentage has
moved. Two honest renders can carry identical stamps.

So the ranking key is the reset first, the observation time second, and position
in the file third. Both time parts are parsed before comparison — comparing
timestamps as text only works while every row carries the same UTC offset, and
that changes twice a year.

A reset further ahead than any real window, such as a year-9999 "never" sentinel,
is refused on the way in and ignored on the way out, since the reset ranks first
and one such value would otherwise outrank every genuine row.

## When it says it does not know

If the ledger holds no live reading, or the newest is older than `--max-age`, the
answer is `UNKNOWN` and the exit code is 1. Not knowing how much is left is not
the same as knowing there is room. A stale ledger usually means the status line
stopped running, which is exactly when a naive tool reads an old low number and
launches everything.

Pass `--allow-unknown` to start anyway.

## What it does not do

**No provider status or overage flag.** Those exist, but not in the status-line
payload — the agent toolkit's own type definitions put them on a rate-limit event
in the CLI's streaming output, a channel a status line never receives. Each window
here carries a percentage and a reset time, and that is all there is to read.

**No credential or endpoint access.** Payload only. Reading a token out of a
keychain to call an undocumented usage endpoint gets more data and breaks worse.

**No cost prediction.** The session's reported cost is recorded because the
payload includes it, but nothing estimates what a job will spend.

**No scheduling policy.** Whether *now* is a good moment, as opposed to whether
there is room at all, depends on things a ledger does not know.

## Limits

The quota fields only appear for Claude.ai subscribers, and only after the first
API response in a session. On an API-key setup there is nothing to read, and this
says so rather than inventing a number.

The payload schema is Anthropic's. It is documented, but fields have been added
across releases, so every field is treated as possibly absent and each row records
a schema version.

Only the five-hour and seven-day windows are understood, because only those two
are documented. Another window would be ignored rather than misread.

The append is one small write to a descriptor opened for append, which a local
filesystem makes atomic against other writers. Advisory locks are unreliable on
network filesystems, and on Windows there is no lock at all, so concurrent renders
there can lose a row to the throttle check.

## Similar projects

Several people have built pieces of this, and they are worth a look before you
use this one: [pareshrnayak/quota-guard](https://github.com/pareshrnayak/quota-guard),
[ruoxijiang/claude_quota_guard](https://github.com/ruoxijiang/claude_quota_guard),
[TakalaWang/claude-quota-guard](https://github.com/TakalaWang/claude-quota-guard),
[Ike-li/claude-quota-guard](https://github.com/Ike-li/claude-quota-guard) and
[raysonmeng/agent-quota-guard](https://github.com/raysonmeng/agent-quota-guard).
There are also established status-line tools that display this same data with
history and colour, and they do that better than this does.

## Tests

```console
$ python3 tests/test_capacity.py
```

No test runner, no dependencies.

## License

MIT.
