#!/usr/bin/env python3
"""Every Telegram bridge test, offline. Exit 1 if any check failed.

    python3 -m tests.tg_bridge            # bare interpreter: sdk_shapes skips
    .venv/bin/python -m tests.tg_bridge   # with the SDK and httpx installed
"""
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

MODULES = ["inbox_offsets", "serialization", "render_chunks", "approvals_owner",
           "sessions_health", "media_voice", "delivery_notify", "sdk_shapes", "fixes_live", "proactive_quiet", "brief_fix", "browser_gate", "fetch_gate", "rulebook_gate", "shell_gate", "connector_rules"]

fails = []
for name in MODULES:
    module = importlib.import_module(f"tests.tg_bridge.{name}")
    fails += [f"{name}: {f}" for f in module.run()]

print(f"\n{'FAILED:\n  ' + chr(10).join('  ' + f for f in fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
