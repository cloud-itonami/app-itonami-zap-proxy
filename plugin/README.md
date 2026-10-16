# hermes-zap-proxy plugin

Hermes agent から `kotoba-lang/zap-proxy` の DAST 判定核を呼ぶ plugin。
プラグイン設計の正本は Hermes 本家 docs（`plugin.yaml` + `register(ctx)` + schemas/tools）。

## 構成（4 ファイル）

```
plugin/
├── plugin.yaml   # manifest v2: name / provides_tools / config_schema
├── __init__.py   # register(ctx) — 3 tools を toolset "zap-proxy" に登録
├── schemas.py    # LLM が読むツール仕様（引数・説明）
└── tools.py      # ハンドラ。承認記録 gate はここでコードとして強制
src/kotoba/zap_proxy/plugin_entry.cljk  # 範囲検査付き transport
```

## ツール

| tool | 何 | gate |
|---|---|---|
| `zap_scan` | passive scan（spider → ヘッダ/Cookie/情報漏えい検査） | 現在有効な承認記録が対象URL・パス・passive modeを含むこと |
| `zap_scan_active` | active scan（SQLi/XSS/CMDi/Trav/CRLF probe） | 上記 + 承認記録にactive modeが明記されていること |
| `zap_scan_rules` | ルール登録簿 8 件の表示 | 無し（読み取りのみ） |

## 安全境界（コードで強制、モデル判断に委ねない）

1. **承認記録の検証**: owner-only の `ZAP_PROXY_APPROVALS_FILE` が無い、記録が無効・期限切れ、
   approver/確認者が登録されていない、または対象が範囲外なら subprocess 前に拒否。
2. **範囲境界**: origin と path prefix を各リクエスト前に確認し、兄弟パスを拒否。HTTP
   redirect は追跡しない。
3. **返信を許諾にしない**: 質問、受領通知、自動返信、配送通知を解析して許諾記録に変換する
   経路はない。人間の担当者が権限・書面承認・対象範囲を確認して記録する。
4. **効果の分離**: production entrypoint は固定の kbb/Node または JVM transport を使い、
   request から callback を注入できない。callback mock は test-only namespace に隔離する。

## install（この workspace）

Set `ZAP_PROXY_APPROVALS_FILE` to an owner-only ledger only after the
operator has verified the approver's authority and recorded an explicit scope.
Without that file and a matching `approval_id`, both scan tools fail closed.

⚠ `hermes plugins list` の user plugins は `~/.hermes/plugins/` ではなく
**profile の plugins/（`~/.hermes/profiles/<p>/plugins/`）** を見る（実測 2026-09-05）。

## 旧バージョンの検証記録（2026-09-05）

The historical smoke-test notes above apply to the earlier allowlist-only gate.
They do not establish the new approval-record gate or authorize future scans.

The low-level spider and active functions are trusted in-process implementation
APIs that accept a transport callback for composition. They are not exposed
through the plugin schema: scan tools accept only target, approval ID, and page
cap; the adapter serializes only those values into the allowlisted EDN request,
and the process entrypoint rejects extra fields. Tests verify that callback-like
tool arguments are not forwarded and that the process boundary rejects transport
injection.
