# SPSA チューニング (rshogi ラッパー)

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
[GUI /tune/new/]                 [ワーカー (Linux)]
  エンジン/ブランチ/ビルド変種      1. rshogi の spsa を cargo ビルド (初回のみ、キャッシュされる)
  ネットワーク (eval + aux)        2. エンジンをビルドし bench で eval ロードを検証 (NPS も報告)
  .params テキスト                 3. SPSA/<test_id>/canonical.params を書き run dir を用意
  総ペア数/バッチペア数/seed等  →   4. spsa を起動 (別プロセスグループ、run.log へ出力)
                                   5. meta.json / stats.csv / state.params を30秒ごとに読んで報告
[GUI /tune/<id>/]                 6. 完走で final.params をアップロード
  進捗・現在値・final.params  ←
```

- **割り当て**: SPSA ワークロードは常に 1 台のワーカーが専有する。他のマシンが
  実行中 (直近3分以内に報告あり) の間は誰にも配られない。Linux ワーカー限定。
- **成績表示**: W/L/D は rshogi の「+側 (プラス摂動側)」視点。raw_result が
  0 近傍を揺れるのが正常 (収束のシグナル)。

## GUI での作り方 (従来スクリプトとの対応)

| run_production.sh のフラグ | GUI の入力 |
|---|---|
| `--engine-path <TUNEビルド>` | エンジン + ブランチ + ビルド (TUNE ビルドの変種を /builds/ で登録して選ぶ) |
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
