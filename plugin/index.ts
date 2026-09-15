// SPDX-FileCopyrightText: 2026 The CTRLRun contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * CTRLRun for OpenClaw.
 *
 * Every tool call OpenClaw is about to make is checked against the operator's CTRLRun policy
 * before it runs, and what happened to it is recorded after. Four rules, each one a test in
 * the CTRLRun repository before it is a sentence here:
 *
 *   Exact means exact     changed arguments need a new approval
 *   Once stays once       the same effect key does not run twice
 *   Unknown means wait    an outcome nobody established is not a failure, and blocks a retry
 *   Every answer is kept  requests, decisions and results, refusals included
 *
 * The third is the one this plugin exists for. OpenClaw's `after_tool_call` is an observation
 * hook: handlers run concurrently, return values are ignored, and its own reference says to
 * use a fail-closed gate rather than assume an observation hook will reject anything. So a
 * tool call the host never reports on is not lost here. The effect stays reserved, and the
 * next attempt on the same key is refused until a human runs `ctrlrun resolve`.
 *
 * This file registers hooks and moves bytes. Everything that *decides* is in `decision.ts`,
 * which is tested without a Gateway.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { BridgeClient, BridgeUnreachable, type HostDecision } from "./bridge-client.js";
import { type ApprovalMode, outcomeOf, toHookResult } from "./decision.js";

type Config = {
  url?: string;
  token?: string;
  timeoutMs?: number;
  approvalMode?: ApprovalMode;
  tools?: string[];
};

const DEFAULTS = {
  url: "http://127.0.0.1:8931",
  timeoutMs: 10_000,
  approvalMode: "native" as const,
};

type Logger = {
  log?: {
    info?: (m: string) => void;
    warn?: (m: string) => void;
    error?: (m: string) => void;
  };
};

/**
 * Log through the host where it offers a logger, and to the console where it does not.
 *
 * Verified against OpenClaw 2026.9.4: `api.log?.info?.()` alone printed nothing, so a plugin
 * that only logged that way told the operator nothing about whether its gate was live. A
 * guardrail that is silent about being down is the failure this avoids.
 */
function say(api: Logger, level: "info" | "warn" | "error", message: string): void {
  const line = `[ctrlrun] ${message}`;
  const host = api.log?.[level];
  if (typeof host === "function") host(line);
  else console[level](line);
}

export default definePluginEntry({
  id: "ctrlrun",
  name: "CTRLRun",
  description: "Checks every tool call against a CTRLRun policy, and records what happened.",
  register(api) {
    const config = (api.config ?? {}) as Config;
    const token = config.token ?? process.env.CTRLRUN_OPENCLAW_TOKEN;
    if (!token) {
      throw new Error(
        "CTRLRun needs the bridge's shared secret: set plugins.entries.ctrlrun.config.token " +
          "or CTRLRUN_OPENCLAW_TOKEN. Without one, any process on this machine could answer " +
          "approvals.",
      );
    }
    const client = new BridgeClient({
      url: config.url ?? DEFAULTS.url,
      token,
      timeoutMs: config.timeoutMs ?? DEFAULTS.timeoutMs,
    });
    const approvalMode: ApprovalMode = config.approvalMode ?? DEFAULTS.approvalMode;

    /** Tool call id -> the bridge's call id, so the outcome reaches the right effect. */
    const inFlight = new Map<string, string>();
    const matcher = config.tools && config.tools.length > 0 ? { matcher: config.tools } : {};
    const keyOf = (toolCallId: string | undefined, runId: string | undefined, tool: string) =>
      toolCallId ?? `${runId ?? ""}:${tool}`;

    api.on(
      "before_tool_call",
      async (event, ctx) => {
        // A throw here fails closed: the host blocks the tool call. That is the correct
        // behaviour when the decision point is unreachable, and it is the host's default,
        // so BridgeUnreachable is deliberately not caught.
        const answer = await client.decide({
          tool: event.toolName,
          params: (event.params ?? {}) as Record<string, unknown>,
          agentId: ctx?.agentId,
          sessionId: ctx?.sessionId,
          runId: ctx?.runId ?? event.runId,
          toolCallId: event.toolCallId,
        });

        const key = keyOf(event.toolCallId, ctx?.runId ?? event.runId, event.toolName);
        const result = toHookResult(answer, {
          toolName: event.toolName,
          approvalMode,
        });

        if (answer.decision === "allow") {
          if (answer.callId) inFlight.set(key, answer.callId);
          return result;
        }
        if (!result.requireApproval) return result;

        // The host owns the waiting. This hook's budget is 15 seconds and it fails closed, so
        // blocking here would deny every approval a human was slower than that to answer.
        return {
          requireApproval: {
            ...result.requireApproval,
            onResolution: async (decision: HostDecision) => {
              if (!answer.callId) return;
              const resumed = await client.resolve({
                callId: answer.callId,
                decision,
                approver: `openclaw:${ctx?.sessionId ?? "session"}`,
                // The host's frozen snapshot of what the operator was shown, never the
                // arguments the bridge sent back in the card: handing those back would make
                // the binding check trivially pass.
                approvedParams: (event.params ?? {}) as Record<string, unknown>,
              });
              if (resumed.decision === "allow") inFlight.set(key, answer.callId);
            },
          },
        };
      },
      { ...matcher, timeoutMs: config.timeoutMs ?? DEFAULTS.timeoutMs },
    );

    api.on(
      "after_tool_call",
      async (event, ctx) => {
        const key = keyOf(event.toolCallId, ctx?.runId ?? event.runId, event.toolName);
        const callId = inFlight.get(key);
        if (!callId) return;
        inFlight.delete(key);
        const outcome = outcomeOf(event);
        try {
          await client.outcome({ callId, status: outcome.status, detail: outcome.detail });
        } catch (error) {
          // Nothing to do but say so. The bridge's own timeout lands the effect in AMBIGUOUS,
          // which is the correct state for an outcome that never arrived.
          say(
            api,
            "warn",
            `could not record the outcome of ${event.toolName}; the effect will be left ` +
              `unresolved. ${String(error)}`,
          );
        }
      },
      matcher,
    );

    api.on("gateway_start", async () => {
      try {
        const health = await client.health();
        say(
          api,
          "info",
          `gating tool calls: policy ${health.policy}, mode ${health.mode}, ` +
            `environment ${health.environment}.`,
        );
      } catch (error) {
        if (error instanceof BridgeUnreachable) {
          say(
            api,
            "error",
            `the bridge is not answering. Every gated tool call will be blocked until it is: ` +
              `start it with 'ctrlrun-openclaw-bridge --agent <name>'.`,
          );
        }
      }
    });
  },
});
