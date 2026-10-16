---
name: fleet-resource-allocation
description: Use for murakumo fleet allocation and the valueflow bot.
---

# fleet-resource-allocation

murakumo fleet（mac-mini 群）の計算資源を最適分配するための設計・実測手順。

## 実測現在地（2026-09-04）

- ノード: zebulun/judah/levi/simeon/dan/joseph/benjamin/asher(operator)、各 10コア。
  naphtali・issachar は unreachable（tailnet 要確認）。
- **外向き egress はノードごとに非対称**: levi は github/pypi/hf 全通、judah は
github+hf 通、zebulun/benjamin は全 external 不通（0.003 秒で即 reject —
LAN 内の何かが返答）。AGENTS.md の「egress 有り」実測（2026-08-05）は今は
再現しない。ノード横断でモデル重みが必要なときは egress のあるノードが
入口になる。
- CI 負荷: fleet-ci EMA の gate は大半 7〜45秒。稼働率は 5% 未満 — 余剰は膨大。
- `murakumo-main` alias（qwen3.8-27b）は b70 hosted slot 常設。

### 16GB mini 画像生成 PoC 実測（2026-09-04/05、judah M1 Max 16GB）

- **成功経路**: mflux + `filipstrand/Z-Image-Turbo-mflux-4bit`（事前量子化
  4bit ミラー、~5.9GB on disk）。1024x1024 8step、**21.9秒/step、約3分/枚、
  Peak MLX memory 5.70GB**。SIGKILL 無しで安定。
- **SIGKILL (exit 137) の原因と教訓**: `-q 4` / `-q 8` は**フル精度重み
  （~33GB）を一旦ロードしてから量子化する**ので 16GB では必ず kill される。
  16GB では `-q` を使わず事前量子化ミラーを `--model` で指定する。
  `--low-ram` を付ける。`iogpu.wired_limit_mb` の調整は解決にならない
  （物理 16GB が天井）。
- FLUX.1-schnell は gated（HF token 必須）→ z-image-turbo で代替。
- **disk 注意**: フル精度 Z-Image（31GB）を DL して失敗→削除という事故が
  起きる。judah は 228GB 中一度 1.9GB まで落ちた。DL 前に `df -h` 確認。
- 動画（Wan 等）は 16GB ではスループット不出 — 画像を第一候補にする。
- リソース競合: judah では inga witness node（nbb, 100% CPU 常駐）と
  litellm が常駐。生成 job はこれらと共存できたが、swap 6GB 使用中。
  生成前に重い常駐 process の有無を確認する。
- PoC 証跡: /tmp/mflux-judah.png（本地）+ judah:/tmp/mflux-test.png。
  実画像を vision 検証済み（リンゴの写真、破綻無し）。

## murakumo-main 推論スループット実測（2026-09-05、murakumo-tok bot が継続測定）

- **registry 宣言**: 35.56 tok/s aggregate @ conc 4（2026-08-31 測定、
  `GET /v1/models` の `capacity-measured-aggregate-tok-s`。Token 必須、
  未認証は 403 error 1010 / 404）。
- **実測（2026-09-05 17:02 JST）**: conc1=5.7 / conc2=11.1 / conc4=3.19 (502×1/4) /
  conc8=4.21 tok/s (502×4/8)。**conc2 が実用上限**、conc4+ は
  `HTTP 502 murakumo fleet unreachable TimeoutError`。宣言値とは −81% 乖離。
  その後も劣化進行（conc2 6.69、conc1 avg 47.5→75.8s）— エルロ 0 のまま
  応答が伸びるのはサーバ側キューイングのパターン。
- **継続測定は profile `murakumo-tok`**（cron d08b199efa22 毎朝 9時、
  `scripts/murakumo_infer_bench.py`、台帳 `workspace/tok-ledger.jsonl`）。
  手動でベンチを打つ前に台帳を見ること。ベンチ script は User-Agent を
  ブラウザ風にしないと 403 (error 1010) になる。並列ベンチは conc 2 まで、
  それ以上は 1 回だけ（連打禁止、SOUL 規則）。

## 分配の原則

