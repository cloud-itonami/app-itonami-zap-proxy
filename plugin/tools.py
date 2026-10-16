"""zap-proxy plugin tool handlers.

Architecture: the decision core is pure .cljc in the zap-proxy repo (judgment,
no I/O); this plugin is the host side. It:
  1. enforces a reviewed, time-bounded approval record in PYTHON, before any
     subprocess runs — a target allowlist or email reply is not authorization;
  2. shells out to the engine that can load the .cljc core on THIS host with
     the request on stdin (EDN) and reads the report on stdout (EDN):
       - `kbb -M -m kotoba.zap-proxy.kbb-scan` (Node transport, kbb/nbb
         engine — the production path; clojure -M cannot load .cljc files);
       - falls back to `clojure -M -m kotoba.zap-proxy.plugin-entry` (JVM,
         java.net.http transport) when kbb is absent.
  3. never passes raw shell strings — argv is a fixed list.

Transport note: the entry point injects its own HTTP effects (Node subprocess
in kbb-scan, java.net.http in plugin-entry) — the plugin never opens sockets
itself.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .authorization import authorize_request, load_ledger

def _repo_root() -> Path:
    """Resolve the checkout containing deps.edn without a machine-local path."""
    override = os.environ.get("ZAP_PROXY_REPO")
    if override:
        return Path(override).expanduser().resolve()
    plugin_dir = Path(__file__).resolve().parent
    for candidate in (plugin_dir.parent, plugin_dir):
        if (candidate / "deps.edn").is_file() and (candidate / "src").is_dir():
            return candidate
    return plugin_dir.parent


_REPO = _repo_root()

def _origin(url: str) -> str:
    """Return a canonical HTTP(S) origin and reject ambiguous target syntax."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("target must be a non-empty URL")
    value = url.strip()
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("target scheme must be http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("target must have a host and no embedded credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("target port is invalid") from exc
    port = port if port is not None else (443 if parsed.scheme.lower() == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("target port is invalid")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme.lower()}://{host}:{port}"


def _gate(target: str, active: bool, approval_id: str, max_pages: int) -> Optional[str]:
    """Fail closed unless a reviewed, unexpired record covers this URL and mode."""
    mode = "active" if active else "passive"
    if not approval_id:
        return "A reviewed security assessment approval record is required."
    if authorize_request(load_ledger(), approval_id, target, mode, max_pages=max_pages) is None:
        return "No current approval record covers this target and scan scope. Refusing to run."
    return None


# ---- subprocess bridge ------------------------------------------------------

def _run_core(edn_request: str) -> Dict[str, Any]:
    if not _REPO.exists():
        return {"error": f"zap-proxy repo not found at {_REPO}"}
    # Engine selection: kbb (nbb/Node) can load the .cljc core on this host;
    # clojure -M cannot (FileNotFoundException on .cljc namespaces), so it is
    # only the fallback for hosts that lack kbb.
    kbb = shutil.which("kbb")
    clojure = shutil.which("clojure")
    if kbb:
        argv = [kbb, "-M", "-m", "kotoba.zap-proxy.kbb-scan"]
    elif clojure:
        argv = [clojure, "-M", "-m", "kotoba.zap-proxy.plugin-entry"]
    else:
        return {"error": "neither kbb nor clojure on PATH"}
    try:
        env = os.environ.copy()
        env["ZAP_PROXY_REPO_ROOT"] = str(_REPO.resolve())
        r = subprocess.run(
            argv,
            input=edn_request,
            capture_output=True,
            text=True,
            cwd=str(_REPO),
            env=env,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"error": "scan timed out after 600s"}
    if r.returncode != 0:
        return {"error": "scan failed", "stderr": r.stderr[-500:]}
    return {"raw": r.stdout}


def _to_jsonable(raw: str) -> Any:
    """Best-effort EDN->JSON for the chat surface: numbers/counts are extracted
    by the clojure entry (it prints a :json line); full EDN is included for
    the agent to read."""
    return {"report_edn": raw}


# ---- tool handlers ----------------------------------------------------------
# The Hermes registry invokes handlers as handler(args_dict) — one positional
# dict of the tool-call arguments (see tools/registry.py dispatch). Accept that
# form natively while still supporting direct keyword calls in tests.

def _extract(args: Any, kwargs: Dict[str, Any]) -> tuple:
    """Tool args may arrive as one positional dict (registry dispatch) or as
    keywords (direct calls/tests). Normalize to (target, max_pages, approval_id).
    The
    handler forwards its own keyword defaults as None — only non-None
    keyword values take precedence over the args dict."""
    merged: Dict[str, Any] = dict(args) if isinstance(args, dict) else {}
    for k, v in kwargs.items():
        if v is not None:
            merged[k] = v
    target = str(merged.get("target") or "")
    approval_id = str(merged.get("approval_id") or "")
    raw_max_pages = merged.get("max_pages")
    if isinstance(raw_max_pages, bool) or (
        raw_max_pages is not None and not isinstance(raw_max_pages, (int, str))
    ):
        raise ValueError("max_pages must be an integer")
    max_pages = 50 if raw_max_pages is None else int(raw_max_pages)
    if not 1 <= max_pages <= 500:
        raise ValueError("max_pages must be between 1 and 500")
    return target, max_pages, approval_id


def _edn_request(target: str, active: bool, max_pages: int, approval_id: str) -> str:
    # JSON string syntax is a strict subset of EDN string syntax and safely
    # escapes quotes, backslashes and control characters from the URL.
    encoded_target = json.dumps(target, ensure_ascii=False)
    encoded_approval_id = json.dumps(approval_id, ensure_ascii=False)
    return (
        f"{{:target {encoded_target} :approval-id {encoded_approval_id} :active? {'true' if active else 'false'} "
        f":max-pages {max_pages}}}"
    )


def zap_scan(args: Any = None, target: Any = None, max_pages: Any = None, approval_id: Any = None, **kwargs: Any) -> str:
    target, max_pages, approval_id = _extract(args, {"target": target, "max_pages": max_pages, "approval_id": approval_id, **kwargs})
    refusal = _gate(target, active=False, approval_id=approval_id, max_pages=max_pages)
    if refusal:
        return json.dumps({"refused": refusal})
    edn = _edn_request(target, False, max_pages, approval_id)
    result = _run_core(edn)
    if "error" in result:
        return json.dumps(result)
    return json.dumps({"target": target, "mode": "passive", **_to_jsonable(result["raw"])})


def zap_scan_active(args: Any = None, target: Any = None, max_pages: Any = None, approval_id: Any = None, **kwargs: Any) -> str:
    target, max_pages, approval_id = _extract(args, {"target": target, "max_pages": max_pages, "approval_id": approval_id, **kwargs})
    refusal = _gate(target, active=True, approval_id=approval_id, max_pages=max_pages)
    if refusal:
        return json.dumps({"refused": refusal})
    edn = _edn_request(target, True, max_pages, approval_id)
    result = _run_core(edn)
    if "error" in result:
        return json.dumps(result)
    return json.dumps({"target": target, "mode": "active", **_to_jsonable(result["raw"])})


def zap_scan_rules(args: Any = None, **_: Any) -> str:
    if not _REPO.exists():
        return json.dumps({"error": f"zap-proxy repo not found at {_REPO}"})
    rules_file = _REPO / "resources/zap_proxy/rules/rules.edn"
    try:
        text = rules_file.read_text()
    except OSError as e:
        return json.dumps({"error": f"cannot read rules registry: {e}"})
    # crude but honest: hand the registry to the agent as EDN text
    return json.dumps({"registry_edn": text})
