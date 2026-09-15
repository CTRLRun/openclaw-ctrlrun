# @ctrlrun/openclaw

The OpenClaw half of [CTRLRun for OpenClaw](https://github.com/CTRLRun/openclaw-ctrlrun).

This package is a client. It registers `before_tool_call`, `after_tool_call` and
`gateway_start`, and carries no policy of its own: every decision is made by the
`ctrlrun-openclaw` bridge against the operator's `ctrlrun.yaml`. A second place where a tool
call could be decided is a second place it could be decided differently.

```bash
pip install ctrlrun-openclaw
ctrlrun init
export CTRLRUN_OPENCLAW_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
ctrlrun-openclaw-bridge --agent openclaw-gateway

openclaw plugins install clawhub:@ctrlrun/openclaw
```

See the repository README for the policy format, what each decision does, and the known
limits. Apache-2.0.