1. **CI を最優先、生成ジョブを余剰 slot に。** CI gate は秒単位なので干渉は
   軽微。LPT assign（tick.cljs）と生成 queue の 2 層にする。
2. **仮説で台数を増やさない。** demand（engagement/売上）が実測できるまで
   PoC は 1 ノード。「測れなかった測定を成功として報告しない」の適用先。
3. **生成ごとに実測秒数を EMA 台帳に記録**（fleet-ci-cost.edn と同型）。
4. 重みは egress のあるノードで取得し、LAN 内で配る。

## valueflow/xmile 経済面（設計済み、実装未）

- **語彙**: `kotoba-lang/ws-valueflo-vocabulary`（REA、経済語彙の正本）
- **計算**: `kotoba-lang/ws-valueflo-algorithms`（network-based algorithm 8 本）
- **SD**: `kotoba-lang/dynamics`（stock/flow/loop + Meadows）+
  `kotoba-lang/loop-system-dynamics`（orchestrator、`refresh_and_run.cljs` で
  BMC metrics から実データ給電）
- **XMILE**: `kotoba-lang/org-oasis-open-xmile`（OASIS XMILE 1.0 mirror）—
  外部経済モデルの import 面。⚠ ADR-2608153000: SD の flow は連続 rate で
  VF の flow ではない — **変換できない別形式として並存させる**（統合しない）。
- **thermal routing**: `orgs/cloud-itonami/toi`（Energy Order Protocol の
  compute leg。site-score で分配提案 — observe only、G1 map-not-job-kill）。

### BigQuery public → R2 Iceberg 経路 (2026-09-05 実測)

- **認証の現在地**: gcloud は `jun784@gmail.com` で browser flow 認証済み
  (`--no-browser` の remote-bootstrap は redirect_uri エラーで使えない。
  この Mac には browser があるので通常 flow を使う)。
  project は `jun784` (BigQuery Sandbox, billing 無し)。
- **Sandbox 制約**: `bq extract` (GCS export) は billing が要るので不可。
  代わりに `bq query --format=csv --max_rows=200000` の年区切り分割で
  全行取得できる (939,014 行を 10 chunk で取得実測)。
- **R2 Iceberg 経路**: PyIceberg RestCatalog + kagi の
  `CLOUDFLARE_R2_DATA_CATALOG_TOKEN` (catalog Bearer) と
  `CLOUDFLARE_R2_ACCESS_KEY` (JSON blob: access-key-id/secret-access-key/
  endpoint/bucket) で `cloud_itonami` namespace に append。
  `cloud_itonami` には既に gleif/otent/hyakka/sukashi 等 41 table が在る。
- **認証の正解 (2026-09-05 確定)**: catalog Bearer は **macOS Keychain
  `security find-generic-password -s gftd.cf -a API_TOKEN -w`** (app-hyakka
  docs/r2-data-catalog.md に文書化済みの解決順)。kagi の
  CLOUDFLARE_R2_DATA_CATALOG_TOKEN は rotate 競合で 403 になることがある —
  Keychain 側が正本。**S3 credentials は catalog が vend する** —
  PyIceberg に s3.access-key-id を渡さなくても append できる
  (s3.region=auto のみ必須)。kagi の CLOUDFLARE_R2_ACCESS_KEY JSON blob は
  実質不要。
- **Sandbox の max_rows**: query あたり最大 250k 行 (200k で切れたこともあるので
  chunk は 250k 指定+行数検算で確認)。939k 行 = 10 chunk、6.3M 行 = 57 chunk。
- venv: `~/.gftd/iceberg-venv` (pyarrow + pyiceberg)。
- 受領証: `itonami/workspace/ingest-receipts/bq-public-*.json`
  (2026-09-05 現在 5 table landing: bls_c_cpi_u / bls_cpi_u / usa_names /
  hacker_news_2023 / google_trends_jp_top3_7d、全て read-back 検証済み)。
