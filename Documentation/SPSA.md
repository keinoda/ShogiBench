# SPSA チューニング (rshogi ラッパー)

> 実際に回すときの操作手順は **[SPSA_RUNBOOK.md](SPSA_RUNBOOK.md)** (AI向けランブック) を参照。
> この文書は仕組みと設計の説明。

ShogiBench の SPSA ワークロードは、rshogi の `spsa` チューナー
([keinoda/rshogi](https://github.com/keinoda/rshogi) の
`crates/tools/src/bin/spsa.rs`) のラッパーとして動く。
以前の OpenBench 由来の分散 SPSA (サーバがイテレーション状態を持ち、多数の
ワーカーが摂動ペアを分担する方式) は廃止し、**1 台のワーカーが rshogi の spsa を
丸ごと実行して進捗をサーバへ報告する** 方式に置き換えた。

これは keinoda/fuuppi-spsa で実運用したパイプライン
(`run_production.sh` + `live_progress.sh` + `spsa_status.sh`) を
GUI から発行できるようにしたもの。fishtest 互換のスケジュール (alpha / gamma /
A-ratio / C_end / R_end)、バッチ更新、stats.csv / values.csv、resume、
early stop など rshogi 側の機能がそのまま使える。

## 全体の流れ

```
[GUI /tunekits/]                  [GUI /tune/new/]                 [ワーカー (Linux)]
  .tune キット登録                   エンジン/ブランチ/ビルド変種      1. rshogi の spsa を cargo ビルド (初回のみ)
  masterが進んだら照合→自動追随  →   .tuneキット選択 (params自動転記)   2. .tune キットをソースに注入して TUNE ビルド
                                    総ペア数/バッチペア数/seed等  →    3. bench で eval ロードを検証 (NPS も報告)
                                    (作成時にブランチと自動照合)        4. SPSA/<test_id>/ に run dir を用意し spsa を起動
                                                                      5. meta.json / stats.csv / state.params を30秒ごとに報告
[GUI /tune/<id>/]                                                     6. 完走で final.params をアップロード
  進捗・現在値・final.params  ←
```

- **割り当て**: SPSA ワークロードは常に 1 台のワーカーが専有する。他のマシンが
  実行中 (直近3分以内に報告あり) の間は誰にも配られない。Linux ワーカー限定。
- **成績表示**: W/L/D は rshogi の「+側 (プラス摂動側)」視点。raw_result が
  0 近傍を揺れるのが正常 (収束のシグナル)。

## .tune キット (TUNE ビルドとバージョン追随) — /tunekits/

素のやねうら王には探索パラメータを外から変える USI オプションが無いので、SPSA の
前段として **tune.py で `TUNE(...)` マクロを注入した「TUNE ビルド」** が要る
(fuuppi-spsa の `10_patch_build.sh` 相当)。これを GUI で完結させるのが .tune キット:

1. **登録**: `/tunekits/` に `.tune` を貼る (`Scripts/tune/` と同じ書式:
   `#set file` / `#context 名前` / `123@` マーカー / `#add マーカー`)。
   `.params` は省略すれば .tune から自動生成される (初期値=ソースの数値、
   レンジ=0〜2倍、step=可動域の1/20、delta=0.002 — tune.py と同じ既定)。
   既存の .params を貼れば値・レンジが引き継がれる。
2. **SPSA 作成**: `/tune/new/` でキットを選ぶと .params が SPSA 入力へ自動転記される。
   ブランチは通常の `master` でよい — **ワーカーがビルド直前に tune.py でパッチを
   当てて TUNE ビルドを作る** (バイナリはキット内容のハッシュ付きでキャッシュ)。
   パッチ済みビルドでは名前がそのまま USI オプションなので、名前マッピングは「なし」。
3. **バージョンが変わったら (master が進んだら)**: SPSA 作成時に全 context と
   挿入マーカーが対象ブランチと自動照合され、ずれていると作成が止まりキットページへ
   誘導される。キットページで:
   - 「照合」→ 各ブロックを **EXACT / NUMDRIFT / MISSING** に分類して表示
   - 「数値ドリフトを自動追随」→ NUMDRIFT の本文を現行ソースの実テキストに書き換え
     (@ マーカーは同じ序数の数値に付け直し。check_contexts.py / retune.py 相当)
   - MISSING (構造変化) はエディタで該当 `#context` を現行ソースに合わせて手で直す
     (@ の個数と順序 = パラメータ名の対応を維持)
   - 「.params を .tune から再同期」→ 消えたパラメータは `[[NOT USED]]` で残し、
     新しいパラメータを既定レンジで追加。**既存行の現在値は変わらない**ので、
     前回チューニングの到達値から継続できる
4. **検査**: SPSA 作成時、SPSA 入力のパラメータ名とキットの .tune の名前集合が
   突き合わされる (欠けはエラー: サイレントな死にパラメータを防ぐ。外すときは
   `[[NOT USED]]` を付けて残す)。

キットを使った SPSA はキット内容の**スナップショット**を持つ: 後からキットを
編集・追随しても、実行中/再開中のチューニングは作成時点の内容でビルドされ続ける。

キットを使わない場合 (「なし」) は従来どおり: 既に USI オプション化済みのブランチ
(手動 TUNE ビルド) を指すか、rshogi 正準名 + 名前マッピング=YaneuraOu で回す。

## GUI での作り方 (従来スクリプトとの対応)

| 従来のスクリプト/フラグ | GUI の入力 |
|---|---|
| `10_patch_build.sh` (tune.py tune + TUNEビルド) | .tuneキット選択 (ワーカーが自動でパッチ+ビルド) |
| `check_contexts.py` / `retune.py` / `manual_fix.py` | /tunekits/ の「照合」「数値ドリフトを自動追随」+ エディタ |
| `--engine-path <TUNEビルド>` | エンジン + ブランチ + ビルド (キット使用時は master のままで可) |
| `--init-from canonical.params` | SPSA 入力欄に .params をそのまま貼る (`//` コメント・`[[NOT USED]]` 可) |
| `--engine-param-mapping yo_rshogi_mapping.toml` | 名前マッピング = YaneuraOu |
| `--active-only-regex "$(cat active_regex.txt)"` | 対象絞り込み欄 |
| `--total-pairs 51200 --batch-pairs 96` | 総ペア数 / バッチペア数 |
| `--concurrency 192` | 自動 (ワーカーのスレッド数と 2×バッチペア数の小さい方) |
| `--threads 1 --hash-mb 16` | オプション欄の `Threads=1 Hash=16` |
| `--btime 2000 --binc 20` | 持ち時間 `2.0+0.02` (ワーカーの NPS でスケール) |
| `--startpos-file taya36_shogi_sfen.epd` | 開始局面集 |
| `--usi-option EvalDir=...` ほか eval 系 | ネットワーク選択で自動 (aux の eval_options.txt も適用) |
| `--usi-option USI_OwnBook=false` など | オプション欄 (tune プリセットに含まれる) |
| `--seed 1` | 乱数 Seed |
| resume (`--resume --force-unlock`) | 自動 (ワーカー再起動時に続きから) |

持ち時間は `2.0+0.02` (フィッシャー) のほか、`MT=1000` (秒読み ms →
`--byoyomi`)、`N=25000` (ノード固定 → `--nodes`) が使える。

### .params の入力形式

rshogi / tune.py と共通の 7 カラム CSV。fuuppi-spsa の
`build_canonical.py` が出力する canonical をそのまま貼れる:

```
SPSA_LMR_BASE_QUIET, int, 181, 90, 362, 14, 0.0020
SPSA_NMP_MARGIN_OFFSET, int, -390, -780, -195, 30, 0.0020  // sign_flip 済み
old_param, int, 10, 0, 20, 1, 0.0020 [[NOT USED]]
```

- 名前マッピング「なし」: 名前はエンジン (TUNE ビルド) の USI オプション名
- 名前マッピング「YaneuraOu」: 名前は rshogi 正準名 (`SPSA_*`)。setoption の直前に
  rshogi の `tune/yo_rshogi_mapping.toml` で YO 名へ翻訳・符号反転される

**注意 (fuuppi-spsa の知見)**: min/max/C_end/R_end は必ず YO 側 .params 由来の値を
使うこと。`yo_to_rshogi_params` の素の出力 (レンジ ±8192、col7=span/20) を貼ると
更新量が爆発する。`build_canonical.py` を通した canonical を貼るのが正解。

## 進捗ページ

- バッチ / ペア消化率 / 対局数 / 直近バッチの raw_result / avg|update|
- パラメータ表: 初期値 → 現在値、Δ、min/max、**レンジ張り付き警告**
  (現在値が min/max に C_end 以内まで寄ると ⚠ が付く。suisho10 レンジの
  aspiration_window_1 のような「max 拡大の検討対象」を拾うため)
- 「現在値 (.params) をコピー」: 実行中の state.params (7 カラム形式)
- 完走後は「final.params をコピー」: そのまま `tune.py apply` の入力に使える
  (state.params ではなく final.params を使うこと)

## 停止・再開・引き継ぎ

- **停止**: 進捗ページの停止ボタン。次の報告 (30秒以内) でワーカーが spsa を
  SIGTERM → プロセスグループごと終了。状態は `Client/SPSA/<test_id>/run/` に残る
- **再開**: 再開ボタンで再び割り当てられると、同じワーカーなら
  `--resume --force-unlock` で **スケジュール位置 k を保って** 続きから回る
- **マシン引き継ぎ**: 別のワーカーに渡った場合、サーバが保持する最新の
  state.params を起点に **残りペア数を新しいスケジュール地平線として** 回す
  (rshogi の meta.json はマシンを跨げないため、k は引き継がれない。
  現在値からの再スタートとして扱われる)
- ワーカーが突然死しても spsa プロセスは別プロセスグループで生きているので、
  ワーカー再起動時にそのまま再接続して監視を続ける

## ワーカー側の要件

- Linux + cargo (rustup 経由推奨。`Deploy/worker/setup_worker.sh` が入れる)。
  rshogi は `rust-toolchain.toml` でツールチェーンを pin しており、rustup が
  自動で該当バージョンを取得する
- 初回の SPSA 受領時に `Config/config.json` の `rshogi_repo_url` / `rshogi_repo_ref`
  から zip を取得し `cargo build --release -p tools --bin spsa` でビルドする
  (Client 直下に `spsa-ob` としてキャッシュ。ref が変わると再ビルド)
- eval まわりの事故 (LS_BUCKET_MODE 忘れ等) は Network の aux ファイル
  (`eval_options.txt`, `progress.bin`) の仕組みで防ぐ。ファイル位置系オプション
  (EvalDir 等) はワーカーが所有し、テスト側からの上書きは無視される

## 焼き戻し (チューニング後)

final.params をコピーして、fuuppi-spsa の `40_apply_build.sh` 相当を実行する:

```bash
# mapping=YaneuraOu で回した場合は YO 形式へ逆変換してから
rshogi_to_yo_params --rshogi-params final.params \
  --base X.params --mapping tune/yo_rshogi_mapping.toml --output X_tuned.params
# X.params を更新して定数として焼き込み → 通常ビルド → SPRT で検証
python3 tune.py apply X.tune <YOのsource>
```

mapping なし (名前が最初から USI オプション名) の場合は final.params を
そのまま `Scripts/tune/tune.py` の .params として apply できる。
検証 SPRT は通常のテスト作成 (調整前 vs 調整後ブランチ) で行う。
