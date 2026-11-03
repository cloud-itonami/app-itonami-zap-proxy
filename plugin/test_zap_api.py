"""Contract tests for the declared ZAP JSON subset on the actual HTTP path."""
import json
import os
import tempfile
import threading
import time
import unittest
import http.server
import shutil
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from plugin.zap_api import Handler, Scan, ScanManager, _run_engine
from plugin.authorization import authorize_request, load_ledger


KEY = "a" * 32


def ledger(port):
    now = int(time.time())
    return {"version": 1, "authorized_approvers": ["owner@example.test"],
            "operators": ["operator"], "approvals": [{
                "id": "reviewed-1", "status": "approved",
                "approval_method": "human_verified_written_scope",
                "approver": "owner@example.test", "authority_verified_by": "operator",
                "reviewed_by": "operator", "recorded_by": "operator",
                "authority_evidence_ref": "asset:1", "approval_evidence_ref": "written:1",
                "approved_at": now - 120, "reviewed_at": now - 60,
                "not_before": now - 30, "expires_at": now + 3600,
                "assets": [f"http://127.0.0.1:{port}/app/"],
                "methods": ["GET"], "modes": ["passive", "active"], "max_pages": 5,
            }]}


class APITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp.name, "approvals.json")
        self.old_ledger = os.environ.get("ZAP_PROXY_APPROVALS_FILE")
        os.environ["ZAP_PROXY_APPROVALS_FILE"] = self.ledger_path
        self.calls = []

        def runner(scan, _approval, _max_pages):
            self.calls.append((scan.kind, scan.target))
            return {"target": scan.target,
                    "collection_state": "partial" if scan.kind == "ascan" else "complete",
                    "urls": [scan.target], "findings": [{
                        "rule_id": "10001", "severity": "low", "title": "Missing header",
                        "url": scan.target, "evidence": "header absent"}]}

        self.manager = ScanManager("reviewed-1", KEY, max_pages=5, runner=runner)
        class BoundHandler(Handler):
            pass
        BoundHandler.manager = self.manager
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BoundHandler)
        self.port = self.server.server_port
        self.target = f"http://127.0.0.1:{self.port}/app/"
        with open(self.ledger_path, "w", encoding="utf-8") as stream:
            json.dump(ledger(self.port), stream)
        os.chmod(self.ledger_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.old_ledger is None:
            os.environ.pop("ZAP_PROXY_APPROVALS_FILE", None)
        else:
            os.environ["ZAP_PROXY_APPROVALS_FILE"] = self.old_ledger
        self.tmp.cleanup()

    def call(self, path, params=None, *, key=True, method="GET", headers=None):
        params = dict(params or {})
        if key:
            params["apikey"] = KEY
        raw = urlencode(params)
        url = f"http://127.0.0.1:{self.port}{path}"
        data = raw.encode() if method == "POST" else None
        if method == "GET":
            url += "?" + raw
        request = Request(url, data=data, method=method,
                          headers=({"Content-Type": "application/x-www-form-urlencoded"} if method == "POST" else {}) | (headers or {}))
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def wait(self, scan_id):
        for _ in range(100):
            if self.manager.get(scan_id).state != "running":
                return
            time.sleep(0.01)
        self.fail("scan did not finish")

    def test_spider_client_calls_return_zap_shaped_results_and_alerts(self):
        status, body = self.call("/JSON/spider/action/scan/", {"url": self.target}, method="POST")
        self.assertEqual(status, 200)
        scan_id = body["scan"]
        self.wait(scan_id)
        self.assertEqual(self.call("/JSON/spider/view/status/", {"scanId": scan_id}), (200, {"status": "100"}))
        self.assertEqual(self.call("/JSON/spider/view/status/"), (200, {"status": "100"}))
        self.assertEqual(self.call("/JSON/spider/view/results/", {"scanId": scan_id}), (200, {"results": [self.target]}))
        self.assertEqual(self.call("/JSON/core/view/urls/")[1], {"urls": [self.target]})
        self.assertEqual(self.call("/JSON/core/view/numberOfAlerts/")[1], {"numberOfAlerts": 1})
        self.assertEqual(self.call("/JSON/core/view/numberOfAlerts/", {"riskId": 3})[1], {"numberOfAlerts": 0})
        self.assertEqual(self.call("/JSON/alert/view/numberOfAlerts/", {"riskId": 1})[1], {"numberOfAlerts": 1})
        self.assertEqual(self.call("/JSON/pscan/view/recordsToScan/")[1], {"recordsToScan": "0"})
        alerts = self.call("/JSON/core/view/alerts/", {"start": 0, "count": 10})[1]["alerts"]
        self.assertEqual((alerts[0]["pluginId"], alerts[0]["risk"]), ("10001", "Low"))
        self.assertEqual(self.call("/JSON/alert/view/alerts/", {"riskId": 1})[1]["alerts"], alerts)

    def test_active_partial_scan_fails_closed_and_evidence_remains_available(self):
        scan_id = self.call("/JSON/ascan/action/scan/", {"url": self.target})[1]["scan"]
        self.wait(scan_id)
        self.assertEqual(self.call("/JSON/ascan/view/status/", {"scanId": scan_id})[1]["code"], "INCOMPLETE_SCAN")
        self.assertEqual(self.call("/JSON/core/view/alerts/")[1]["code"], "INCOMPLETE_SCAN")
        evidence = self.call("/JSON/mithril/view/scanEvidence/", {"scanId": scan_id})[1]["scanEvidence"]
        self.assertEqual(evidence["state"], "partial")
        self.assertEqual(len(evidence["report"]["findings"]), 1)

    def test_scope_key_origin_and_unsupported_options_are_enforced(self):
        self.assertEqual(self.call("/JSON/core/view/alerts/", key=False)[1]["code"], "BAD_API_KEY")
        self.assertEqual(self.call("/JSON/spider/action/scan/", {"url": self.target},
                                   headers={"Origin": "https://other.test"})[1]["code"], "CROSS_ORIGIN")
        self.assertEqual(self.call("/JSON/spider/action/scan/", {"url": f"http://127.0.0.1:{self.port}/outside"})[1]["code"], "NO_AUTHORIZATION")
        self.assertEqual(self.call("/JSON/spider/action/scan/", {"url": self.target, "subtreeOnly": "true"})[1]["code"], "UNSUPPORTED_PARAMETER")
        self.assertEqual(self.call("/JSON/ajaxSpider/action/scan/", {"url": self.target})[1]["code"], "UNSUPPORTED_OPERATION")
        self.assertEqual(self.calls, [])

    def test_results_are_not_reported_clean_while_scan_is_running(self):
        entered = threading.Event()
        release = threading.Event()
        def slow_runner(scan, _approval, _max_pages):
            entered.set()
            release.wait(timeout=2)
            return {"target": scan.target, "collection_state": "complete", "urls": [], "findings": []}
        self.manager.runner = slow_runner
        scan_id = self.call("/JSON/spider/action/scan/", {"url": self.target})[1]["scan"]
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(self.call("/JSON/core/view/alerts/")[1]["code"], "SCAN_IN_PROGRESS")
        self.assertEqual(self.call("/JSON/spider/view/status/", {"scanId": scan_id})[1], {"status": "0"})
        release.set()
        self.wait(scan_id)

    def test_stop_marks_run_incomplete_and_keeps_evidence_state(self):
        entered = threading.Event()
        release = threading.Event()
        def slow_runner(scan, _approval, _max_pages):
            entered.set()
            release.wait(timeout=2)
            return {"target": scan.target, "collection_state": "complete", "urls": [], "findings": []}
        self.manager.runner = slow_runner
        scan_id = self.call("/JSON/spider/action/scan/", {"url": self.target})[1]["scan"]
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(self.call("/JSON/spider/action/stop/", {"scanId": scan_id}), (200, {"Result": "OK"}))
        release.set()
        self.assertEqual(self.call("/JSON/spider/view/status/", {"scanId": scan_id})[1]["code"], "INCOMPLETE_SCAN")
        self.assertEqual(self.call("/JSON/mithril/view/scanEvidence/", {"scanId": scan_id})[1]["scanEvidence"]["state"], "stopped")


class EngineIntegrationTest(unittest.TestCase):
    def test_actual_engine_json_report_on_authorized_loopback_target(self):
        if not shutil.which("kbb"):
            self.skipTest("kbb unavailable")

        class Target(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return
            def do_GET(self):
                body = b'<html><a href="/app/next">next</a></html>' if self.path == "/app/" else b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        target_server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        target_thread = threading.Thread(target=target_server.serve_forever, daemon=True)
        target_thread.start()
        old_ledger = os.environ.get("ZAP_PROXY_APPROVALS_FILE")
        try:
            with tempfile.TemporaryDirectory() as directory:
                approval_file = os.path.join(directory, "approvals.json")
                with open(approval_file, "w", encoding="utf-8") as stream:
                    json.dump(ledger(target_server.server_port), stream)
                os.chmod(approval_file, 0o600)
                os.environ["ZAP_PROXY_APPROVALS_FILE"] = approval_file
                target = f"http://127.0.0.1:{target_server.server_port}/app/"
                self.assertIsNotNone(authorize_request(load_ledger(), "reviewed-1", target, "passive", max_pages=5))
                check = subprocess.run(["python3", str(Path(__file__).with_name("authorization.py")),
                                        "--check", "reviewed-1", target, "passive", "5"],
                                       capture_output=True, text=True, timeout=5)
                self.assertEqual(check.returncode, 0, check.stderr)
                report = _run_engine(Scan("1", "spider", target), "reviewed-1", 5)
                self.assertEqual(report["target"], target)
                self.assertEqual(report["collection_state"], "complete")
                self.assertEqual(set(report["urls"]), {target, target + "next"})
        finally:
            if old_ledger is None:
                os.environ.pop("ZAP_PROXY_APPROVALS_FILE", None)
            else:
                os.environ["ZAP_PROXY_APPROVALS_FILE"] = old_ledger
            target_server.shutdown()
            target_server.server_close()
            target_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
