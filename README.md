# claude-capacity

[![tests](https://github.com/Fru1tt/claude-capacity/actions/workflows/tests.yml/badge.svg)](https://github.com/Fru1tt/claude-capacity/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![dependencies](https://img.shields.io/badge/dependencies-none-lightgrey)

**Quota-aware gating for Claude Code that runs without you — autonomous
agents, cron jobs, overnight work.**

Claude Code subscriptions have usage limits — a five-hour window and weekly
ones. You can see them in the app. Your scripts and scheduled agents cannot,
so an overnight job can start with the week nearly used up, burn what is
left, and leave you nothing in the morning.

This tool is that missing check. It does two things:

1. **Logs your usage.** A small hook in Claude Code's status line writes
   every limit reading — percent used, when it resets — to a file on your
   machine, automatically, while you work.
2. **Answers one question before a job runs: is there enough left?** The
   `check` command reads the newest numbers against a threshold you pick and
   answers with its exit code — 0 run, 1 don't.

```console
$ python3 gate.py check --max-pct 80 --quiet && ./run-the-overnight-job
```

Exit 0 to go, 1 to wait. Without `--quiet` it says why:

```console
$ python3 gate.py check --max-pct 80
WAIT -- seven-day-opus is at 96%, at or past the 80% limit; it resets in 5760 minutes
  five-hour         32.5%  resets in 180 min  measured 0 min ago
  seven-day         72.0%  resets in 5760 min  measured 0 min ago
  seven-day-opus    96.2%  resets in 5760 min  measured 0 min ago
  seven-day-sonnet  11.0%  resets in 5760 min  measured 0 min ago
  context           41.2%  measured 0 min ago
```

What that gets you:

- Overnight and scheduled agents only run when the week still has room for
  them, and never find out halfway through that it didn't.
- You keep quota for yourself. Gate your jobs at 60% and they stop launching
  while 40% is still left for your own sessions — when you wake up, and for
  the rest of the week.
- Your morning five-hour window is defensible. `--protect 08:00` refuses any
  job whose five-hour window would still be open at eight — so gated work can
  run all night and you still sit down to a full window.
- Anything that can read an exit code can use it: cron, a script, CI, an
  agent deciding on its own whether now is a good time to start.

Python 3.9 or later. No dependencies. Only the logging half is
Claude-specific — anything that writes the same rows can be gated the same
way.

## Install

```console
$ git clone https://github.com/Fru1tt/claude-capacity
$ cd claude-capacity
$ python3 gate.py register
```

`register` prints a settings entry. Paste it into `~/.claude/settings.json`
yourself, restart Claude Code, and use a session for a minute. The entry makes
`capture.py` your status line: it shows each window's percentage while recording
it. If you already have a status line you like, you will have to combine the two
yourself — `register` never writes anything. Then:

```console
$ python3 gate.py show
```

Until a session has rendered its status line there is nothing to read, and it
will say so rather than invent a number.

## Use

```console
$ python3 gate.py check --max-pct 80        # exit 0 to go, 1 to wait
$ python3 gate.py check --max-context 70    # also wait on a full context window
$ python3 gate.py check --protect 08:00     # never start what would hold the
                                            # five-hour window at eight
$ python3 gate.py check --protect 08:00 --protect-slack 120
                                            # ...unless its window dies within
                                            # two hours after eight
$ python3 gate.py check --json              # the full answer, for a script
$ python3 gate.py show                      # where you stand
$ python3 gate.py compact                   # trim by hand (it also trims itself)
```

In cron:

```cron
0 2 * * * cd ~/claude-capacity && python3 gate.py check --quiet --max-age 1440 --protect 08:00 && ~/bin/nightly-agent
```

That line gates at 80% — leaving `--max-pct` unstated means the default of 80,
not an absence of a gate.

## Worth knowing

**It fails closed.** No reading, or one older than `--max-age`, means "don't
start". Not knowing how much is left is not the same as knowing there is room.
Both answers come out as exit 1; the printed line, or `--json`, says whether you
are waiting on a spent window or on a ledger with nothing usable in it. If you
would rather start anyway when nothing is known, that is `--allow-unknown` — a
decision to make deliberately, not a default to inherit.

**Set `--max-age` for cron.** Numbers are only recorded while you have a session
open, so at 2am the newest reading is hours old. The default of 30 minutes would
skip the job every night. `1440` accepts a reading up to a day old. Know what an
old reading buys you: by 2am the evening's five-hour reading has passed its own
reset and is discarded, so the weekly windows alone decide. And the job's own
spend is never written back — a second job the same night is approved on the
same evening numbers as the first.

**Use `check`, not `show`.** Only `check` puts the answer in its exit code.
`show` always exits 0.

**`--protect` outranks everything**, including not knowing: inside the five
hours before a protected time the answer is wait, whatever the ledger says.
A window that dies minutes after the hour steals no morning, though — so
`--protect-slack 120` tolerates one running up to two hours past it. The
refusal is about overhang, not existence. The time is this machine's local
clock, and on the one night a year the clocks change the boundary can be off
by an hour.

**The file looks after itself.** One row per status-line render, in a data
directory `register` names — on a Mac,
`~/Library/Application Support/claude-capacity/`. When it passes 4 MB, the
write that noticed trims it to the newest 5,000 rows; `compact` does the same
by hand, and does nothing below the threshold. Readers only walk the newest
rows either way.

**It reads every window Claude Code sends**, not a fixed list. The docs describe
two — the five-hour session and the all-models week — but recent builds also send
per-model weekly windows such as `seven_day_opus` and `seven_day_sonnet`. Those
are checked too, and a new one starts working the day it appears, with no change
here. A window is never dropped for being new or undocumented. But the gate
answers on the windows it can read: one whose reading will not parse shows as
"no live reading" and decides nothing, and past 32 distinct window names the
rest become a printed count, not checked. Both are visible in the output — which
`--quiet` suppresses, so look at `show` now and then.

**But some limits are not in the payload at all.** If your usage screen shows a
per-model weekly bar that Claude Code does not send — a Fable weekly limit, as of
August 2026 — then no script on your machine can see it, including this. The
`/usage` screen shows it to you; it just isn't in the data a status line
receives, so nothing automated can gate on it. That is
[an open request](https://github.com/anthropics/claude-code/issues/88137), so
check whether it is still true when you read this.

**No overage or provider status.** The status line payload doesn't carry them —
just a percentage and a reset time per window. Those fields exist, but on a
different channel a status line never receives. Check for yourself:

```console
$ npm pack @anthropic-ai/claude-agent-sdk && tar -xzOf anthropic-ai-claude-agent-sdk-*.tgz package/sdk.d.ts | grep -i ratelimit
```

**Subscribers only.** The quota fields appear for Claude.ai plans, after the
first response in a session. With an API key there is nothing to read.

## Similar projects

Worth a look before you use this one:
[pareshrnayak/quota-guard](https://github.com/pareshrnayak/quota-guard),
[ruoxijiang/claude_quota_guard](https://github.com/ruoxijiang/claude_quota_guard),
[TakalaWang/claude-quota-guard](https://github.com/TakalaWang/claude-quota-guard),
[Ike-li/claude-quota-guard](https://github.com/Ike-li/claude-quota-guard),
[raysonmeng/agent-quota-guard](https://github.com/raysonmeng/agent-quota-guard).

## Design

Three files, no dependencies. The recorder may never raise — a status line
that throws is a dead status line, so every failure degrades to a plain
string. The gate fails closed — not knowing is never permission to start.
Every claim above is pinned by a test, and the suite runs in CI on Linux,
macOS and Windows, Python 3.9 through 3.13:

```console
$ python3 tests/test_capacity.py
```

## License

MIT.
