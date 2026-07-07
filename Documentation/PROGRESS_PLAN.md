# progress対応の現状整理と改善方針 (2026-07)

fuuppi系など「進行度(progress)ファイル+バケット選択」を必要とする評価関数を
ShogiBenchで正しく回すための、実装の経緯・現状・残課題のまとめ。

## 1. これまでに実装したもの(done)

### ネットワークの補助ファイル機構
- ネットワークは**任意個数の補助ファイル**を持てる(`NetworkAuxFile` モデル)。
  ワーカーは全補助ファイルを評価関数と同じ `Networks/<sha>-dir/` に
  **元のファイル名のまま**配置する
- `build.network_aux_options` の名前→オプション対応に載っているファイルは、
  配置先の**絶対パス**が対応するUSIオプションで渡される。
  現在は全YaneuraOu系エンジンで `{"progress.bin": "LS_PROGRESS_COEFF"}` に統一
  (keinoda/YaneuraOu 側もオプション名を `LS_PROGRESS_COEFF` に統一済みのため)
- **`eval_options.txt`**: `Name=Value` 行を bench・対局の両方で必ず setoption。
  `{DIR}` は配置先ディレクトリに展開。EvalDir等ワーカー管理オプションの上書きは拒否
- `{DIR}` は**テスト作成フォームのオプション欄でも**使える(対局のみ適用。
  benchには適用されない点が eval_options.txt との違い)
- 補助ファイルは登録後でもネットワークのEDITページから**追加・削除**できる
  (再アップロード不要)

### この過程で潰した実バグ
- **相対パスの解決基準問題**: 新しめのYaneuraOuは相対パスを実行ファイル基準で
  解決するため、benchだけ FileNotFound になっていた → 全パスを絶対パス化
- **評価関数ロードのファイルロック渋滞**: 384並列benchがinodeロックで直列化し
  タイムアウト、殺し残しプロセスがロックを塞いで全benchが永久ハング
  → 並列bench上限32 + TERM→KILL二段掃除
- **フォークごとのオプション名差** (`ProgressFilePath` vs `LS_PROGRESS_COEFF`)
  → エンジン設定のマッピングで吸収、現在は統一
- Bench欄は空欄=照合なしに(bench値はビルド+eval+オプションの組で変わるため)

### 診断ツール
- `Scripts/progress_check.sh`: インスタンス上で 配置→オプション存在→
  実パス/偽パス読み込み→固定ノードbench比較 を自動判定
- ワーカーはbenchに渡した全オプションをログに出す(`Bench option for ...`)

## 2. まだうまくいっていないこと(ユーザー報告)

- **progress付きエンジンがShogiBench経由の対局で本来の強さにならない**。
  「全敗ではないが恐ろしく弱い」= 水匠11事件(バケット選択の指定漏れで数百Elo損)
  と同型の症状
- fuuppi-v3 ロード時に **NNUEアーキテクチャ警告**が残っている:
  ファイル側 `HalfKaHmMerged 768x2 / fv_scale=28` に対し、ビルド側の期待が
  `SFNN_HALFKAHM2_768_7_64_K3K3{LayerStack=9}` 等で不一致のまま
  "continuing anyway" で走っている。**この警告が出ている限り正しさは保証されない**
- 上記により、ShogiBench経由のprogress系テストは判定保留のまま中断。
  SPSA等の作業はインスタンス直実行に切り替えて先行中

## 3. 改善方針(plan)

### Step 1: 統一オプション名での再検証 ← 今回のデプロイ後、最初にやる
1. `git pull origin shogi && fly deploy`(LS_PROGRESS_COEFF統一を反映。
   サーバー設定変更のみなのでワーカーは自動で追従)
2. インスタンスで `Scripts/progress_check.sh <対象バイナリ>` を実行。
   期待: 段階2で `LS_PROGRESS_COEFF` が検出され、段階4(偽パスでエラー)と
   段階5(bench値が変化)がOKになること
3. OKなら fuuppi ネットを `nn.bin + progress.bin + eval_options.txt
   (LS_BUCKET_MODE=progress8kpabs)` で登録し直し、小規模テスト(25kノード×数百局)
   で「恐ろしく弱い」が解消したか確認

### Step 2: アーキテクチャ警告の根本解決
1. fuuppi-v3 が想定する **正確なYANEURAOU_EDITION/アーキテクチャ文字列**を確定する
   (評価関数の作者情報 or nnue_arch_gen.py の生成候補と `arch_in_file` の突き合わせ)
2. 一致するビルドで `NNUE hash mismatch` 警告が**消える**ことを確認し、
   そのビルド引数を /builds/ のバリアントとして登録
3. 検証: 同一ネット・新旧ビルドの固定ノード対戦(速度差の影響なし)で
   新ビルドが有意に勝ち越すこと

### Step 3: 誤設定の構造的防止
1. 警告一致確認の定型化: `arch_in_file` と `arch_expected` の不一致を
   ワーカーが検出して警告できる仕組み。既に `build.bench_fatal_patterns`
   (bench出力の特定文言を失敗扱いにする)が実装済みなので、
   Step 2 完了後に `"NNUE hash mismatch"` を登録するか判断する
   (現状は正当な組でも警告が出るため有効化していない)
2. bench にもテスト欄のオプションを反映するかの検討
   (現状: eval_options.txt はbench+対局、テスト欄は対局のみ。
   混乱の元なら bench にも適用する変更を入れる)
3. DEPLOYMENT.md の「評価関数登録チェックリスト」化
   (必須オプション→eval_options.txt、progress.bin の名前厳守、警告の読み方)

### Step 4: 中断中の関連作業の再開判断
- progress実機ロードの完全検証(当初「後回し」としていたもの)は Step 1-2 に吸収
- SPSA(インスタンス直実行・rshogi)で得た調整値の最終検証はShogiBenchのSPRTで行う
  → その時点で Step 1-3 が完了していることが前提条件

## 4. 参照
- 診断: `Scripts/progress_check.sh` / 過去の切り分けログはこのファイルの経緯参照
- 補助ファイル仕様: DEPLOYMENT.md 6章「YaneuraOu対応」4項
- SPSA: `Scripts/spsa_standalone.py`(小規模)、rshogi(本番)、`Scripts/tune/`(ソースパッチ)
