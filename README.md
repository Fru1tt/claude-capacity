# claude-capacity

Stop your Claude Code automation from launching into an empty tank.

It reads the quota numbers Claude Code already gives your status line, keeps them
in a local file, and answers with an exit code.

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

Python 3.9 or later. No dependencies.

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
$ python3 gate.py check --json              # the full answer, for a script
$ python3 gate.py show                      # where you stand
$ python3 gate.py compact                   # shrink an old file
```

In cron:

```cron
0 2 * * * cd ~/claude-capacity && python3 gate.py check --quiet --max-age 1440 && ~/bin/nightly-agent
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

**The ledger grows until you compact it.** One row per status-line render, in a
data directory `register` names — on a Mac,
`~/Library/Application Support/claude-capacity/`. A busy month of open sessions
is a few megabytes. Nothing compacts automatically, and `compact` does nothing
until the file is past 4 MB. Readers only walk the newest rows, so an
uncompacted file costs disk, never a wrong answer.

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

## Tests

```console
$ python3 tests/test_capacity.py
```

## License

MIT.
