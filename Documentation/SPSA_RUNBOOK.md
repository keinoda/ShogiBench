# SPSA 実施手順書 (AI 向けランブック)

ShogiBench で実際に SPSA チューニングを回すときの手順書。AI エージェント
(Claude 等) がこの文書だけを頼りに、準備 → スモークテスト → 本番 → 焼き戻し →
検証まで完走できるように書いてある。仕組みの背景説明は
[SPSA.md](SPSA.md) を参照 (この文書は「操作と判定」だけに絞る)。

## この手順書の使い方 (AI 向けの約束事)

- 各ステップは **[操作] → [期待される結果] → [NGなら]** の形で書く。
  期待される結果を確認してから次へ進むこと。確認せずに先へ進まない。
- `<SERVER>` = ShogiBench サーバーの URL (例 `https://shogibench.fly.dev`)。
  `<WORKER>` = ワーカーインスタンス (SSH で入れる Linux マシン)。
- GUI 操作は Web ページの URL とフォーム欄名で指定する。CLI が使えるなら
  `curl` での代替も併記する。
- **初回や構成変更後は、必ず手順4のスモークテストを先に行う**。いきなり
  10万局の本番を作らない。
- 困ったら最後の「トラブルシューティング」表を引く。表にない事象は、
  ワーカーの `~/shogibench-worker.log` と `Client/SPSA/<test_id>/run/run.log`
  を読んでから判断する。

---

## 手順0: 前提条件の確認

以下がそろっていることを確認する。

| # | 前提 | 確認方法 |
|---|---|---|
| 0-1 | サーバーが動いている | `<SERVER>/index/` が 200 を返す |
| 0-2 | approver 権限のアカウント | `<SERVER>/login/` でログインできる。承認ボタンが出る |
| 0-3 | Linux ワーカーが接続済み | `<SERVER>/machines/` に稼働マシンが出る (SPSA は Linux ワーカー限定・1台が専有) |
| 0-4 | ワーカーに cargo がある | `setup_worker.sh` 経由なら自動で入っている。SSH で `cargo --version` |
| 0-5 | 評価関数 (Network) 登録済み | `<SERVER>/networks/` に対象 eval がある (無ければ手順2) |
| 0-6 | .tune キット登録済み | `<SERVER>/tunekits/` に対象キットがある (無ければ手順1) |

メモ: SPSA ワークロードはサーバー側の判定で **Linux ワーカー1台だけ** に
割り当てられる。ワーカーのスレッド数が並列対局数の上限になるので、
本番は大きいマシン (数十〜百数十スレッド) を使う。

---

## 手順1: .tune キットの準備 (パラメータの USI オプション化の定義)

素のやねうら王には探索パラメータを外から変える USI オプションが無い。
`.tune` キットが「どの定数をオプション化するか」の定義で、ワーカーが
ビルド直前にソースへ注入する (TUNE ビルド)。

### 1-1. キットが無い場合: 新規登録

- [操作] `<SERVER>/tunekits/` を開き、エンジン (例 `YaneuraOu-nagisa`)、
  キット名 (例 `fuuppi`)、`.tune` の内容を貼って「キットを作成」。
  手持ちの `.params` があれば一緒に貼る (値・レンジが引き継がれる)。
  無ければ空欄でよい (自動生成される)。
- [期待される結果] キット詳細ページに遷移し、「パラメータ N 個」が
  意図した数になっている。`.params` 欄が7カラム形式で埋まっている。
- [NGなら] 「#set file がありません」「@マーカー付きのパラメータがありません」
  → `.tune` の書式を確認 (`Scripts/tune/` の tune.py と同じ書式。
  `#set file <path>` / `#context 名前` / 数値に `123@` マーカー)。

### 1-2. 対象ブランチとの照合 (毎回、SPSA 作成の前に行う)

- [操作] キット詳細ページ (`<SERVER>/tunekits/<id>/`) の「ソースとの照合」で、
  ソース (例 `https://github.com/keinoda/YaneuraOu`) とブランチ (例 `master`)
  を入れて「照合」。
