// SPDX-FileCopyrightText: 2026 The CTRLRun contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Translating CTRLRun's answer into OpenClaw's result type, and the host's report back.
 *
 * Kept apart from the host wiring in `index.ts` on purpose: this is the part that decides
 * what happens to a tool call, and it should be testable without a Gateway, a bridge or a
 * network. `index.ts` registers hooks and moves bytes; everything that *decides* is here.
 */

import type { Answer } from "./bridge-client.js";

export type ApprovalMode = "native" | "block-and-retry";

export type HookResult = {
  params?: Record<string, unknown>;
  block?: boolean;
  blockReason?: string;
  requireApproval?: {
    title: string;
    description: string;
    severity?: "info" | "warning" | "critical";
    timeoutMs?: number;
    allowedDecisions?: ReadonlyArray<"allow-once" | "allow-always" | "deny">;
  };
};

/**
 * What the model is told when a call is refused.
 *
 * A refusal by CTRLRun is a statement that the tool did not run, so it names the rule that
 * refused it and, where there is one, the command that unblocks it. "Denied" on its own
 * gives the model nothing to do except try again.
 */
export function explain(reason?: string, resolveWith?: string): string {
  const head = reason ? `CTRLRun refused this call: ${reason}.` : "CTRLRun refused this call.";
  return resolveWith ? `${head} Resolve it with '${resolveWith}'.` : head;
}

export function describe(detail: Answer["detail"], requestId?: string): string {
  const card = detail ?? {};
  return [
    card.agent ? `Agent: ${card.agent}` : undefined,
    card.resource ? `Resource: ${card.resource}` : undefined,
    card.arguments ? `Arguments: ${JSON.stringify(card.arguments)}` : undefined,
    requestId ? `Request: ${requestId}` : undefined,
  ]
    .filter(Boolean)
    .join("\n");
}

/**
 * What to do when the bridge cannot be reached.
 *
 * Throwing would also be safe: this hook fails closed, so the host blocks the call either
 * way. Blocking explicitly is better because the reason survives. A thrown hook gives the
 * model a generic refusal, and an operator whose every tool call is suddenly blocked has
 * nothing to act on. This says which process is missing and how to start it.
 *
 * The failure mode this exists for is the quiet one: a plugin installed, no bridge running,
 * and a person who believes their agent is gated. Blocked-and-loud is the only honest state.
 */
export function unreachable(url: string): HookResult {
  return {
    block: true,
    blockReason:
      `CTRLRun blocked this call: its bridge is not answering at ${url}, so no policy ` +
      `could be applied and nothing may run. Start it with 'ctrlrun-openclaw-bridge ` +
      `--agent <name>', or install it with 'pip install ctrlrun-openclaw'. To stop gating ` +
      `tool calls entirely, disable the ctrlrun plugin.`,
  };
}

/**
 * The bridge's answer as an OpenClaw `before_tool_call` result.
 *
 * `approval` under `block-and-retry` becomes a refusal rather than a prompt. That mode exists
 * because `onResolution` cannot veto: once the operator answers `allow-once` the host
 * proceeds, so a second-pass refusal from CTRLRun has nowhere to go. Refusing up front closes
 * that window at the cost of the native prompt.
 */
export function toHookResult(
  answer: Answer,
  options: { toolName: string; approvalMode: ApprovalMode; approvalTimeoutMs?: number },
): HookResult {
  if (answer.decision === "allow") return {};

  if (answer.decision === "deny") {
    return { block: true, blockReason: explain(answer.reason, answer.detail?.resolveWith) };
  }

  if (options.approvalMode === "block-and-retry") {
    return {
      block: true,
      blockReason:
        `CTRLRun is holding this call for a human. Approve it with ` +
        `'ctrlrun approve ${answer.requestId ?? "<request-id>"}', then ask again.`,
    };
  }

  return {
    requireApproval: {
      title: `CTRLRun: ${answer.detail?.action ?? options.toolName} needs a human`,
      description: describe(answer.detail, answer.requestId),
      severity: "warning",
      allowedDecisions: ["allow-once", "deny"],
      // How long the operator has. Unresolved always denies, so this is the window in which
      // a person can answer, not a grace period: too short and a policy that asks for a
      // human is a policy that refuses.
      ...(options.approvalTimeoutMs ? { timeoutMs: options.approvalTimeoutMs } : {}),
    },
  };
}

/**
 * The host's report, as the outcome CTRLRun's executor should produce.
 *
 * `PluginHookAfterToolCallEvent` is
 * `{ toolName, params, runId?, toolCallId?, result?: unknown, error?: string, durationMs? }`.
 * Both `result` and `error` are optional, so an event carrying neither is not evidence of
 * success.
 *
 * `error` is the only branch that asserts non-execution. Everything unreadable becomes
 * `unknown`, which lands the effect in AMBIGUOUS rather than quietly clearing it: guessing
 * "it failed" from an absent field is how a committed effect gets retried. Note that for
 * exec and bash-family tools the host cannot currently report a nonzero exit at all
 * (openclaw#102961), so "ran" is the most this can honestly claim for those.
 */
export function outcomeOf(event: { result?: unknown; error?: string }): {
  status: "ok" | "error" | "unknown";
  detail?: string;
} {
  if (typeof event.error === "string" && event.error.length > 0) {
    return { status: "error", detail: event.error.slice(0, 500) };
  }
  if (event.result !== undefined) return { status: "ok" };
  return { status: "unknown", detail: "the host reported neither a result nor an error" };
}
