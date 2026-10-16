"""hermes-zap-proxy plugin — wiring.

Exposes the kotoba-lang/zap-proxy DAST decision core as agent tools:
zap_scan (passive), zap_scan_active (intrusive, gated), zap_scan_rules.

The human-reviewed approval gate lives in tools.py and runs before any subprocess. Plugin
kind is standalone: opt-in via plugins.enabled in config.yaml.
"""
from __future__ import annotations

import json
import logging

from . import schemas, tools

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    ctx.register_tool(
        name="zap_scan", toolset="zap-proxy",
        schema=schemas.ZAP_SCAN, handler=tools.zap_scan,
        description=schemas.ZAP_SCAN["description"], emoji="🛡️",
    )
    ctx.register_tool(
        name="zap_scan_active", toolset="zap-proxy",
        schema=schemas.ZAP_SCAN_ACTIVE, handler=tools.zap_scan_active,
        description=schemas.ZAP_SCAN_ACTIVE["description"], emoji="🛡️",
    )
    ctx.register_tool(
        name="zap_scan_rules", toolset="zap-proxy",
        schema=schemas.ZAP_SCAN_RULES, handler=tools.zap_scan_rules,
        description=schemas.ZAP_SCAN_RULES["description"], emoji="🛡️",
    )
    logger.info("hermes-zap-proxy: registered 3 tools (gate enforced in tools.py)")