- [期待される結果] `EXACT N / NUMDRIFT 0 / MISSING 0` と
  「✅ 全ブロックが一致」が出る。
- [NGなら] 手順1-3へ。

### 1-3. バージョンが変わってずれた場合の追随

masterが進むと定数や式が変わり、NUMDRIFT / MISSING が出る。対処:

1. [操作] 「数値ドリフトを自動追随」を押す。
   [期待される結果] 「自動追随: N ブロックを書き換え」と出て、NUMDRIFT が
   0 になる (再照合で確認)。
2. MISSING が残る場合 (構造変化): 照合結果テーブルで該当 context 名と
   理由を確認し、現行ソース (GitHub で該当ファイルを開く) を見て、
   ページ下部のエディタで該当 `#context` ブロックの本文を現行ソースの
   実テキストに書き直して「保存」。
   **`@` マーカーの個数と順序 = パラメータ名の対応なので、必ず維持する。**
   パラメータ自体が消えた場合はブロックごと削除してよい (次の同期で
   .params 側は [[NOT USED]] になる)。
3. [操作] 再照合して全 EXACT を確認 → 「.params を .tune から再同期」。
   [期待される結果] 「同期しました (追加 X / 引退 Y / 維持 Z)」。
   既存パラメータの現在値は変わらない (前回到達値から継続できる)。

---

## 手順2: 評価関数 (Network) の登録

対象 eval が `<SERVER>/networks/` に無い場合のみ。

- [操作] `<SERVER>/newNetwork/` から nn.bin をアップロード。**補助ファイルに
  `eval_options.txt` と (あれば) `progress.bin` を必ず付ける**。
  CLI の場合:
  ```sh
  curl -sS -X POST <SERVER>/scripts/ \
    -F action=UPLOAD_NETWORK -F engine=YaneuraOu-nagisa -F name=<eval名> \
    -F username=<user> -F password=<アカウントパスワード> \
    -F netfile=@nn.bin \
    -F auxfiles=@eval_options.txt -F auxfiles=@progress.bin
  ```
- `eval_options.txt` の書き方 (1行1オプション、値に空白不可):
  ```
  FV_SCALE=24
  LS_BUCKET_MODE=progress8kpabs
  LS_PROGRESS_COEFF={DIR}/progress.bin
  ```
  `{DIR}` はワーカー上の配置先に展開される。**LS_BUCKET_MODE の指定漏れは
  数百 Elo 弱くなる事故につながる** (eval_options.txt に書いておけば
  bench と対局の両方に必ず適用され、忘れようがない)。
- [期待される結果] `<SERVER>/networks/YaneuraOu-nagisa/` に載り、EDIT ページで
  補助ファイル2つが見える。

---

## 手順3: ビルド変種の確認

- [操作] `<SERVER>/builds/` で、対象評価関数に合う EDITION のビルド変種が
  あるか確認。無ければビルドコマンドを貼って登録
  (例: `make -j tournament COMPILER=clang++ TARGET_CPU=AVX2 YANEURAOU_EDITION=<EDITION>`)。
- 高速化フラグ `EXTRA_CPPFLAGS='-DHASH_KEY_BITS=128 -DTT_CLUSTER_SIZE=4 -DUSE_LAZY_EVALUATE'`
  は SPRT 実測で有意にプラスだった実績があるが、**SPSA 中と検証 SPRT で
  同じフラグを使う**こと (途中で変えない)。
- [期待される結果] SPSA 作成フォームの「ビルド」ドロップダウンに変種が出る。

---

## 手順4: スモークテスト (初回・構成変更後は必須)

本番の前に、小さな SPSA を1本流してパイプライン全体
(TUNE ビルド → rshogi ビルド → 対局 → 進捗報告 → 完走 → final.params) を検証する。

### 4-1. 作成

- [操作] `<SERVER>/tune/new/` を開き:

