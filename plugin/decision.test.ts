// SPDX-FileCopyrightText: 2026 The CTRLRun contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Each test is one sentence the README claims about how a decision reaches OpenClaw.
 * No Gateway, no bridge, no network: this is the part that must be right on its own.
 */

import assert from "node:assert/strict";
import { describe as suite, test } from "node:test";

import { explain, outcomeOf, toHookResult } from "./decision.js";

const opts = { toolName: "exec", approvalMode: "native" as const };

suite("toHookResult", () => {
  test("an allowed call returns an empty result, which is how the host proceeds", () => {
    assert.deepEqual(toHookResult({ decision: "allow" }, opts), {});
  });

  test("a denial blocks and names the rule that refused it", () => {
    const result = toHookResult({ decision: "deny", reason: "rule[2]" }, opts);
    assert.equal(result.block, true);
    assert.match(result.blockReason!, /rule\[2\]/);
  });

  test("a duplicate effect blocks and does not invite a retry", () => {
    const result = toHookResult(
      { decision: "deny", reason: "duplicate_effect:committed" },
      opts,
    );
    assert.equal(result.block, true);
    assert.match(result.blockReason!, /duplicate_effect:committed/);
  });

  test("an ambiguous effect hands the model the command that resolves it", () => {
    const result = toHookResult(
      {
        decision: "deny",
        reason: "ambiguous_effect",
        detail: { resolveWith: "ctrlrun resolve refund:pi_D" },
      },
      opts,
    );
    assert.match(result.blockReason!, /ctrlrun resolve refund:pi_D/);
  });

  test("an approval raises the host's own prompt, and never allow-always", () => {
    const result = toHookResult(
      { decision: "approval", requestId: "apr_1", detail: { action: "stripe.refund" } },
      opts,
    );
    assert.equal(result.block, undefined);
    assert.equal(result.requireApproval?.title, "CTRLRun: stripe.refund needs a human");
    assert.deepEqual(result.requireApproval?.allowedDecisions, ["allow-once", "deny"]);
  });

  test("allow-always is never offered, because an approval binds to one action hash", () => {
    const result = toHookResult({ decision: "approval", requestId: "apr_1" }, opts);
    assert.ok(!result.requireApproval?.allowedDecisions?.includes("allow-always" as never));
  });

  test("block-and-retry refuses instead of prompting, and names the request", () => {
    const result = toHookResult(
      { decision: "approval", requestId: "apr_9" },
      { toolName: "exec", approvalMode: "block-and-retry" },
    );
    assert.equal(result.block, true);
    assert.match(result.blockReason!, /ctrlrun approve apr_9/);
    assert.equal(result.requireApproval, undefined);
  });

  test("the approval card carries the request's own fields and invents nothing", () => {
    const result = toHookResult(
      {
        decision: "approval",
        requestId: "apr_2",
        detail: { action: "stripe.refund", agent: "openclaw-gateway", arguments: { amount: 500000 } },
      },
      opts,
    );
    const description = result.requireApproval!.description;
    assert.match(description, /Agent: openclaw-gateway/);
    assert.match(description, /"amount":500000/);
    assert.match(description, /Request: apr_2/);
  });
});

suite("outcomeOf", () => {
  test("an error string is the only thing that asserts the tool did not run", () => {
    assert.deepEqual(outcomeOf({ error: "spawn ENOENT" }), {
      status: "error",
      detail: "spawn ENOENT",
    });
  });

  test("a present result is a commit", () => {
    assert.equal(outcomeOf({ result: "ok" }).status, "ok");
  });

  test("a falsy-but-present result is still a commit, not an absence", () => {
    assert.equal(outcomeOf({ result: "" }).status, "ok");
    assert.equal(outcomeOf({ result: null }).status, "ok");
    assert.equal(outcomeOf({ result: 0 }).status, "ok");
  });

  test("neither result nor error is unknown, never success", () => {
    assert.equal(outcomeOf({}).status, "unknown");
  });

  test("an empty error string is not an assertion of failure", () => {
    assert.equal(outcomeOf({ error: "" }).status, "unknown");
  });

  test("a long error is bounded so a receipt cannot be flooded", () => {
    assert.equal(outcomeOf({ error: "x".repeat(5000) }).detail!.length, 500);
  });
});

suite("explain", () => {
  test("a refusal says the tool did not run, not that it failed", () => {
    assert.match(explain("unknown_action"), /refused this call: unknown_action/);
  });

  test("a refusal with no reason still reads as a refusal", () => {
    assert.equal(explain(undefined, undefined), "CTRLRun refused this call.");
  });
});