- **wiki 公開 (2026-09-05 現在地)**: 認証トークンは kagi の
  `hyakka-proof-upload-token` (grok-bots worker の
  `HYAKKA_PROOF_UPLOAD_TOKEN` env と timing-safe 比較)。これで 401 は通る。
  **しかし上流 kotobase datom plane が全滅している**:
  (1) `/ipld/:cid` PUT/GET が 405 (ADR-2608159100 が宣言する surface が live で消えている。datoms.kotobase.net も DNS 不解決)
  (2) transact が 502 CPU limit (resident 直近 200 件中 199 件が 502、1 件 401)
  — hyakka resident の projection 成功率 **0%** (2026-09-05 実測。コードの
  comment には 2026-09-03 時点で 50% とあり、悪化)。受領証は
  `itonami/workspace/findings/kotobase-ipld-put-405-20260905.json`。
  修復担当は net-kotobase/control-plane 側 (surface 再宣言か
  KOTOBASE_DATOM_SERVICE_URL の付け直し)。

## fleet-kaizen (2026-09-05 作成)

- profile `fleet-kaizen`: 品質・安定性・性能の kaizen bot。正本測定 script は
  `~/.hermes/scripts/fleet-kaizen-audit.py` (no_agent 計測 → LLM が 1 iteration
  1 finding を propose)。cron は **every 3h (23 */3 * * *、8 runs/日、owner 指示で
  高頻度化 2026-09-05)**、ledger は
  `fleet-kaizen/workspace/kaizen-ledger.jsonl` (append-only)。
- 実測初回 (2026-09-05): 品質最悪 itonami-maint 17% / murakumo-cloud 33% /
  fleet-watchdog 自身 39%。stability の反復 error クラス: Fire claim lost 162x、
  429 claude 62x、TERMINAL_CWD lock 109x (write 56 + read 53)、shutdown 中断 47x、
  murakumo unreachable 46x、provider 欠落 44x。
- **罠**: openrouter-free (z-ai/glm-5.3-flash) が stream を hang させることがある
  ("waiting for stream response" のまま 21 分停止を初回 run で実測)。これは
  kaizen 自身が測った 23x "Non-streaming API call timed out after 90s" と同じ
  クラス。cron job が hang したら scheduler の timeout を信じて放置し、
  次の fire に任せる。
- **stream hang の根因 (2026-09-05 特定、fleet-wide)**: cron session が
  "receiving stream response" のまま永遠に開いたままになる zombie が
  **24h で 101 本** (100+ profile、median age 80分、max 20.3時間)。
  openrouter 直 call では再現しない (同一 key/model/60KB で TTFB 1.2-1.8s)。
  stream stale detector (180s) は first chunk 以降しか動かず、create() 後
  first chunk 前の沈黙は httpx read timeout (120s) に依存するが、それも
  発火していない (host load 26-36 での thread 飢餓が疑い)。修復方向:
  (A) hermes-agent 上流で create() に wall-clock deadline
  (B) profile env に HERMES_STREAM_READ_TIMEOUT 明示
  (C) fleet-watchdog に zombie session reaper を追加
  詳細: `itonami/workspace/findings/gateway-stream-zombies-20260905.json`

### cron token 経済監査（2026-09-04 実測済み、測定法）

- **実測法**: 各 profile `state.db` の `sessions` 表（`source='cron'`、`started_at` で
  期間絞り）に token/cost が session 単位で載っている。頻度は `cron/jobs.json`、
  成否は `cron/executions.db`。この 3 つを join すれば tok/run・$/週が機械計算できる。
- 2026-09-04 実測: 全 fleet 3,153 cron sessions / 7日、693.9M tok、$91.83 週。
  悪化トップ: seiri 4.85M tok/run、kuro-maturity 48 runs/日×1.07M tok/run、
  hyakka と hyakka-corpus で同名 job 二重化、itonami-maint 成功率 16%、
  `.bak` profile（codinator.bak-localmain2-*）が有効 job を持つ。
- 実測レポート: `~/.hermes/profiles/itonami/cache/cron-econ-audit-20260904.md`
  （消失したら本 skill の方法で再生成する）。
- 教訓: **fail した cron session も token を消す** — 削減の最優先は頻度ではなく
  低成功率 job の修復。
- **例外 (2026-09-05 実測訂正)**: 「Fire claim lost」型の fail は実行開始前に
  放棄されるので token を焼かない。token を焼くのは完走した session であり、
  頻度×unit cost (tok/run) と「REFUSED 全件で完走する job」の 2 つが本当の
  削減対象。
