# SPDX-FileCopyrightText: 2026 The CTRLRun contributors
# SPDX-License-Identifier: Apache-2.0
"""The four rules, through the bridge, with no HTTP and no OpenClaw.

Each test is one thing the plugin's README claims. A claim without one of these is a claim
nobody checked.
"""

from __future__ import annotations

import pytest

from ctrlrun_openclaw.bridge import Bridge

POLICY = """
schema: ctrlrun.policy/v2
actions:
  notes.read:
    decision: allow
  stripe.refund:
    effect: "refund:{payment_id}"
    rules:
      - when: { amount_gte: 0, amount_lte: 50000 }
        decision: allow
      - when: { amount_gte: 0, amount_lte: 500000 }
        decision: approve
      - decision: deny
"""


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    policy = tmp_path / "ctrlrun.yaml"
    policy.write_text(POLICY)
    monkeypatch.chdir(tmp_path)
    return Bridge(str(policy), "secret", agent="openclaw-test", outcome_timeout=2.0)


def _report(bridge: Bridge, answer: dict, status: str = "ok") -> dict:
    return bridge.outcome({"callId": answer["callId"], "status": status})


def test_an_allowed_call_is_allowed_and_its_outcome_is_recorded(bridge):
    answer = bridge.decide({"tool": "notes.read", "params": {}})
    assert answer["decision"] == "allow"
    assert _report(bridge, answer)["recorded"] is True


def test_an_unknown_tool_is_denied_because_nothing_is_default_allow(bridge):
    answer = bridge.decide({"tool": "rm_rf", "params": {}})
    assert answer["decision"] == "deny"
    assert answer["reason"] == "unknown_action"


def test_a_call_past_the_ceiling_is_denied_by_the_rule_that_refused_it(bridge):
    answer = bridge.decide(
        {"tool": "stripe.refund", "params": {"payment_id": "p", "amount": 900_000}}
    )
    assert answer["decision"] == "deny"
    assert answer["reason"].startswith("rule[")


def test_once_stays_once(bridge):
    first = bridge.decide({"tool": "stripe.refund", "params": {"payment_id": "p1", "amount": 1000}})
    assert first["decision"] == "allow"
    _report(bridge, first)
    second = bridge.decide(
        {"tool": "stripe.refund", "params": {"payment_id": "p1", "amount": 1000}}
    )
    assert second["decision"] == "deny"
    assert second["reason"] == "duplicate_effect:committed"
    assert second["detail"]["effectKey"] == "refund:p1"


def test_an_outcome_that_never_arrives_is_ambiguous_and_blocks_the_retry(bridge):
    """The one a decision-only guardrail cannot do, and the reason this adapter exists."""
    first = bridge.decide({"tool": "stripe.refund", "params": {"payment_id": "p2", "amount": 1000}})
    assert first["decision"] == "allow"
    # The host never reports. The bridge's outcome timeout elapses and the effect is unresolved.
    retry = _await_ambiguous(bridge, {"payment_id": "p2", "amount": 1000})
    assert retry["decision"] == "deny"
    assert retry["reason"] == "ambiguous_effect"
    assert retry["detail"]["resolveWith"] == "ctrlrun resolve refund:p2"


def _await_ambiguous(bridge: Bridge, params: dict, tries: int = 20) -> dict:
    import time

    for _ in range(tries):
        time.sleep(0.25)
        answer = bridge.decide({"tool": "stripe.refund", "params": params})
        if answer.get("reason") == "ambiguous_effect":
            return answer
    return answer


def test_an_approval_is_raised_then_granted_through_the_hosts_own_prompt(bridge):
    answer = bridge.decide(
        {"tool": "stripe.refund", "params": {"payment_id": "p3", "amount": 200_000}}
    )
    assert answer["decision"] == "approval"
    assert answer["requestId"].startswith("apr_")
    assert answer["detail"]["action"] == "stripe.refund"

    resumed = bridge.resolve(
        {
            "callId": answer["callId"],
            "decision": "allow-once",
            "approver": "openclaw:test",
            "approvedParams": {"payment_id": "p3", "amount": 200_000},
        }
    )
    assert resumed["decision"] == "allow"
    assert _report(bridge, answer)["recorded"] is True


def test_a_denied_approval_does_not_run_the_tool(bridge):
    answer = bridge.decide(
        {"tool": "stripe.refund", "params": {"payment_id": "p4", "amount": 200_000}}
    )
    assert answer["decision"] == "approval"
    resumed = bridge.resolve({"callId": answer["callId"], "decision": "deny"})
    assert resumed["decision"] == "deny"


def test_an_unresolved_host_approval_denies(bridge):
    """`timeout` and `cancelled` are not grants. The host denies on them too."""
    answer = bridge.decide(
        {"tool": "stripe.refund", "params": {"payment_id": "p5", "amount": 200_000}}
    )
    resumed = bridge.resolve({"callId": answer["callId"], "decision": "timeout"})
    assert resumed["decision"] == "deny"


def test_the_token_is_the_only_thing_that_authorizes_a_request(bridge):
    assert bridge.authorized("secret") is True
    assert bridge.authorized("Secret") is False
    assert bridge.authorized(None) is False
