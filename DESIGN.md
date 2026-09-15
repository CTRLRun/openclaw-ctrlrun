# Does CTRLRun fit OpenClaw, and where

The review Jason asked for in Discord on 2026-09-16, against `openclaw/openclaw` `main` as of
2026-09-15, and the answer it produced.

## 1. The core interface is a closed door. Do not knock on it again

The obvious contribution, a guardrail provider interface in core, has been proposed and
refused twice:

- Issue #46441, "Pluggable Guardrail Provider Interface for tool authorization", closed
  2026-04-26 by clawsweeper.
- PR #64868, which implemented it, closed 2026-04-25 by steipete.

Both give the same reason. `VISION.md` keeps core lean, optional runtime capability ships as a
plugin, and guardrail providers belong on ClawHub. clawsweeper's closing comment names the
only condition under which a core issue is welcome again:

> open a narrow SDK/API issue **only if a concrete provider cannot be implemented with the
> existing hook context, block, rewrite, and approval semantics**

So the order is: build the provider, find the wall, then file. A feature proposal filed before
the provider exists gets the same boilerplate the last two got.

## 2. The hook surface fits CTRLRun almost exactly

`src/plugins/hook-before-tool-call-result.ts` on main:

```typescript
export type PluginHookBeforeToolCallResult = {
  params?: Record<string, unknown>;
  block?: boolean;
  blockReason?: string;
  requireApproval?: {
    title: string; description: string; severity?: "info" | "warning" | "critical";
    timeoutMs?: number; allowedDecisions?: Array<"allow-once" | "allow-always" | "deny">;
    onResolution?: (decision: PluginApprovalResolution) => Promise<void> | void;
  };
};
```

Three returns, three decisions:

| OpenClaw | CTRLRun |
|---|---|
| return `{}` | `ALLOW`, and the executor is entered |
| `block: true` | `DENY`, with the rule that refused it |
| `requireApproval` | `APPROVE`, through the framework's own interrupt |
| host executes the tool | the `executor` passed to `Control.execute` |
| `after_tool_call` | the executor's return, `NotExecuted`, or anything else |

That last row is why this is an *adapter* in CTRLRun's own sense (SPEC-v0.5 §2) rather than a
wrapper. The adapter surface exists for one reason, to route an `APPROVE` through a
framework's own human-in-the-loop primitive instead of raising past it, and OpenClaw has one.

Two host properties make the fit better than expected, both from
`docs/plugins/hooks/reference.md`:

- `before_tool_call` **fails closed**: "On thrown error or timeout: Fail closed: block the run,
  tool call, or install", 15 seconds by default. A guardrail whose decision point is down
  blocks tool calls, without the plugin implementing anything.
- Unresolved approvals **always deny**. `timeoutBehavior: "allow"` is deprecated in
  `hook-before-tool-call-result.ts` with a note that it is retained for compatibility only.

## 3. Where the fit is imperfect, and it is one place

The 15-second fail-closed budget means the interrupt cannot block inside the hook. A `wait()`
there would deny every approval a human took more than fifteen seconds to answer. So the hook
returns `requireApproval` immediately, the host owns the waiting, and the CTRLRun worker
thread blocks instead on a verdict the plugin posts back when `onResolution` fires.

That works, and it leaves exactly one residual window: **`onResolution` cannot veto.** Its
return type is `Promise<void> | void`. Once the operator answers `allow-once` the host
proceeds, so if CTRLRun's second pass then refuses, because a precondition moved or the
approval no longer binds to those arguments, the plugin cannot stop the call.

This is the narrow gap clawsweeper's closing comment left the door open for. It is not a
request for a guardrail subsystem; it is one optional field on an existing result type.
`ISSUE-DRAFT.md` is the filing, held until the provider is running against a live host so the
issue can point at it.

PR #142700, "prevent tool parameter changes after approval", open since 2026-09-09, is the
same concern one step earlier: it stops an approved call from executing rewritten parameters.
It is also what lets this adapter declare `carries_approved_arguments: true` honestly, because
the host's frozen snapshot is then a real second record rather than an echo of the request.

## 4. What is already possible with no plugin at all

OpenClaw's MCP client takes a URL:

```json5
{ mcp: { servers: { payments: { url: "http://127.0.0.1:8900/mcp", transport: "streamable-http" } } } }
```

`ctrlrun gateway --upstream <the real server>` intercepts `tools/call` and relays every other
method unchanged, so any MCP server OpenClaw talks to can be put behind a policy today, with
no code on either side. `ctrlrun mcp-operator` added as a second MCP server lets the held
approval be answered from the same conversation. That covers MCP tools; it does not cover
OpenClaw's own `exec`, `write`, `browser` or messaging tools, which is what the plugin is for.

## 4b. Two open bugs that bound what the plugin can claim

Both found by reading the tracker before writing the README, and both are load-bearing:

- **#102961**, open, `needs-product-decision`, diamond-rated. `after_tool_call` flattens the
  structured `ToolResult` to a bare string for exec and bash-family tools, so `event.error` is
  set only for harness-level failures and a nonzero exit reports nothing. A maintainer-framed
  comment already lays out three options. **This is the single best thread in the repo for
  CTRLRun to speak in**, because the whole issue is the distinction between *failed* and
  *cannot tell*, which is the kernel's subject. `no-new-fix-pr`, so a comment, not a PR.
- **#119253**, open, `needs-security-review`. Bundle-MCP tools bypass
  `wrapToolWithBeforeToolCallHook`, so MCP tool calls produce no audit records and the plugin
  hooks do not run for them. The plugin therefore does not gate MCP tools today, and
  `ctrlrun gateway` is the answer for those.

## 5. The competitive read

Four providers are already visible in those threads: CRE (`claude-rule-enforcer`), APort Agent
Guardrails, SHACKLE (`pyshackle`), and `openclaw-dataops-guardian`. Every one of them is a
decision engine. None of them models execution, so none can tell a failed call from one whose
outcome was never established, and OpenClaw's own observation hook cannot help them: it runs
concurrently, its return value is ignored, and the reference tells plugin authors not to rely
on it for a policy requirement.

That is the whole differentiation, and it is structural rather than a feature list.
