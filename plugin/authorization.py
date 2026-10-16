"""Fail-closed, record-based authorization for every hosted scan entry point.

The approval ledger is operator-managed and intentionally separate from email
inbox processing. Incoming messages are never parsed into grants. A human
operator must verify the named approver's authority and record the approved
assets, paths, scan modes, and expiry before a scan can start.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit


_ENCODED_BYTE = re.compile(r"%[0-9a-fA-F]{2}")


def _reject_duplicate_keys(pairs):
    """Reject ambiguous JSON objects instead of silently keeping the last key."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_url(value: Any, *, asset: bool = False) -> Optional[Tuple[str, str]]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if asset and (parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        host = parsed.hostname.lower()
    except ValueError:
        return None
    if ":" in host:
        host = f"[{host}]"
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    origin = f"{parsed.scheme.lower()}://{host}:{port}"
    path = parsed.path or "/"
    # Decode nested escapes too. Some servers/proxies decode more than once;
    # reject unusually deep encoding instead of letting those layers disagree.
    for _ in range(32):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if _ENCODED_BYTE.search(path):
        return None
    if "\\" in path or "\x00" in path or "//" in path:
        return None
    segments = path.split("/")
    if any(segment in (".", "..") for segment in segments):
        return None
    normalized = "/" + "/".join(segment for segment in segments if segment)
    if path.endswith("/") and normalized != "/":
        normalized += "/"
    return origin, normalized


def _path_in_scope(path: str, approved_path: str) -> bool:
    root = approved_path.rstrip("/") or "/"
    if root == "/":
        return path.startswith("/")
    return path == root or path.startswith(root + "/")


def load_ledger() -> Optional[Dict[str, Any]]:
    """Read only an owner-managed ledger; never fall back to target allowlists."""
    configured = os.environ.get("ZAP_PROXY_APPROVALS_FILE")
    if not configured:
        return None
    path = Path(configured).expanduser()
    try:
        # Validate the opened inode, not a path stat followed by a second open.
        # A writable parent directory must not let another local user swap in a
        # forged ledger between the permission check and the JSON read.
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return None
            if stat.S_IMODE(info.st_mode) & 0o077:
                return None
            data = json.load(stream, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def authorize_request(
    ledger: Any,
    approval_id: Any,
    target: Any,
    mode: str,
    *,
    max_pages: int = 1,
    now: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return a minimal scoped capability when a reviewed record covers target."""
    now = int(time.time()) if now is None else int(now)
    requested = _canonical_url(target)
    if (
        not isinstance(approval_id, str)
        or not approval_id.strip()
        or requested is None
        or mode not in ("passive", "active")
    ):
        return None
    if (
        not isinstance(ledger, dict)
        or not isinstance(ledger.get("version"), int)
        or isinstance(ledger.get("version"), bool)
        or ledger.get("version") != 1
        or not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
    ):
        return None
    approvers = ledger.get("authorized_approvers")
    operators = ledger.get("operators")
    records = ledger.get("approvals")
    if not isinstance(approvers, list) or not isinstance(operators, list) or not isinstance(records, list):
        return None
    matches = [r for r in records if isinstance(r, dict) and r.get("id") == approval_id]
    if len(matches) != 1:
        return None
    record = matches[0]
    if not isinstance(record.get("id"), str) or not record["id"].strip():
        return None
    if not record or record.get("status") != "approved":
        return None
    if record.get("approval_method") != "human_verified_written_scope":
        return None
    principal_fields = ("approver", "authority_verified_by", "reviewed_by", "recorded_by")
    if any(not isinstance(record.get(field), str) or not record[field].strip() for field in principal_fields):
        return None
    if record.get("approver") not in approvers or record.get("authority_verified_by") not in operators:
        return None
    if record.get("reviewed_by") not in operators or record.get("recorded_by") not in operators:
        return None
    if record.get("approver") == record.get("reviewed_by"):
        return None
    for field in ("authority_evidence_ref", "approval_evidence_ref"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return None
    approved_at = record.get("approved_at")
    reviewed_at = record.get("reviewed_at")
    not_before = record.get("not_before")
    expires_at = record.get("expires_at")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (approved_at, reviewed_at, not_before, expires_at)):
        return None
    if approved_at > now or reviewed_at < approved_at or reviewed_at > now:
        return None
    if not_before < approved_at or not_before > now or expires_at <= now or expires_at <= not_before:
        return None
    if expires_at - not_before > 31 * 24 * 60 * 60:
        return None
    modes = record.get("modes")
    methods = record.get("methods")
    max_pages_cap = record.get("max_pages")
    assets = record.get("assets")
    if (
        not isinstance(modes, list)
        or mode not in modes
        or not isinstance(methods, list)
        or any(not isinstance(method, str) for method in methods)
        or set(methods) != {"GET"}
    ):
        return None
    if not isinstance(max_pages_cap, int) or isinstance(max_pages_cap, bool) or not 1 <= max_pages <= max_pages_cap <= 500:
        return None
    if not isinstance(assets, list) or not assets:
        return None
    scoped_assets = []
    for raw_asset in assets:
        parsed_asset = _canonical_url(raw_asset, asset=True)
        if parsed_asset is None:
            return None
        origin, path = parsed_asset
        scoped_assets.append({"origin": origin, "path": path})
    origin, path = requested
    if not any(a["origin"] == origin and _path_in_scope(path, a["path"]) for a in scoped_assets):
        return None
    return {"mode": mode, "max_pages": max_pages_cap, "assets": scoped_assets}


def _edn_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def scope_edn(scope: Dict[str, Any]) -> str:
    assets = " ".join(
        "{:origin " + _edn_string(asset["origin"]) + " :path " + _edn_string(asset["path"]) + "}"
        for asset in scope["assets"]
    )
    return "{:ok true :mode " + _edn_string(scope["mode"]) + " :max-pages " + str(scope["max_pages"]) + " :assets [" + assets + "]}"


def cli() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--check", nargs=4, metavar=("APPROVAL_ID", "TARGET", "MODE", "MAX_PAGES"))
    args = parser.parse_args()
    if not args.check:
        return 2
    approval_id, target, mode, max_pages_raw = args.check
    try:
        max_pages = int(max_pages_raw)
    except ValueError:
        return 3
    scope = authorize_request(load_ledger(), approval_id, target, mode, max_pages=max_pages)
    if scope is None:
        return 3
    sys.stdout.write(scope_edn(scope))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
