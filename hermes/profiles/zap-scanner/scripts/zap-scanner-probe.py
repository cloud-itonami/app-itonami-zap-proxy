#!/usr/bin/env python3
"""zap-scanner daily probe: verify the zap-proxy decision core is runnable
and report the legacy target inventory. No network scan happens here. This
probe does not grant consent or establish scope; any real scan must pass the
reviewed approval-record gate in the plugin and transport."""
import json, subprocess, sys, os, datetime

HOME = os.path.expanduser("~")
REPO = os.environ.get("ZAP_PROXY_REPO", f"{HOME}/github/mithril-lang/zap-proxy")
out = {"date": datetime.datetime.now().isoformat(timespec="seconds"), "checks": []}

def check(name, ok, detail):
    out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

# 1. repo present and pinned
r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True)
check("zap-proxy checkout", r.returncode == 0, r.stdout.strip() or r.stderr.strip())

# 2. decision core loads (no network — pure require + rules registry read)
core_code = (
    "(require '[kotoba.zap-proxy.test-support :as test-support]) "
    "(def rules (read-string (slurp \"resources/zap_proxy/rules/rules.edn\"))) "
    "(println :RULES (count rules))"
)
r = subprocess.run(["kbb", "-e", core_code], capture_output=True, text=True, cwd=REPO, timeout=240)
rules_ok = ":RULES 8" in r.stdout
check("decision core loads + rules registry", r.returncode == 0 and rules_ok,
      "out=" + r.stdout.strip()[-120:] + " err=" + (r.stderr.strip()[-200:] if r.returncode else ""))

# 3. self-scan against a local mock (proves the pipeline end-to-end without network)
mock_code = (
    "(require '[kotoba.zap-proxy.test-support :as test-support] "
    "'[kotoba.zap-proxy.authorization :as authorization]) "
    "(with-redefs [authorization/authorization-scope "
    "(fn [_ _ mode max-pages] {:ok true :mode mode :max-pages max-pages "
    ":assets [{:origin \"http://t.local:80\" :path \"/\"}]})] "
    "(let [mock (fn [_] {:status 200 :headers {\"set-cookie\" \"sid=1; Path=/\"} "
    ":body \"<html><a href='/x?q=1'>x</a> password: hunter2</html>\"}) "
    "doc (test-support/scan {:target \"http://t.local/\" :approval-id \"test-only-probe\" "
    ":fetch-fn mock :active? false})] "
    "(println :TOTAL (:report/total doc))))"
)
r = subprocess.run(["kbb", "-e", mock_code], capture_output=True, text=True, cwd=REPO, timeout=240)
total_ok = ":TOTAL " in r.stdout and any(ch.isdigit() for ch in r.stdout.split(":TOTAL")[-1])
check("mock scan pipeline", r.returncode == 0 and total_ok,
      "out=" + r.stdout.strip()[-120:] + " err=" + (r.stderr.strip()[-200:] if r.returncode else ""))

# 4. legacy target inventory — presence is not authorization
inventory = f"{HOME}/github/mithril-lang/manifest/origin-domains.edn"
check("legacy target inventory present", os.path.exists(inventory),
      inventory if os.path.exists(inventory) else "missing")

out["verdict"] = "green" if all(c["ok"] for c in out["checks"]) else "red"
print(json.dumps(out, ensure_ascii=False, indent=1))
sys.exit(0 if out["verdict"] == "green" else 1)
