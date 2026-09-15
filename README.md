# CTRLRun for OpenClaw

Every tool call OpenClaw is about to make, checked against your policy before it runs, and
recorded after.

```
openclaw tool call
   |
   | before_tool_call          fail-closed gate, 15s budget
   v
ctrlrun bridge  --->  allow        the host runs the tool
                      deny         block, with the rule that refused it
                      approval     OpenClaw's own approval prompt, in your channel
   |
   | after_tool_call           observation hook, may never fire
   v
receipt        committed | failed | ambiguous
```

## Why this is not another allowlist

There are already several tool-authorization plugins for OpenClaw, and every one of them is a
decision engine: policy in, allow or deny out. That is the first half. CTRLRun is the half
after the decision, and OpenClaw's own hook reference explains why that half is missing:

> `after_tool_call` | Observe | Observe tool results, errors, and duration

> For a policy requirement, use a fail-closed gate rather than assuming an observation or
> delivery hook will reject the operation on failure.

Observation hooks run concurrently, their return values are ignored, and on error the runner
logs and continues. So nothing in OpenClaw today distinguishes **the tool failed** from **we
do not know whether it committed**. A refund that reached Stripe and lost its reply on the way
back looks exactly like a refund that never left, and the agent retries.

CTRLRun holds `Control.execute` open across the tool call. An outcome that never arrives is
not lost, it is `AMBIGUOUS`: the effect stays reserved, and the next attempt on the same key
is refused until a human resolves it.

## What it does, run against a real policy

```
1. a refund inside the autonomous band
   decide  stripe.refund 50000            {"decision": "allow"}
   outcome ok                             {"recorded": true}

2. the same refund again, once stays once
   decide  stripe.refund 50000 (repeat)   {"decision": "deny", "reason": "duplicate_effect:committed",
                                            "detail": {"effectKey": "refund:pi_A"}}

3. above the ceiling
   decide  stripe.refund 900000           {"decision": "deny", "reason": "rule[2]"}

4. in the approval band, the host is asked
   decide  stripe.refund 500000           {"decision": "approval", "requestId": "apr_ad136a09..."}
   resolve allow-once                     {"decision": "allow"}

5. an unknown tool, nothing is default-allow
   decide  rm_rf                          {"decision": "deny", "reason": "unknown_action"}

6. allowed, then after_tool_call never fires
   decide  stripe.refund 10000            {"decision": "allow"}
   ...the outcome never comes...
   retry the same refund                  {"decision": "deny", "reason": "ambiguous_effect",
                                            "detail": {"resolveWith": "ctrlrun resolve refund:pi_D"}}
```

And the store afterwards:

```
$ ctrlrun effects
refund:pi_A  committed  attempt 1  act_6ecc501c...
refund:pi_C  committed  attempt 1  act_567d45c9...
refund:pi_D  ambiguous  attempt 1  act_dab68f53...

$ ctrlrun receipts
stripe.refund  allow/committed    refund:pi_A  openclaw-gateway
stripe.refund  allow/blocked      refund:pi_A  openclaw-gateway
stripe.refund  deny/denied        refund:pi_B  openclaw-gateway
stripe.refund  approve/committed  refund:pi_C  openclaw-gateway
rm_rf          deny/denied        -            openclaw-gateway
stripe.refund  allow/ambiguous    refund:pi_D  openclaw-gateway
stripe.refund  allow/blocked      refund:pi_D  openclaw-gateway
```

## Verified against a live host

OpenClaw 2026.9.4 (`3a9d69d`), Node 24.21.0, 2026-09-16:

```
$ openclaw plugins inspect ctrlrun --runtime --json
  "status": "loaded",  "activated": true,  "enabled": true,
  "hookCount": 3,      "configSchema": true

$ openclaw gateway
[gateway] http server listening (15 plugins: anthropic, bonjour, browser, canvas,
          ctrlrun, cua-computer, device-pair, file-transfer, geolocation, linux-node,
          memory-core, ollama, openai, talk-voice, xai; 1.1s)
[ctrlrun] gating tool calls: policy ctrlrun.yaml, mode enforce, environment production.
```

That last line is the `gateway_start` hook reading `/v1/health` off the bridge, so it is the
host's own log saying the gate is live and which policy it loaded. `before_tool_call` firing
on a real tool call is not yet exercised here: that needs a configured model provider.

Two things the host taught us that the docs do not:

- A packaged install requires compiled JavaScript at `./dist/index.js`. A TypeScript entry is
  only accepted for source checkouts and local development paths, so `npm run build` is not
  optional.
- `api.log?.info?.()` alone printed nothing on 2026.9.4. The plugin logs through the host
  where a logger exists and falls back to the console, because a guardrail that is silent
  about being down is the one failure it must not have.

## Install

Two halves: a Python bridge that holds the policy and the receipt chain, and an OpenClaw
plugin that is a client of it.

```bash
pip install ctrlrun-openclaw
ctrlrun init                       # writes ctrlrun.yaml and .ctrlrun/
export CTRLRUN_OPENCLAW_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
ctrlrun-openclaw-bridge --agent openclaw-gateway
```

```bash
openclaw plugins install clawhub:@ctrlrun/openclaw
```

```json5
{
  plugins: {
    entries: {
      ctrlrun: {
        config: {
          url: "http://127.0.0.1:8931",
          approvalMode: "native",
          // Omit `tools` to gate every tool, or name canonical ids: ["exec", "apply_patch"]
        },
      },
    },
  },
}
```

The bridge binds loopback and requires the shared secret on every request. It fronts your
approval authority; it has no business on another interface, and it refuses to start on one.

## The principal is yours, not the session's

`--agent` is set by you. It is deliberately not OpenClaw's `agentId`: a principal taken from
the calling session is not an authorization input, and CTRLRun removed exactly that from its
own gateway (`--principal-from-client-info`, SPEC-v0.3 §8.1) rather than keep it. Where you
configure an identity provider in the policy, it answers instead and wins.

## Known limits, stated rather than discovered

- **`onResolution` cannot veto.** Once the operator answers `allow-once`, OpenClaw proceeds.
  If CTRLRun's second pass then refuses (a changed precondition, an approval that no longer
  binds), the plugin has no way to stop the call. `approvalMode: "block-and-retry"` has no
  such window: it refuses outright and names the request for `ctrlrun approve`. This is the
  one gap worth raising upstream, and it is narrow.
- **Exec-family tools cannot report failure.** Per openclaw#102961, open and
  `needs-product-decision`, `after_tool_call` flattens the structured result to a bare string
  for exec and bash-family tools: `event.error` is set only for harness-level failures, and a
  command that ran and exited nonzero sets none. So for those tools the adapter can see that
  the call ran, not whether it worked. That is survivable, because CTRLRun's `FAILED` means
  *did not execute* and a nonzero exit is not that, but it does mean the receipt records less
  than it should. Watch that issue.
- **MCP tool calls do not reach the hook at all.** Per openclaw#119253, open, bundle-MCP tools
  are added to an embedded agent run without the wrapper that carries `before_tool_call`, so
  this plugin does not see them. Use `ctrlrun gateway` in front of the MCP server for those,
  which is the surface it was written for; the two are complementary rather than overlapping.
- **The bridge is Python.** OpenClaw is a Node install and this asks for a second runtime.
  That is the price of the guarantees living in one store with one receipt chain, rather than
  a second implementation of them that can be wrong in a second way.

Apache-2.0, same as the kernel.
