// SPDX-FileCopyrightText: 2026 The CTRLRun contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * The loopback client for the CTRLRun bridge.
 *
 * Every decision is made in the bridge. This file carries no policy, no allowlist and no
 * notion of what a risky tool is: a second place where that could be decided is a second
 * place it could be decided differently.
 */

export type Decision = "allow" | "deny" | "approval";

export type Answer = {
  decision: Decision;
  reason?: string;
  requestId?: string;
  callId?: string;
  detail?: {
    action?: string;
    arguments?: Record<string, unknown>;
    resource?: string | null;
    agent?: string;
    user?: string | null;
    environment?: string;
    expiresAt?: string;
    effectKey?: string;
    resolveWith?: string;
  };
};

export type HostDecision = "allow-once" | "allow-always" | "deny" | "timeout" | "cancelled";

export type BridgeOptions = {
  url: string;
  token: string;
  /** Kept under `before_tool_call`'s 15s fail-closed budget so the refusal explains itself. */
  timeoutMs: number;
};

export class BridgeUnreachable extends Error {}

export class BridgeClient {
  constructor(private readonly options: BridgeOptions) {}

  async decide(input: {
    tool: string;
    params: Record<string, unknown>;
    agentId?: string;
    sessionId?: string;
    runId?: string;
    toolCallId?: string;
  }): Promise<Answer> {
    return this.post("/v1/decide", input);
  }

  async resolve(input: {
    callId: string;
    decision: HostDecision;
    approver?: string;
    /**
     * The host's own frozen snapshot of the parameters the operator was shown. Never the
     * arguments the bridge sent back in the approval card: handing those back makes the
     * binding check trivially pass, which is manufacturing it.
     */
    approvedParams?: Record<string, unknown>;
  }): Promise<Answer> {
    return this.post("/v1/resolve", input);
  }

  async outcome(input: {
    callId: string;
    status: "ok" | "error" | "unknown";
    detail?: string;
  }): Promise<{ recorded: boolean; reason?: string }> {
    return this.post("/v1/outcome", input);
  }

  async health(): Promise<{ ok: boolean; policy: string; environment: string; mode: string }> {
    const response = await fetch(`${this.options.url}/v1/health`, {
      signal: AbortSignal.timeout(this.options.timeoutMs),
    });
    if (!response.ok) throw new BridgeUnreachable(`health ${response.status}`);
    return (await response.json()) as never;
  }

  private async post(path: string, body: unknown): Promise<never> {
    let response: Response;
    try {
      response = await fetch(`${this.options.url}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CTRLRun-Token": this.options.token,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(this.options.timeoutMs),
      });
    } catch (cause) {
      // Thrown, not swallowed. `before_tool_call` fails closed on a throw, which is the
      // behaviour a guardrail wants when its decision point is unreachable.
      throw new BridgeUnreachable(`CTRLRun bridge unreachable at ${this.options.url}`, { cause });
    }
    if (!response.ok) {
      throw new BridgeUnreachable(`CTRLRun bridge returned ${response.status}`);
    }
    return (await response.json()) as never;
  }
}