- **hyakka 系の構造的罠 (2026-09-05 特定)**: app-hyakka repo を 13 profile
  約 30 job が git worktree で共有し、git の worktree lock は親 repo 側
  `.git/worktrees/<name>/index.lock` に集中するため、worktree を分けても
  同一 repo への並行 git は衝突する。hyakka profile は source-scout と
  ontology-scout が両方 `50 * * * *` で確実に衝突する (頻度ずらしが最安の修復)。
- **provider 欠落の修復手順**: config.yaml に `model:` + `providers:` ブロックが
  無い profile は「No LLM provider configured」で全滅する。hyakka-crawl の
  ブロックをコピーして追記し、`hermes cron run <job_id> --profile <p>` で
  1 回手動 fire して succeeded を実測する。
- 提案台帳: `fleet-alloc/workspace/cron-econ-proposals-20260905.json`
  (rank 1-6。rank 3 sec-sentinel 修復は実施済み・検証済み)

### 最適分配 bot（新 profile `fleet-alloc`）の設計

loop: observe（fleet probe + cost EMA + BMC metrics）→ evaluate
（dynamics scoring + VF algorithms で資源→成果物→価値の flow を計算）→
decide（余剰 slot への job 割当 ranked list）→ act（**propose only**。
assign 実行は murakumo / operator）→ record-evidence（append-only ledger）。