| 欄 | スモーク値 | 備考 |
|---|---|---|
| エンジン | `YaneuraOu-nagisa` 等 | キットと同じエンジンにする |
| ブランチ | `master` | TUNE 化前の普通のブランチでよい (ワーカーがパッチする) |
| Bench | 空欄 | 空欄=照合なし。TUNE ビルドは bench 値が変わるため空欄が正しい |
| ネットワーク | 対象 eval | |
| ビルド | 対象 EDITION の変種 | |
| .tuneキット | 対象キット | 選ぶと SPSA 入力に .params が自動転記される |
| オプション | プリセットのまま | `Threads=1 Hash=16 USI_OwnBook=false NetworkDelay=0 ...` |
| 持ち時間 | `N=10000` | スモークはノード固定で高速・決定的に |
| 総ペア数 | `64` | 128局。数分で終わる規模 |
| バッチペア数 | `16` | |
| Seed | `1` | |
| 対象絞り込み | 空欄 | |
| 名前マッピング | なし | キット使用時は「なし」必須 (自動設定される) |

  「SPSAチューニングを作成」を押す。
- [期待される結果] `/index/` に遷移してチューニングが一覧に出る。
- [NGなら]
  - 「.tuneキット "..." が ... からずれています」→ 手順1-3で追随してから再作成。
  - 「SPSA入力に .tuneキットのパラメータがありません」→ SPSA 入力を編集した
    場合に起きる。キットを選び直して自動転記からやり直すか、外したい行に
    `[[NOT USED]]` を付ける。
  - 「範囲 [...] がキットの範囲 [...] を超えています」→ レンジを広げたい場合は
    先にキットの .params を編集する (TUNE ビルドの USI レンジはキット由来)。

### 4-2. 承認と割当

- [操作] 作成したチューニングのページで「承認」(approver 権限)。
- [期待される結果] 30〜60秒以内にワーカーが取り、ページの進捗ブロックに
  「実行マシン: #N」が出る。初回は次の準備で数分〜十数分かかる:
  1. TUNE ビルド (tune.py パッチ + make。ワーカーログに
     `Applying .tune kit "..."` → `Building [...]`)
  2. bench (eval ロード検証 + NPS 報告。`/machines/` に MNPS が出る)
  3. rshogi spsa の cargo ビルド (**初回のみ**。`Building rshogi spsa from ...`)
- [NGなら] `<SERVER>/errors/` を確認:
  - 「TUNE patch ... の適用に失敗しました」+ ログに `replaced count = 0`
    → キットとブランチがずれている。手順1-2の照合をやり直す
    (作成時照合をすり抜けるのは、作成後にブランチ側が動いた場合)。
  - ビルドエラー → ビルド変種の EDITION と eval のアーキ不一致が典型。
  - ワーカーが取らない → ワーカーが Linux か、`/machines/` で生きているか、
    別マシンが同じ SPSA を実行中でないか確認。

### 4-3. 進捗と完走の確認

- [操作] チューニングページを数分おきに確認 (30秒ごとに自動報告される)。
- [期待される結果]
  - 進捗ブロック: `batch X/4 | ペア n/64 (%)` が進む。
  - `raw_result` が ±batch_pairs の範囲で 0 近傍を揺れる。
  - パラメータ表の Curr が Start から動き始める。
  - 64ペア完走で「✅ 完走済み」+ passed (青/緑) になり、
    「final.params をコピー」ボタンが出る。
- [操作] final.params をコピーし、7カラム形式で全パラメータが
  入っていることを確認する。
- ここまで通れば本番に進んでよい。

### 4-4. (任意) 停止・再開の動作確認

- [操作] 実行中に「停止」→ 30秒以内にワーカーが止まる (進捗が止まる) →
  「再開」→ 同じワーカーが `--resume` で続きから走る (ペア数が
  巻き戻らずに増えることを確認)。

---

## 手順5: 本番 SPSA

### 5-1. 設定の決め方

