# ctrlrun-openclaw

The Python half of [CTRLRun for OpenClaw](https://github.com/CTRLRun/openclaw-ctrlrun): a
loopback bridge that answers OpenClaw's `before_tool_call` from a CTRLRun policy, and records
what the tool actually did.

```bash
pip install ctrlrun-openclaw
ctrlrun init
export CTRLRUN_OPENCLAW_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
ctrlrun-openclaw-bridge --agent openclaw-gateway
```

Then install the plugin in OpenClaw:

```bash
openclaw plugins install clawhub:@ctrlrun/openclaw
```

## Why a bridge and not a policy file

OpenClaw already has tool-authorization plugins, and each is a decision engine: policy in,
allow or deny out. This is the half after the decision.

`after_tool_call` is an observation hook. OpenClaw's own reference says handlers run
concurrently, return values are ignored, and to use a fail-closed gate rather than assume an
observation hook will reject anything on failure. So nothing in OpenClaw today separates *the
tool failed* from *we cannot tell whether it committed*.

This bridge holds `Control.execute` open across the host's tool call, which makes OpenClaw the
executor and its report the outcome. A call the host never reports on is not lost: the effect
stays reserved, the state is `AMBIGUOUS`, and the next attempt on the same key is refused
until a human runs `ctrlrun resolve`.

Approvals route through OpenClaw's own prompt using `InterruptApprovalProvider`, so the grant
is written by the same path `ctrlrun approve` uses, never by this adapter.

## Verified

OpenClaw 2026.9.4, Node 24.21.0. One real agent turn, both hooks firing:

```
[bridge] "POST /v1/decide"    200
[bridge] "POST /v1/outcome"   200

$ ctrlrun receipts
read  deny/denied      openclaw-gateway   # policy did not name this action
read  allow/committed  openclaw-gateway   # allowed, ran, outcome reported
```

The bridge binds loopback only and requires a shared secret on every request. It fronts your
approval authority and refuses to start on a non-loopback interface.

Apache-2.0. Full documentation, limits and known upstream issues in the
[repository README](https://github.com/CTRLRun/openclaw-ctrlrun).
