# Reproducing a live tool call, and the approval that follows it

Three small programs that let you drive a real OpenClaw agent turn through this adapter with
no model credentials, no spend, and no person watching a terminal. They exist because every
interesting property of a tool gate only shows up when a real tool call goes through it.

Verified against OpenClaw 2026.9.4 on Node 24.21.0.

## Why each one is here

**`stub_model.py`** is an OpenAI-compatible server that reads the tools the host offered and
returns a `tool_call` for one of them. It is deterministic and costs nothing, and it makes
`before_tool_call` fire for real rather than being invoked by hand from a test.

**`drive_tui.py`** starts `openclaw tui` inside a pty and types a prompt into it. This is the
part that is not obvious: an agent turn started with `openclaw agent` from a shell has no
`turnSourceChannel`, so `plugin.approval.request` returns `decision: null` and the host
refuses the call with

```
[tools] read failed: Plugin approval unavailable (no approval route)
```

An approval that no one can answer always denies, which is correct, but it means a bare CLI
turn can never exercise the granted-approval path. The TUI connects as an approval-capable
client, and a turn inside it holds the approval open. With it connected the host waits:

```
[ws] ⇄ res ✓ plugin.approval.waitDecision 39147ms
```

**`autogrant.sh`** polls `openclaw approvals pending` and resolves the first held approval
with `allow-once`. It stands in for a person being quick. It has to be quick: polling by hand
took about two minutes and the run had already given up, logging

```
plugin approval wait cancelled by run abort: AbortError
```

## Running it

```bash
# 1. a policy that needs a human, and a bridge on it
ctrlrun init          # then set:  read: { effect: "read:{path}", decision: approve }
CTRLRUN_OPENCLAW_TOKEN=probe ctrlrun-openclaw-bridge --agent openclaw-gateway --verbose

# 2. the stub model
python3 stub_model.py

# 3. an OpenClaw profile pointed at both, with gateway token auth
#    models.providers.stub.baseUrl = http://127.0.0.1:8099/v1
#    plugins.entries.ctrlrun.config = { url, token, approvalTimeoutMs: 300000 }
openclaw --profile gap gateway

# 4. the turn, and the grant
./autogrant.sh &
python3 drive_tui.py
```

Then read `ctrlrun receipts`. Each decision path leaves its own row.

## What the paths look like

| Receipt | What produced it |
|---|---|
| `deny/denied` | the policy does not name the action |
| `allow/committed` | allowed, ran, `after_tool_call` reported |
| `approve/denied` | needed a human, no approval route, failed closed |
| `approve/blocked` | a human granted it, and CTRLRun refused on the second pass |
| `approve/failed` | granted, ran, the tool itself failed |

The fourth row is the one worth looking at, and `ISSUE-DRAFT.md` in the repository root is
about it: OpenClaw proceeded with the tool call after the provider refused, because
`onResolution` returns `Promise<void> | void` and has no way to say no.

## Two traps

The probe file must live **inside the agent workspace**. `/tmp/anything` is refused by
OpenClaw's own sandbox with `Path escapes sandbox root`, which looks like a gate failure and
is not one. Use a relative path under `.openclaw/tmp/`.

A failing tool makes the agent retry, and with a stub model that answers instantly the retry
loop is fast enough to write thousands of receipts in a minute. `stub_model.py` caps itself
for that reason.