| 欄 | 決め方 (実績値) |
|---|---|
| 持ち時間 | `2.0+0.02` (tc=2+0.02、fuuppi 実績)。ワーカー NPS でスケールされる |
| 総ペア数 | パラメータ数 × 50〜500。約100個なら 25600〜51200 (51200=102,400局で×500フル) |
| バッチペア数 | **ワーカーのスレッド数 ÷ 2 以上** (並列対局数の上限が 2×バッチペア数のため)。192スレッドなら 96 |
| オプション | プリセットのまま (`Threads=1 Hash=16 USI_OwnBook=false NetworkDelay=0 NetworkDelay2=0 MinimumThinkingTime=100 RoundUpToFullSecond=false`) |
| 開始局面集 | `taya36_shogi_sfen.epd` (多様で偏りのない互角局面集を使う) |
| Seed | 固定値 (例 `1`)。同じ seed なら θ 軌跡を再現できる |
| 対象絞り込み | 全パラメータなら空欄。グループごとに回すなら正規表現 (例 `^(SPSA_LMR\|SPSA_NMP)_`) |
| 早期停止 | **初回は使わない** (閾値の運用実績を作ってから) |

エンジンプリセットボタン「本番 tc=2+0.02 (fuuppi実績)」がこの構成を一括入力する。

所要時間の目安: 192スレッド・tc=2+0.02 で 102,400局 ≈ 1〜2日。

### 5-2. 作成・承認

手順4-1, 4-2 と同じ。スモークと違い、値は 5-1 の本番値。

### 5-3. 監視 (1日数回、またはバッチ数十個ごと)

チューニングページで以下を確認する。判定基準:

| 見る場所 | 正常 | 異常のサイン |
|---|---|---|
| 進捗ブロック batch | 単調に進む | 長時間止まっている → ワーカー側を調査 |
| raw_result | 0近傍を±で揺れる | 常に大きく片側に偏る → TC が短すぎる/エンジン不安定 |
| avg\|update\| | 徐々に小さくなる傾向 | 桁違いに大きい → C_end/R_end の設定ミス (作り直し) |
| パラメータ表 ⚠ | 無し〜少数 | ⚠ min/max付近 が多発 → そのパラメータはレンジが最適を切っている。完走後にキットの .params でレンジを広げて次回に反映 |
| 実行マシン/最終報告 | 数十秒以内の時刻 | 数分以上古い → ワーカー切断。自動で復帰するが、`/machines/` と ワーカーログを確認 |

ワーカー側を直接見る場合 (SSH):
```sh
tail -f ~/shogibench-worker/Client/SPSA/<test_id>/run/run.log   # rshogi の生ログ
cat  ~/shogibench-worker/Client/SPSA/<test_id>/run/meta.json    # 進捗メタ
```

### 5-4. 停止・再開・引き継ぎの性質 (知っておくこと)

- 「停止」→ 状態はワーカーの `Client/SPSA/<test_id>/run/` とサーバー双方に
  残る。「再開」で同じワーカーなら **スケジュール位置 k を保って** 続きから。
- ワーカープロセスが死んでも spsa は別プロセスグループで走り続け、
  ワーカー再起動時に自動で再接続する。
- 別のワーカーに引き継がれた場合は、サーバー保持の最新値から
  **残りペア数で新しいスケジュール**として再開する (kは引き継がれない。
  SPSA としては「現在値からの再スタート」で、実用上問題ない)。
- 総ペア数は途中で変更できない。延長したい場合は完走後、final.params を
  新しいチューニングの SPSA 入力に貼って続きを回す (値が引き継がれる)。

---

## 手順6: 完了後の焼き戻しと検証

### 6-1. final.params の取得

- [操作] 完走した (✅ passed) チューニングページで「final.params をコピー」。
- **state.params (実行中の現在値) ではなく、必ず final.params を使う。**

### 6-2. 定数として焼き込み (tune.py apply)

作業マシン (やねうら王のソースがある環境) で:

```sh
cd <YaneuraOuリポジトリ>
git checkout master && git checkout -B tuned-<日付>

# キットの .tune と、final.params を <キット名>.params として並べる
# (ファイル名は .tune と同じベース名 + .params にすること)
cp <キットの.tune> kit.tune
# final.params の内容を kit.params に保存

python3 <ShogiBench>/Scripts/tune/tune.py apply kit.tune source
# 期待: "All patches have been applied successfully."
# NG (replaced count エラー): .tune がソースとずれている → キットページで追随した
#    最新の .tune を使う

make -C source clean
make -C source -j tournament COMPILER=clang++ TARGET_CPU=<CPU> YANEURAOU_EDITION=<EDITION>
```

