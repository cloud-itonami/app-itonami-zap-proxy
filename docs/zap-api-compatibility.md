# ZAP JSON API compatibility: first Mithril implementation slice

Mithril Web Scanner implements a bounded subset of the [ZAP API URL format](https://www.zaproxy.org/docs/api/) for existing client workflows. It is a first-party scanner and does not invoke or package OWASP ZAP. Compatibility is tracked per endpoint and parameter; the service does not claim whole-API or scanner-rule parity.

## Start the loopback facade

The operator must first create an owner-only approval ledger following [authorization.md](authorization.md), with a current reviewed target/path/mode grant. The API key is a separate random secret of at least 32 characters. Keep both outside source control. The facade listens only on `127.0.0.1` and accepts only a loopback `Host` header. It does not install a network proxy or TLS trust root.

```sh
export ZAP_PROXY_APPROVALS_FILE=/private/owner/approvals.json
export MITHRIL_ZAP_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 -m plugin.zap_api --approval-id reviewed-record-id --port 8090 --max-pages 50
```

Pass the key in `X-ZAP-API-Key` or the ZAP `apikey` query/form parameter. Query parameters may enter client-side logs, so the header is preferred for direct callers. Every endpoint requires the key. A browser `Origin` or cross-site request is refused.

## Implemented JSON endpoints

| Endpoint | Response | Boundary |
| --- | --- | --- |
| `core/view/version` | `version` | Reports Mithril adapter version `0.1.0`, not a ZAP release identity |
| `spider/action/scan` | `scan` string ID | Only `url` and recursive GET crawl; one run at a time; operator-bound approval |
| `spider/view/status` | `status` string `0` or `100` | `100` means this bounded crawl finished, not full app coverage |
| `spider/view/results` | `results` URL array | Fetched URLs in the engine report |
| `spider/action/stop` | `Result: OK` | Terminates the associated process when running |
| `ascan/action/scan` | `scan` string ID | Requires separate active-mode approval; current GET query probes only |
| `ascan/view/status` | `status` or `INCOMPLETE_SCAN` | Active probe transport completion is not yet measured, so current active runs remain partial |
| `ascan/action/stop` | `Result: OK` | Terminates the associated process when running |
| `core/view/urls` | `urls` array | Optional `baseurl` filter |
| `core/view/alerts` | `alerts` array | Optional `baseurl`, `start`, `count`; first-party rule IDs, ZAP-shaped fields |
| `core/view/numberOfAlerts` | integer `numberOfAlerts` | Optional `baseurl` and `riskId` |
| `mithril/view/scanEvidence` | `scanEvidence` | Mithril extension exposing exact report, coverage, limitations and terminal state |

Unsupported components, formats and scan options return a JSON `UNSUPPORTED_OPERATION`, `UNSUPPORTED_FORMAT` or `UNSUPPORTED_PARAMETER` error without starting a scan. Unknown, stopped or partial runs never return an empty clean finding set: standard alert views return `SCAN_IN_PROGRESS` or `INCOMPLETE_SCAN`; the Mithril evidence view remains available. The service currently keeps at most 16 in-memory runs, and does not provide persistent history, authentication contexts, Ajax/Client Spider, HTTP interception, WebSocket, OpenAPI import, ZAP rule IDs or report formats. These remain in [MITHRIL-1858](https://linear.app/kotobalabs/issue/MITHRIL-1858/deliver-mithril-web-scanner-to-qualified-zap-class-capability-coverage) and its child issues.

The adapter relies on the same owner ledger and per-request authorization checks as the native engine. It is not an authentication service for remote tenants, and must not be exposed on a public interface. A Registry package may pin this commit and expose its declared local capability; Registry installation alone does not provision an executor or authorize a target.
