import json
import os
import tempfile
import unittest
from unittest.mock import patch

from plugin import schemas, tools
from plugin.authorization import _canonical_url, authorize_request, load_ledger

NOW = 1_800_000_000


def approval_ledger():
    return {
        "version": 1,
        "authorized_approvers": ["owner@example.test"],
        "operators": ["security-operator"],
        "approvals": [{
            "id": "approval-123",
            "status": "approved",
            "approval_method": "human_verified_written_scope",
            "approver": "owner@example.test",
            "authority_verified_by": "security-operator",
            "authority_evidence_ref": "asset-registry:record-7",
            "reviewed_by": "security-operator",
            "recorded_by": "security-operator",
            "approval_evidence_ref": "mail-record:message-42",
            "approved_at": NOW - 3600,
            "reviewed_at": NOW - 1800,
            "not_before": NOW - 600,
            "expires_at": NOW + 86400,
            "assets": ["https://example.test/app/"],
            "methods": ["GET"],
            "modes": ["passive", "active"],
            "max_pages": 50,
        }],
    }


class OriginTest(unittest.TestCase):
    def test_canonicalizes_default_ports_and_ipv6(self):
        self.assertEqual(tools._origin("HTTPS://Example.COM/path"), "https://example.com:443")
        self.assertEqual(tools._origin("http://[::1]/"), "http://[::1]:80")

    def test_rejects_credentials_and_non_http_schemes(self):
        for value in ("https://user@example.com", "file:///tmp/x", "", "https://host:bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tools._origin(value)

    def test_approval_url_rejects_port_zero_and_encoded_traversal(self):
        self.assertIsNone(_canonical_url("https://example.test:0/app"))
        self.assertIsNone(_canonical_url("https://example.test/app/%2e%2e/admin"))
        self.assertIsNone(_canonical_url("https://example.test/app/%252e%252e/admin"))


class GateTest(unittest.TestCase):
    def test_explicit_record_covers_exact_path_and_mode(self):
        ledger = approval_ledger()
        self.assertIsNotNone(authorize_request(ledger, "approval-123", "https://example.test/app/users", "passive", max_pages=50, now=NOW))
        self.assertIsNotNone(authorize_request(ledger, "approval-123", "https://example.test/app/users", "active", max_pages=50, now=NOW))

    def test_questions_acknowledgments_auto_replies_and_delivery_notices_never_create_approval(self):
        ambiguous_replies = (
            "Could you send more information?",
            "Received, thank you.",
            "Automatic reply: I am out of office.",
            "Delivery status notification: message delayed.",
        )
        for raw_reply in ambiguous_replies:
            with self.subTest(reply=raw_reply):
                self.assertIsNone(authorize_request(raw_reply, "approval-123", "https://example.test/app", "passive", max_pages=1, now=NOW))

    def test_out_of_scope_asset_and_sibling_path_are_refused(self):
        ledger = approval_ledger()
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://outside.test/app", "passive", max_pages=1, now=NOW))
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://example.test/application", "passive", max_pages=1, now=NOW))

    def test_active_mode_requires_explicit_record_mode(self):
        ledger = approval_ledger()
        ledger["approvals"][0]["modes"] = ["passive"]
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://example.test/app", "active", max_pages=1, now=NOW))

    def test_expired_or_unverified_approval_is_refused(self):
        ledger = approval_ledger()
        ledger["approvals"][0]["expires_at"] = NOW
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://example.test/app", "passive", max_pages=1, now=NOW))
        ledger = approval_ledger()
        ledger["approvals"][0]["approver"] = "unknown@example.test"
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://example.test/app", "passive", max_pages=1, now=NOW))

    def test_page_cap_and_approved_window_are_enforced(self):
        ledger = approval_ledger()
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://example.test/app", "passive", max_pages=51, now=NOW))
        ledger["approvals"][0]["not_before"] = NOW + 1
        self.assertIsNone(authorize_request(ledger, "approval-123", "https://example.test/app", "passive", max_pages=1, now=NOW))

    def test_scan_tools_refuse_before_start_when_no_approval_record_exists(self):
        with patch("plugin.tools.load_ledger", return_value=None), patch.object(tools, "_run_core") as run:
            result = json.loads(tools.zap_scan({"target": "https://example.test/app", "approval_id": "missing"}))
        self.assertIn("refused", result)
        run.assert_not_called()

    def test_scan_tools_refuse_out_of_scope_before_start(self):
        with patch("plugin.tools.load_ledger", return_value=approval_ledger()), patch.object(tools, "_run_core") as run:
            result = json.loads(tools.zap_scan({
                "target": "https://example.test/application",
                "approval_id": "approval-123",
            }))
        self.assertIn("refused", result)
        run.assert_not_called()

    def test_external_tool_input_cannot_inject_a_transport_callback(self):
        callback = lambda _request: {"status": 200, "headers": {}, "body": ""}
        request = {
            "target": "https://example.test/app",
            "approval_id": "approval-123",
            "fetch_fn": callback,
            "send_fn": callback,
            "transport": "custom",
            "http-client": "custom",
        }
        with patch("plugin.tools._gate", return_value=None), \
             patch.object(tools, "_run_core", return_value={"raw": "{}"}) as run:
            tools.zap_scan(request)
        run.assert_called_once_with(
            tools._edn_request("https://example.test/app", False, 50, "approval-123")
        )
        self.assertIs(schemas.ZAP_SCAN["parameters"]["additionalProperties"], False)
        self.assertIs(schemas.ZAP_SCAN_ACTIVE["parameters"]["additionalProperties"], False)


class LedgerFileTest(unittest.TestCase):
    def test_owner_only_ledger_file_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "approvals.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(approval_ledger(), stream)
            with patch.dict(os.environ, {"ZAP_PROXY_APPROVALS_FILE": path}, clear=True):
                os.chmod(path, 0o644)
                self.assertIsNone(load_ledger())
                os.chmod(path, 0o600)
                self.assertEqual(load_ledger()["version"], 1)

    def test_duplicate_json_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "approvals.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"version":1,"version":1,"authorized_approvers":[],"operators":[],"approvals":[]}')
            os.chmod(path, 0o600)
            with patch.dict(os.environ, {"ZAP_PROXY_APPROVALS_FILE": path}, clear=True):
                self.assertIsNone(load_ledger())


class RequestTest(unittest.TestCase):
    def test_invalid_page_cap_does_not_fall_back_to_default(self):
        for value in (0, False, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tools._extract(
                    {"target": "https://example.test/app", "max_pages": value},
                    {},
                )

    def test_edn_request_escapes_untrusted_target(self):
        request = tools._edn_request('http://localhost/a" :active? true ;', False, 10, "approval-123")
        self.assertIn('\\"', request)
        self.assertEqual(request.count(":active?"), 2)
        self.assertIn(":active? false", request)
        self.assertIn(':approval-id "approval-123"', request)
        self.assertNotIn(":transport", request)

    def test_rules_use_checkout_relative_repo(self):
        result = json.loads(tools.zap_scan_rules())
        self.assertIn(":zap-proxy.rule/id", result["registry_edn"])


class EngineTest(unittest.TestCase):
    """The host engine must be the one that can load the .cljc core."""

    def test_prefers_kbb_when_both_available(self):
        calls = {}
        def which(name):
            return "/usr/bin/" + name if name in ("kbb", "clojure") else None
        class R: returncode, stdout, stderr = 0, "", ""
        def run(argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            return R()
        with patch.object(tools.shutil, "which", side_effect=which), \
             patch.object(tools.subprocess, "run", side_effect=run):
            tools._run_core("{}")
        self.assertEqual(calls["argv"][:4], ["/usr/bin/kbb", "-M", "-m", "kotoba.zap-proxy.kbb-scan"])
        self.assertEqual(calls["kwargs"]["cwd"], str(tools._REPO))
        self.assertEqual(calls["kwargs"]["env"]["ZAP_PROXY_REPO_ROOT"], str(tools._REPO.resolve()))

    def test_falls_back_to_clojure_when_kbb_absent(self):
        def which(name):
            return "/usr/bin/clojure" if name == "clojure" else None
        class R: returncode, stdout, stderr = 0, "", ""
        calls = {}
        def run(argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            return R()
        with patch.object(tools.shutil, "which", side_effect=which), \
             patch.object(tools.subprocess, "run", side_effect=run):
            tools._run_core("{}")
        self.assertEqual(calls["argv"][:4], ["/usr/bin/clojure", "-M", "-m", "kotoba.zap-proxy.plugin-entry"])
        self.assertEqual(calls["kwargs"]["cwd"], str(tools._REPO))
        self.assertEqual(calls["kwargs"]["env"]["ZAP_PROXY_REPO_ROOT"], str(tools._REPO.resolve()))

    def test_no_engine_is_a_reported_error(self):
        with patch.object(tools.shutil, "which", return_value=None):
            self.assertIn("error", tools._run_core("{}"))

    def test_direct_node_transport_refuses_before_network_without_approval(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        script = Path(__file__).with_name("zap_fetch.js")
        result = subprocess.run(
            [node, str(script), "file:///tmp/no-network", "missing", "passive"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("refused", result.stdout)

    def test_direct_node_transport_caps_synthetic_response(self):
        import http.server
        import shutil
        import subprocess
        import threading
        import time
        from pathlib import Path

        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b"x" * (1024 * 1024 + 4096))
                except BrokenPipeError:
                    pass

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            now = int(time.time())
            ledger = approval_ledger()
            record = ledger["approvals"][0]
            record.update(
                approved_at=now - 3600,
                reviewed_at=now - 1800,
                not_before=now - 600,
                expires_at=now + 3600,
                assets=[url],
                modes=["passive"],
                max_pages=1,
            )
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "approvals.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(ledger, stream)
                os.chmod(path, 0o600)
                script = Path(__file__).with_name("zap_fetch.js")
                result = subprocess.run(
                    [node, str(script), url, "approval-123", "passive"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={**os.environ, "ZAP_PROXY_APPROVALS_FILE": path},
                )
            self.assertEqual(result.returncode, 0)
            self.assertIn(":status 0", result.stdout)
            self.assertIn("response exceeded byte limit", result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
