"""Loopback ZAP JSON API facade for the Mithril-owned scanner.

Only the endpoints in COMPATIBLE_ENDPOINTS are implemented. An operator binds
one reviewed approval record at startup; an API caller cannot create a grant.
The scanner still rechecks the ledger before every network request.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .authorization import authorize_request, load_ledger
from .tools import _REPO, _edn_request


COMPATIBLE_ENDPOINTS = frozenset({
    ("core", "view", "version"),
    ("core", "view", "urls"),
    ("core", "view", "alerts"),
    ("core", "view", "numberOfAlerts"),
    ("spider", "action", "scan"),
    ("spider", "action", "stop"),
    ("spider", "view", "status"),
    ("spider", "view", "results"),
    ("ascan", "action", "scan"),
    ("ascan", "action", "stop"),
    ("ascan", "view", "status"),
    ("mithril", "view", "scanEvidence"),
})


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Scan:
    scan_id: str
    kind: str
    target: str
    state: str = "running"
    report: dict | None = None
    error: str | None = None
    process: subprocess.Popen | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


def _run_engine(scan: Scan, approval_id: str, max_pages: int) -> dict:
    engine = shutil.which("kbb")
    if not engine:
        raise RuntimeError("kbb scanner engine unavailable")
    entry = "kotoba.zap-proxy.kbb-scan"
    env = os.environ.copy()
    env.pop("MITHRIL_ZAP_API_KEY", None)
    env["ZAP_PROXY_REPO_ROOT"] = str(_REPO.resolve())
    env["ZAP_PROXY_OUTPUT_FORMAT"] = "json"
    request = _edn_request(scan.target, scan.kind == "ascan", max_pages, approval_id)
    proc = subprocess.Popen(
        [engine, "-M", "-m", entry], cwd=_REPO, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=(os.name == "posix"),
    )
    scan.process = proc
    if scan.cancel.is_set():
        _terminate(proc)
    try:
        stdout, _stderr = proc.communicate(input=request, timeout=600)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        proc.communicate()
        raise RuntimeError("scanner timed out") from None
    finally:
        scan.process = None
    if scan.cancel.is_set():
        raise RuntimeError("scan stopped")
    if proc.returncode != 0:
        raise RuntimeError(f"scanner process failed with exit {proc.returncode}")
    if len(stdout) > 4 * 1024 * 1024:
        raise RuntimeError("scanner report exceeded size limit")
    report = json.loads(stdout)
    if not isinstance(report, dict) or report.get("target") != scan.target:
        raise RuntimeError("scanner returned an invalid report")
    return report


def _terminate(proc: subprocess.Popen) -> None:
    """Stop the scanner and its network-helper children as one process group."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


class ScanManager:
    def __init__(self, approval_id: str, api_key: str, max_pages: int = 50, runner=None):
        if not approval_id or not api_key or len(api_key) < 32 or not 1 <= max_pages <= 50:
            raise ValueError("approval ID, 32-character API key and bounded page cap are required")
        self.approval_id, self.api_key, self.max_pages = approval_id, api_key, max_pages
        self.runner = runner or _run_engine
        self.lock = threading.Lock()
        self.scans: dict[str, Scan] = {}
        self.next_id = 1

    def start(self, kind: str, target: str) -> str:
        mode = "active" if kind == "ascan" else "passive"
        if authorize_request(load_ledger(), self.approval_id, target, mode, max_pages=self.max_pages) is None:
            raise APIError("NO_AUTHORIZATION", "No current reviewed approval covers this target and mode", 403)
        with self.lock:
            if any(s.state == "running" for s in self.scans.values()):
                raise APIError("SCAN_IN_PROGRESS", "Only one scan can run in this bounded adapter", 409)
            if len(self.scans) >= 16:
                raise APIError("SCAN_LIMIT", "Restart the adapter after exporting scan evidence", 409)
            scan_id = str(self.next_id)
            self.next_id += 1
            scan = Scan(scan_id, kind, target)
            self.scans[scan_id] = scan
        threading.Thread(target=self._work, args=(scan,), daemon=True).start()
        return scan_id

    def _work(self, scan: Scan) -> None:
        try:
            if scan.cancel.is_set():
                return
            report = self.runner(scan, self.approval_id, self.max_pages)
            with self.lock:
                scan.report = report
                scan.state = "stopped" if scan.cancel.is_set() else (
                    "complete" if report.get("collection_state") == "complete" else "partial")
        except Exception as exc:
            with self.lock:
                scan.error = str(exc)[:300]
                scan.state = "stopped" if scan.cancel.is_set() else "failed"

    def get(self, scan_id: str, kind: str | None = None) -> Scan:
        with self.lock:
            scan = self.scans.get(scan_id)
            if scan is None or (kind and scan.kind != kind):
                raise APIError("DOES_NOT_EXIST", "Unknown scan ID", 404)
            return scan

    def latest(self, kind: str | None = None) -> Scan | None:
        with self.lock:
            for scan_id in range(self.next_id - 1, 0, -1):
                scan = self.scans.get(str(scan_id))
                if scan is not None and (kind is None or scan.kind == kind):
                    return scan
            return None

    def stop(self, scan_id: str, kind: str) -> None:
        scan = self.get(scan_id, kind)
        with self.lock:
            if scan.state != "running":
                return
            scan.cancel.set()
            scan.state = "stopped"
            proc = scan.process
        if proc is not None:
            _terminate(proc)


