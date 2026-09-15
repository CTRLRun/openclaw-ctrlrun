### Summary

`PluginHookBeforeToolCallResult.requireApproval.onResolution` returns `Promise<void> | void`. Once the operator answers `allow-once`, the tool executes. A plugin that learns at resolution time that the call must not proceed has no way to say so.

Filed per the closing note on #46441, which asked for a narrow SDK issue "only if a concrete provider cannot be implemented with the existing hook context, block, rewrite, and approval semantics". The provider is [`@ctrlrun/openclaw`](https://www.npmjs.com/package/@ctrlrun/openclaw), published and installable.

Proposing one optional field on the existing result type, not a new subsystem:

```typescript
onResolution?: (decision: PluginApprovalResolution) =>
  Promise<void | { block: true; blockReason?: string }> | void;
```

Undefined behaves exactly as today.

### Why the existing semantics do not cover it

CTRLRun binds an approval to the exact action it was granted against and re-checks it immediately before the effect is reserved. The sequence:

1. `before_tool_call` returns `requireApproval`, because policy says this call needs a human. It cannot block while waiting: the hook's budget is 15s and it fails closed, so a human slower than that would have every approval denied.
2. The operator answers `allow-once`.
3. `onResolution` fires. The plugin re-presents the action under the granted approval, and the re-check refuses it: the logical effect is already in flight from another path.
4. **There is no way to return that refusal.** The host proceeds.

`block: true` at step 1 is not an alternative. At step 1 the call has not been refused; it needs a human, and no human has answered yet.

### Evidence

OpenClaw 2026.9.4, Node 24.21.0. Harness and full logs: https://github.com/CTRLRun/openclaw-ctrlrun/tree/main/repro

A held approval, granted by a person through the CLI:

```
Approval plugin:17239cb7-da98-40c5-9f29-06aaca890f47 resolved allow-once by runtime:cli.
```

The provider then refuses the re-presented call, and records that refusal:

```
2026-09-15T21:41:15.530Z  ctr_8ed789e7…  read  approve/blocked  read:.openclaw/tmp/probe.txt
```

And the host runs the tool regardless:

```
[tools] read  raw_params={"path":".openclaw/tmp/probe.txt"}
```

17 such `approve/blocked` rows in that run, each one a call the provider refused after the grant and the host executed anyway.

**Stated plainly so it is not overclaimed:** in this run the tool then failed on its own (`File not found`, my probe file was outside the workspace root). So this shows the host *proceeding to execute* past the provider's refusal, not a completed side effect. The `onResolution` signature is what makes that unavoidable — there is no channel to refuse on, whatever the tool then does.

Two related observations from the same runs, in case they are useful:

- An agent turn started with `openclaw agent` has no `turnSourceChannel`, so `plugin.approval.request` answers `decision: null` and the call is refused with `Plugin approval unavailable (no approval route)`. Correct, and worth knowing: a bare CLI turn can never reach the granted-approval path. A connected TUI supplies the route, and the host then waits (`plugin.approval.waitDecision 39147ms`).
- Granting by polling the CLI took about two minutes, by which point the run had already logged `plugin approval wait cancelled by run abort`. The window is shorter than it looks.

### Workaround in use, and why it is worse

The plugin ships `approvalMode: "block-and-retry"`, which refuses at step 1 and tells the operator to approve out of band and ask again. It closes the window and gives up the native approval prompt, which is the part operators actually want: the approval arriving in whatever channel the session is bound to.

### Alternatives considered

- **Re-check only in `before_tool_call`.** The re-check exists because state moves while a human deliberates; running it only before the human is asked is running it at the one moment it cannot detect anything.
- **A guardrail provider interface in core.** Refused in #46441 and #64868, correctly. This asks for neither a provider registry nor a config surface.
- **Cancel from `after_tool_call`.** An observation hook, and the hook reference says not to rely on one for a policy requirement.