検証 (焼き込みビルドの健全性):
```sh
(echo usi; sleep 2; echo quit) | ./source/YaneuraOu-by-gcc | grep -c "option name <調整したパラメータ名>"
# 期待: 0 (TUNEオプションが出ない = 定数として焼き込み済み)
```

ブランチを push しておく (次の SPRT で使う)。

### 6-3. SPRT で調整前後を検証

- [操作] `<SERVER>/test/new/` で通常のテストを作成:
  - Dev = 焼き込みブランチ (`tuned-<日付>`)、Base = 調整前 (`master`)
  - 同じネットワーク・同じビルド変種・`10.0+0.1` など本番想定の TC
  - SPRT 境界 `[0.00, 4.00]` (デフォルト)
- [期待される結果] passed (H1 採択)。fuuppi 実績: 102,400局チューニングで
  SPRT (10+1) +34 Elo。
- failed した場合: チューニングが効かなかった。総ペア数不足、TC が本番と
  乖離しすぎ、レンジ張り付き多発などを疑い、条件を変えて再チューニング。

### 6-4. 後始末

- キットの `.params` を final.params の値で更新しておく
  (キットページで .params 欄に貼って保存)。次回チューニングが
  今回の到達値から継続できる。
- 使い終わったワーカー (vast.ai) はインスタンスごと破棄してよい
  (`Client/SPSA/` の状態は完走後は不要。サーバー側に final.params が残る)。

---

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| 作成時「.tuneキットがずれています」 | ブランチが進んだ。手順1-3 (照合→自動追随→MISSING手当て→再同期) |
| 作成時「マーカーがソースにありません」 | `%%TUNE_DECLARATION%%` 等の無いブランチ (素の上流など) を指している。keinoda フォークの対応ブランチを使う |
| ワーカーが取らない | ① Linux ワーカーか ② `/machines/` で生存しているか ③ 別マシンが同じ SPSA 実行中でないか (1台専有) ④ 承認済みか |
| エラー「TUNE patch ... 適用に失敗」 | ログの `replaced count` を確認。作成後にブランチが動いた場合に起きる。キット再照合 → チューニング作り直し |
| エラー「rshogi spsa exited unexpectedly」 | `run.log` 末尾がエラーログとして `/errors/` に送られている。eval ロード失敗 (アーキ不一致) や OOM (Hash×並列超過) が典型。該当ワーカーはそのセッション中は再割当を受けない → 原因を直してワーカー再起動 or 再開 |
| 進捗が止まったまま | ワーカー SSH → `pgrep -f 'spsa-ob --run-dir'` で生死確認。生きていればワーカー再起動で再接続、死んでいれば「再開」で resume |
| raw_result が常に偏る | TC 短すぎ/NetworkDelay 未設定を疑う。オプションのプリセット (MinimumThinkingTime=100 等) を確認 |
| ⚠ max付近/min付近 が多い | レンジが最適を切っている。完走後、キットの .params でその行の min/max を広げ、次回チューニングに反映 (今回のランは完走させてよい) |
| bench 照合エラー | TUNE ビルドは bench 値が変わる。Bench 欄は空欄にする |
| final.params を apply すると replaced count エラー | apply には「照合済みの最新 .tune」を使う。SPSA を回した時点のキット内容はチューニングページのキット行 (スナップショット sha) で確認できる |

## 参考

- 仕組みと設計: [SPSA.md](SPSA.md)
- 実運用の知見の原典: keinoda/fuuppi-spsa の
  `docs/rshogi-spsa-shogibench-integration.md` (2026-07 の fuuppi-v3 /
  Suisho11 / danbo-v11 チューニングで踏んだ罠を反映)
- rshogi 本体の正典: keinoda/rshogi `crates/tools/docs/spsa_runbook.md`