データ源: fleet-ci/nodes.edn（probe 再生成）、fleet-ci-cost.edn、
90-docs/business/metrics/*.edn。VF: 生成ジョブを Process、GPU-hour を
Resource、公開物を Output として model。

作り方: murakumo profile の config.yaml をコピーして新 profile `fleet-alloc`
を作り（openrouter-free provider と secrets command を引き継ぐ）、SOUL.md に
上記 loop と propose-only 境界を書く。cron で 1 日 1 回 observe+report。
実装着手時は skill_view で本 skill を読み直すこと。

## 投稿パイプライン（実測済み、2026-09-05）

aozora.app は **PDS 読み取り増幅問題が現在も生きている**（describeRepo /
prepareWrite が 30-60s で timeout、yoro-social-v2 graph も 504）。bot 投稿は
**kotobase.net datom plane + /ipld へ直接書く**（aozora を経由しない）:

1. **Biscuit 取得**: `POST https://auth.kotobase.net/v1/biscuit/token` に
   `Bearer kb_sa_…`（kagi item `aozora-pds-biscuit-sa-token`、1件だけ狙い撃ち）
   + tenant `t_929a4e84e1b24defac1b01ff` + db `fleet-poster`。寿命 15 分。
2. **画像アーカイブ**: `PUT https://kotobase.net/ipld/<cid>`。CID は
   **CIDv1 base32 (b prefix) raw sha2-256** — base58btc (z prefix) は
   "invalid CID" で拒否される。auth は CACAO `kotobase:pin` capability
   （Biscuit は不可）。operator Ed25519 seed は kagi `aozora-d1-operator-secret`
   （導出 did:key が wrangler.jsonc の YORO_OPERATOR_DID と一致することを確認済み）。
   CACAO mint は app-aozora-engine の `:poster` shadow-cljs build
   （pds/src/poster/core.cljs、kotobase.cacao/mint-cacao を利用）。
3. **投稿 datom**: `POST https://kotobase.net/xrpc/ai.gftd.apps.kotobase.datomic.transact`
   に `{db_name, tx_edn}` + `Authorization: Biscuit`。tx_edn は valid EDN 文字列。
   graph=`kotobase/db/<tenant>/<db>`。

PoC 証跡: 画像 PUT 204/GET 200（sha256 一致）、datom transact 200
（commit bafyreidljeef…、4 datoms）、空 graph fleet-poster は 1.1s で応答。
engagement 計測はこの db の投稿 datom に対して行う。

### 2026-09-05 午後: transact が 401 に（Biscuit write 経路は現在不通）→ CACAO 経路で着地済み

Biscuit 経路（`POST auth.kotobase.net/v1/biscuit/token` →
`Authorization: Biscuit`）は **read（datomic.q/datoms）は 200 で通るが、
write（datomic.transact）だけ graph origin で 401** になることを実測
（fresh mint 3 回で再試行、全て 401。`/api/auth/me` は同 token で 200。
write 側 `KOTOBASE_BISCUIT_AUTH_MODE` が origin で未設定=off の挙動と整合）。

**実績のある正経路（200 着地 + datoms read-back 検証済み）は CACAO:**

1. `app-aozora-engine` の `:poster` shadow-cljs build を使用:
   `cd orgs/network-awai/app-aozora-engine && npx shadow-cljs release poster`
   → `poster/out/core.js` が `mint(secretB64, graphCid, capability, aud)` を export
   （JSON 文字列 `{cacao-b64, did, graph}` を返す）。
2. seed は kagi `aozora-d1-operator-secret`（base64 32B）。導出 did:key が
   `pds/wrangler.jsonc` の `YORO_OPERATOR_DID`（z6MkvXVY…CoMWFH）と一致することを確認済み。
3. **capability は `kotobase:pin` を使う**（edge の identity-capability-check が
   `kotoba://can/kotobase:pin` を必須にする。`datom:transact` だと edge で拒否）。
   graph CID は投稿先 graph（fleet-poster = `bafyreiataqxmnfxrxprujhav7qhrxrjxm5k6ra3mv57d6neiooqfjovhr4`）。
4. transact は **`Authorization: CACAO <cacao-b64>` ヘッダ + body に `cacao_b64`** の両方
   （ヘッダで edge viewer 解決、body で engine の tenant-cap write gate を通す）。
   `POST https://kotobase.net/xrpc/ai.gftd.apps.kotobase.datomic.transact`
   `{db_name:"fleet-poster", tx_edn, cacao_b64}`。
5. 実測 200: commit `bafyreido6f2bvncy3wztsvuolzjbsxo5efok7bx3bx2gbtvjq2mltqy23e`、
   4 datoms。read-back は `datomic.datoms`（`{graph, index:"aevt"}`）で内容確認。
   ⚠ `datomic.q` は同じ graph で rows を返さない（空。datoms では見える — q 側の別問題、
   engagement 計測は datoms か pull を使うこと）。
6. ⚠ 書き先 graph は **operator did:key 由来の graph**
   （`bafyreigcepipg…`）になる。tenant の fleet-poster graph（`bafyreiataq…`）ではない。
   engagement 計測もこの operator graph の datoms に対して行う。

## 割当対象 job 種別（登録簿）

| job 種別 | 実装 repo | 割当先の条件 |
|---|---|---|
| DAST scan（passive / active） | `orgs/kotoba-lang/zap-proxy` + **Hermes plugin `hermes-zap-proxy`**。両scan toolは `ZAP_PROXY_APPROVALS_FILE` の有効記録と `approval_id` を必須とし、承認者の権限・対象URL/パス・検査方式・期限を照合。`zap_proxy_targets` / `allow_active` は inventory のみ。質問、受領通知、自動返信、配送通知から許諾を生成しない。scope外URLは送信前に拒否し、HTTP redirectを追跡しない。 |
| 画像生成 | mflux z-image-turbo（judah 16GB 実測済み） | egress ありノードで重み取得済み。生成前 df -h と常駐 process 確認 |
| CI gate | fleet-ci（tick.cljs LPT assign） | 最優先。生成/scan は余剰 slot |

## PoC 経路（実測済みのコマンド）

```bash
# judah に導入済み（2026-09-04）。venv: ~/.gftd/mflux-venv
# 16GB で動く唯一の経路: 事前量子化 4bit ミラー + --low-ram（-q は使わない）
ssh judah 'export HF_HOME=~/.gftd/hf-cache; \
  ~/.gftd/mflux-venv/bin/mflux-generate-z-image-turbo \
  --model filipstrand/Z-Image-Turbo-mflux-4bit \
  --steps 8 --width 1024 --height 1024 --low-ram \
  --prompt "..." --output /tmp/out.png'
# 実測: 21.9s/step、約3分/枚、Peak MLX 5.70GB、exit 0
```
