# Draft issue for openclaw/openclaw

**Do not file yet.** clawsweeper's bar is a concrete provider that cannot be implemented with
the existing semantics. File this once the plugin is running against a live host and the
"Evidence" section below points at a real run, not a design.

---

### Title

`[Feature]: let before_tool_call's onResolution refuse the call after an approval resolves`

### Summary

`PluginHookBeforeToolCallResult.requireApproval.onResolution` returns `Promise<void> | void`.
Once the operator answers `allow-once`, the tool executes; a plugin that learns, at resolution
time, that the call must not proceed has no way to say so.

Requesting one optional field on the existing result type, not a new subsystem:

```typescript
onResolution?: (decision: PluginApprovalResolution) =>
  Promise<void | { block: true; blockReason?: string }> | void;
```

Undefined behaves exactly as today.

### Why the existing semantics do not cover it

Per #46441's closing comment, this is filed only because a concrete provider hits the wall.
The provider is [`@ctrlrun/openclaw`](https://github.com/CTRLRun/openclaw-ctrlrun), which
gates tool calls against a CTRLRun policy.

CTRLRun binds an approval to the exact action it was granted against, and re-checks the
preconditions the human decided on immediately before the effect is reserved. The sequence:

1. `before_tool_call` returns `requireApproval`, because the policy says this call needs a
   human. The hook cannot block waiting: its budget is 15 seconds and it fails closed, so
   a human who takes longer than that would have every approval denied.
2. The operator answers `allow-once`.
3. `onResolution` fires. The plugin re-presents the action under the granted approval, and
   the re-check refuses it: a precondition moved between the human's decision and now, or the
   logical effect has since committed from another path.
4. **There is no way to return that refusal.** The host proceeds.

`block: true` at step 1 is not an alternative, because at step 1 the call has not been
refused: it needs a human, and a human has not answered yet.

### Workaround in use, and why it is worse

The plugin ships `approvalMode: "block-and-retry"`, which refuses at step 1 and tells the
operator to approve out of band and ask again. It closes the window, and it gives up the
native approval prompt, which is the part operators actually want: the approval arriving in
whatever channel the session is bound to.

### Evidence

**Still missing the one run that proves it, and that is deliberate.** The provider is verified
on OpenClaw 2026.9.4 for three of the four decision paths, each on a real agent turn:

```
read  deny/denied      openclaw-gateway   # policy did not name the action
read  allow/committed  openclaw-gateway   # allowed, ran, after_tool_call reported
read  approve/denied   openclaw-gateway   # needed a human, none attached, failed closed
```

The fourth path, an approval a human **grants**, cannot be produced headlessly: with no
approval-capable client attached the Gateway resolves the request in about 50ms as unresolved,
and unresolved approvals always deny. So reaching step 3 needs a person at a TUI or dashboard
answering `allow-once` while the provider's second pass refuses.

Before filing, run that once with an interactive approver and paste: the host version, the
`onResolution` decision, the provider's refusal, and the `after_tool_call` showing the host
executed anyway.

### Alternatives considered

- **Re-check in `before_tool_call` only.** The re-check exists because state moves while a
  human deliberates; running it only before the human is asked is running it at the one moment
  it cannot detect anything.
- **A new guardrail provider interface in core.** Refused in #46441 and #64868, correctly.
  This asks for neither a provider registry nor config surface.
- **Cancel from `after_tool_call`.** It is an observation hook, and the reference says not to
  rely on one for a policy requirement.
