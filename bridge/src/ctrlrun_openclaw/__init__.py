# SPDX-FileCopyrightText: 2026 The CTRLRun contributors
# SPDX-License-Identifier: Apache-2.0
"""CTRLRun for OpenClaw: the bridge the OpenClaw plugin talks to.

The plugin is TypeScript and lives in the OpenClaw install. This package is the Python half:
it loads the operator's `ctrlrun.yaml`, answers `before_tool_call` from it, routes an
`APPROVE` through OpenClaw's own approval prompt, and keeps `Control.execute` open across the
tool call so the outcome reaches the receipt chain.
"""

from __future__ import annotations

from .bridge import Bridge, serve
from .interrupt import OpenClawInterrupt

__all__ = ["Bridge", "OpenClawInterrupt", "serve"]
