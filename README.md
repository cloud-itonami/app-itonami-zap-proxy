# zap-proxy

**kotoba-lang/zap-proxy は Kotoba/Clojure で書いた Web 脆弱性診断（DAST）
スタックである。** 明示的な診断許諾が確認できた Web アプリに対して spider → passive scan →
active scan（opt-in）→ report を実行する。

- OWASP ZAP のソースコード・スキャンルール・ポリシー・payload/wordlist・JAR は
  含まない。実行時に OWASP ZAP（daemon / API）を呼び出すこともない。
  判定ロジック・payload・ルール登録簿はすべてこの repo で書いたものである。
- repo 名の "zap" はプロジェクト名であり、OWASP ZAP プロジェクトとの提携・承認を
  意味しない。
- ルール ID（`10038`, `40018` など）は 5 桁の数値形式を採っているが、定義
  （タイトル・判定条件・payload・対処）はこの repo 独自のもので、OWASP ZAP の
  同じ番号のルールとは内容が一致しない場合がある（`resources/zap_proxy/rules/rules.edn`
  が正本）。

## 構成（この 1 repo に lib と app を同居させる）

MITM（intercepting）プロキシはまだ実装していない。通信処理は production entrypoint が
固定の Node/JVM transport を生成し、各 HTTP リクエスト前に許諾範囲を検証する。
EDN request が受け付けるのは target、approval-id、active?、max-pages のデータ項目だけで、
呼び出し元が独自 transport 関数や transport 選択肢を注入することはできない。

callback を使う決定核のテストは `test/kotoba/zap_proxy/test_support.cljk` に隔離する。
`spider` と `active` の低レベル callback API は production request surface ではなく、
production は固定 transport を内部で組み立てる `plugin_entry` または `kbb_scan` を使う。

| 層 | 何 | 出力 |
|---|---|---|
| **lib** `kotoba.zap-proxy.core` | 共有データモデル（request/response map、finding、same-origin 判定、URL 正規化） | pure map |
| **lib** `kotoba.zap-proxy.spider` | クロール（リンク/フォーム抽出、same-origin 判定） | 取得ページ列・訪問済み URL |
| **lib** `kotoba.zap-proxy.passive` | レスポンス非侵襲検査（ヘッダ、Cookie、情報漏えい） | findings |
| **lib** `kotoba.zap-proxy.active` | 侵襲検査（SQLi/XSS/path traversal 等の payload 適用と反応判定） | findings |
| **data** `resources/zap_proxy/rules/rules.edn` | ルール登録簿（EDN。ID/重要度/IPA・OWASP WSTG 参照） | 8 件 |
| **lib** `kotoba.zap-proxy.report` | 診断レポート生成（EDN/JSON） | report 文書 |
| **app** `kotoba.zap-proxy.plugin-entry` / `kotoba.zap-proxy.kbb-scan` | 固定 transport を使う production entrypoint | report 文書 |

設計規約（AGENTS.md / ADR-2609060001）:

- 判定核（何を脆弱とみなすか・payload は何か・same-origin はどう決まるか）は
  全て pure `.cljc`。固定の host transport が実通信を担い、テストは test-only callback
  harness を使う。
- 診断項目は `orgs/cloud-itonami/app-wvme/SPEC.tsv`（IPA「ウェブ健康診断仕様」
  125 項目 + OWASP STG）と `manifest/` 側の security governance
  （`kotoba-lang/security`）を参照する。**攻撃実行・侵入・兵器運用は scope 外** —
  これは所有者の明示許諾を記録で確認してから実施する防御側ツールである。

## Runtime boundary

The low-level spider and active functions are trusted in-process implementation
seams. They accept callbacks for transport composition, but untrusted tool
input cannot reach them: the registered schemas allow only target, approval ID,
and page cap; the adapter serializes an allowlisted EDN map; and production
entrypoints reject callback-like and unknown fields. Tests cover adapter
non-forwarding and EDN boundary rejection. Arbitrary code running inside the
trusted host process is outside this external-input boundary.

Production scans run through the registered Hermes tools or the fixed JVM/KBB
process entrypoints. Every run requires a current approval record that covers
the target, path, mode, and page cap. Test code uses mock callbacks through the
test-only `kotoba.zap-proxy.test-support` namespace; that namespace is not a
production API.

## Test

```sh
kbb -M:test
python3 -m unittest plugin.test_tools
```

## Hermes plugin

The repository root is also an installable Hermes plugin. Installation keeps
the Python host adapter, `.cljk` decision core, rule registry, and pinned
dependencies in one reviewed checkout:

```sh
hermes plugins install kotoba-lang/zap-proxy --ref <40-character-commit> --enable
```

Every scan is refused unless `ZAP_PROXY_APPROVALS_FILE` points to an owner-only
JSON ledger and the requested `approval_id` has a current, operator-reviewed
record covering the exact URL path and scan mode. The old
`zap_proxy_targets`/`allow_active` allowlist is inventory only and grants no
authorization. Both the Python adapter and direct `kbb`/JVM transports check
the record; each discovered path is checked before the request is sent, and
redirects are not followed. Incoming email is never parsed into authorization.

The operator-maintained ledger format and review requirements are documented in
[`docs/authorization.md`](docs/authorization.md). The host must provide Python 3
and either the `kbb` or `clojure` CLI. Direct KBB/JVM entry requires
`ZAP_PROXY_REPO_ROOT` to point to the absolute trusted checkout; the Hermes
adapter supplies it automatically. Helper paths are never resolved from the
current working directory.

## License

MIT License. Copyright (c) 2026 Kotoba Labs, Inc. See [LICENSE](LICENSE).