def _one(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    if values is None:
        return default
    if len(values) != 1:
        raise APIError("BAD_REQUEST", "Repeated parameter: " + key)
    return values[0]


def _page(params: dict[str, list[str]], key: str, default: int, maximum: int) -> int:
    raw = _one(params, key, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise APIError("BAD_REQUEST", "Invalid " + key) from None
    if not 0 <= value <= maximum:
        raise APIError("BAD_REQUEST", "Out-of-range " + key)
    return value


def _alerts(report: dict) -> list[dict]:
    risk = {"info": ("Informational", "0"), "low": ("Low", "1"),
            "medium": ("Medium", "2"), "high": ("High", "3"),
            "critical": ("High", "3")}
    result = []
    for i, finding in enumerate(report.get("findings", []), 1):
        severity = str(finding.get("severity", "info")).lower()
        level, code = risk.get(severity, risk["info"])
        result.append({"id": str(i), "name": finding.get("title", ""),
                       "alert": finding.get("title", ""),
                       "pluginId": str(finding.get("rule_id", "")),
                       "risk": level, "riskcode": code, "confidence": "Medium",
                       "url": finding.get("url", ""), "param": finding.get("param", ""),
                       "evidence": finding.get("evidence", ""),
                       "solution": finding.get("solution", ""),
                       "sourceid": "mithril", "description": "Mithril first-party rule; see scan evidence for limitations."})
    return result


class Handler(BaseHTTPRequestHandler):
    manager: ScanManager
    server_version = "MithrilWebScanner/0.1"
    sys_version = ""

    def log_message(self, *_args) -> None:
        # ZAP API keys may be in query strings. Never log request targets/keys.
        return

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _write(self, body: dict, status: int = 200) -> None:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Mithril-Scanner", "true")
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self) -> None:
        try:
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{self.server.server_port}",
                            f"localhost:{self.server.server_port}"):
                raise APIError("BAD_HOST", "Loopback Host required", 403)
            if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise APIError("CROSS_ORIGIN", "Browser cross-origin requests are refused", 403)
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 4 or parts[0] != "JSON":
                raise APIError("UNSUPPORTED_FORMAT", "Only declared JSON endpoints are available", 404)
            route = tuple(parts[1:])
            if route not in COMPATIBLE_ENDPOINTS:
                raise APIError("UNSUPPORTED_OPERATION", "Endpoint is not implemented by Mithril", 501)
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
            if self.command == "POST":
                if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                    raise APIError("BAD_REQUEST", "POST requires form encoding")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 8192:
                    raise APIError("BAD_REQUEST", "Request body exceeds limit")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True, max_num_fields=20)
                for key, value in form.items():
                    params.setdefault(key, []).extend(value)
            key = _one(params, "apikey", self.headers.get("X-ZAP-API-Key", ""))
            if not hmac.compare_digest(key, self.manager.api_key):
                raise APIError("BAD_API_KEY", "A valid API key is required", 403)
            self._write(self._dispatch(route, params))
        except APIError as exc:
            self._write({"code": exc.code, "message": exc.message}, exc.status)
        except (ValueError, UnicodeError) as exc:
            self._write({"code": "BAD_REQUEST", "message": str(exc)[:200]}, 400)

    def _dispatch(self, route: tuple[str, str, str], p: dict[str, list[str]]) -> dict:
        component, operation, name = route
        if route == ("core", "view", "version"):
            return {"version": "0.1.0"}
        if operation == "action" and name == "scan":
            accepted = {"apikey", "url", "recurse", "contextName", "contextId", "subtreeOnly",
                        "scanPolicyName", "method", "postData", "inScopeOnly", "maxChildren"}
            if set(p) - accepted:
                raise APIError("UNSUPPORTED_PARAMETER", "Unknown scan option", 501)
            if _one(p, "recurse", "true").lower() not in ("true", ""):
                raise APIError("UNSUPPORTED_PARAMETER", "Non-recursive scans are not implemented", 501)
            unsupported = ("contextName", "contextId", "subtreeOnly", "scanPolicyName",
                           "method", "postData", "inScopeOnly", "maxChildren")
            if any(_one(p, field) for field in unsupported):
                raise APIError("UNSUPPORTED_PARAMETER", "Requested scan option is not implemented", 501)
            target = _one(p, "url")
            if not target:
                raise APIError("BAD_REQUEST", "url is required")
            scan_id = self.manager.start(component, target)
            return {"scan": scan_id}
        if operation == "action" and name == "stop":
            self.manager.stop(_one(p, "scanId"), component)
            return {"Result": "OK"}
        if route == ("mithril", "view", "scanEvidence"):
            scan = self.manager.get(_one(p, "scanId"))
            return {"scanEvidence": {"scanId": scan.scan_id, "state": scan.state,
                                     "error": scan.error, "report": scan.report}}
        if component in ("spider", "ascan"):
            scan_id = _one(p, "scanId")
            scan = self.manager.get(scan_id, component) if scan_id else self.manager.latest(component)
            if scan is None:
                raise APIError("DOES_NOT_EXIST", "No scan for this component", 404)
            if name == "status":
                if scan.state in ("failed", "partial", "stopped"):
                    raise APIError("INCOMPLETE_SCAN", "Scan did not complete; inspect Mithril scanEvidence", 409)
                return {"status": "100" if scan.state == "complete" else "0"}
            if name == "results":
                return {"results": (scan.report or {}).get("urls", [])}
        scan = self.manager.latest()
        if scan is not None and scan.state == "running":
            raise APIError("SCAN_IN_PROGRESS", "Latest scan is still running", 409)
        if scan is not None and scan.state in ("failed", "partial", "stopped"):
            raise APIError("INCOMPLETE_SCAN", "Latest scan is incomplete; inspect Mithril scanEvidence", 409)
        report = (scan.report if scan else None) or {}
        base = _one(p, "baseurl")
        if name == "urls":
            return {"urls": [u for u in report.get("urls", []) if not base or u.startswith(base)]}
        alerts = [a for a in _alerts(report) if not base or a["url"].startswith(base)]
        if name == "alerts":
            start = _page(p, "start", 0, 100000)
            count = _page(p, "count", len(alerts), 10000)
            return {"alerts": alerts[start:start + count]}
        if name == "numberOfAlerts":
            risk_id = _one(p, "riskId")
            if risk_id:
                if risk_id not in ("0", "1", "2", "3"):
                    raise APIError("BAD_REQUEST", "Invalid riskId")
                alerts = [a for a in alerts if a["riskcode"] == risk_id]
            return {"numberOfAlerts": len(alerts)}
        raise APIError("UNSUPPORTED_OPERATION", "Endpoint is not implemented by Mithril", 501)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mithril loopback ZAP JSON API facade")
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--max-pages", type=int, default=50)
    args = parser.parse_args()
    api_key = os.environ.get("MITHRIL_ZAP_API_KEY", "")
    manager = ScanManager(args.approval_id, api_key, args.max_pages)
    class BoundHandler(Handler):
        pass
    BoundHandler.manager = manager
    server = ThreadingHTTPServer(("127.0.0.1", args.port), BoundHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
