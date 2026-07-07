# ShogiBench デプロイガイド

ShogiBench は「コーディネーションサーバー」(この Django アプリ)と「ワーカー」
(対局を実際に実行するマシン)の2層構成です。

```
[ブラウザ]  ──閲覧(公開)/テスト作成(ログイン)──▶  [コーディネーションサーバー]
                                                    Fly.io / Render / VPS など安価な常設サーバー
                                                    UI + API + SQLite + SPRT/SPSA 集計
                                                          ▲
                                                          │ HTTPS (ワーカーキーで認証)
                                                          │ ワークロード取得・結果送信
                                                    [ワーカー]
                                                    vast.ai などの高性能インスタンス
                                                    エンジンをビルドして対局を実行
```

- **閲覧は公開**: テスト結果・進行状況は誰でも見られます
- **実行はログイン必須**: テスト作成・SPSA・ネットワーク管理はログインが必要です
- **登録は招待制**: Web からの新規登録は無効化されており、管理者が
  `manage.py invite` でアカウントを発行します
- **ワーカーは専用キーで接続**: アカウントのパスワードを vast.ai インスタンスに
  置く必要はありません。`/workers/` で発行したトークンを使います

---

## 1. サーバーのデプロイ

### 共通の環境変数

| 変数 | 必須 | 説明 |
|---|---|---|
| `OPENBENCH_SECRET_KEY` | ✅ | Django の秘密鍵。設定すると自動的に `DEBUG=False` になる |
| `OPENBENCH_DATA_DIR` | 推奨 | SQLite と Media の置き場所。永続ボリュームを指すこと (例 `/data`) |
| `OPENBENCH_ALLOWED_HOSTS` | 推奨 | 公開ホスト名 (カンマ区切り)。例 `shogibench.fly.dev` |
| `OPENBENCH_CSRF_TRUSTED_ORIGINS` | 推奨 | `https://` 付きの公開オリジン。ログインフォームの CSRF に必要 |
| `OPENBENCH_DEBUG` | 任意 | 明示的に上書きしたい場合のみ (`1`/`0`) |
| `WEB_CONCURRENCY` | 任意 | gunicorn ワーカー数 (既定 2) |

秘密鍵の生成例:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
```

> **注意**: DB は SQLite なので、サーバーは常に **1 インスタンス** で運用して
> ください(水平スケール不可)。この用途では十分な性能があります。

### 1-a. Fly.io (推奨)

リポジトリ直下の `fly.toml` を使います。

```sh
fly launch --no-deploy      # アプリ名を決める (fly.toml の app / ホスト名も合わせて変更)
fly volumes create shogibench_data --size 3
fly secrets set OPENBENCH_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
fly deploy
fly scale count 1           # SQLite のため必ず 1 台
```

初期ユーザーの発行:

```sh
fly ssh console -C 'python /app/manage.py invite <ユーザー名> --approver'
```

`fly.toml` は idle 時にマシンを停止する設定 (`min_machines_running = 0`) に
なっています。ワーカーが動いている間はポーリングで起き続けます。UI の
コールドスタートが気になる場合は `1` にしてください(常時起動でも月数ドル程度)。

### 1-b. Render

`render.yaml` (Blueprint) を使います。ダッシュボードから "New +" → "Blueprint"
でこのリポジトリを指定してください。

- 永続ディスクが必要なため **Starter プラン以上** が必要です
  (Free プランはファイルシステムが揮発性で、再起動のたびに DB が消えます)
- デプロイ後、Render の Shell タブで `python manage.py invite <ユーザー名> --approver`

### 1-c. VPS / 自宅サーバー (Docker Compose)

```sh
export OPENBENCH_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
export OPENBENCH_ALLOWED_HOSTS=bench.example.com
export OPENBENCH_CSRF_TRUSTED_ORIGINS=https://bench.example.com
docker compose up -d --build
docker compose exec web python manage.py invite <ユーザー名> --approver
```

HTTPS 終端は Caddy や nginx などのリバースプロキシを前段に置いてください
(`X-Forwarded-Proto` を付与すること)。

---

## 2. ユーザー管理(招待制)

Web からの新規登録は `Config/config.json` の
`"require_manual_registration": true` により無効化されています。

アカウント発行はサーバー上で:

```sh
python manage.py invite <ユーザー名> [--email <メール>] [--password <初期パスワード>] [--approver]
```

- パスワード省略時は対話的に入力を求められます(非対話実行時のみランダム生成して一度だけ表示)
- 忘れた場合は `python manage.py changepassword <ユーザー名>` でリセットできます
- `--approver` を付けるとテストの承認・ネットワーク管理が可能になります
- ユーザーは初回ログイン後に `/profile/` でパスワードを変更できます
- 無効化したい場合は Django admin (`/admin/`) で Profile の `enabled` を外します
  (admin へは `python manage.py createsuperuser` で作った管理者で入れます)

---

## 3. ワーカー (vast.ai / AWS) の接続

### 3-1. ワーカーキーの発行

1. サーバーにログインし、サイドバーの **Worker Keys** (`/workers/`) を開く
2. キー名 (例 `vastai-epyc`) を付けて **Create Worker Key**
3. 表示されたトークンをコピー(ページに接続用スニペットも表示されます)

トークンは「アカウントのパスワードの代わり」に使う接続専用の文字列です。
SSH 鍵のようにインスタンスへ登録する必要はなく、ワーカーのクライアントを
起動するときに環境変数 `OPENBENCH_PASSWORD` として渡すだけで機能します:

- Web サイトへのログインには使えません(ワーカー用 API 専用)
- 漏洩したら `/workers/` で Delete / Disable するだけで無効化できます
- インスタンスごと・テンプレートごとにキーを分けると管理が楽です

### 3-2. UI からの SSH ワンクリック接続(最も簡単)

`/workers/` ページの **SSHでインスタンスを接続** に、接続先の SSH アドレスを
貼り付けて接続ボタンを押すと、
サーバーがインスタンスに SSH で入り、依存パッケージのインストールから
ワーカーの起動までを自動で行います。ワーカーキーは未選択でも自動で
選択・作成されます。

- vast.ai: Connect ボタンが表示する `ssh -p 12345 root@ssh4.vast.ai`
- AWS: 既存の deploy 鍵で入れるユーザーを明示して `ec2-user@203.0.113.7`
  または `ubuntu@203.0.113.7`

前提はひとつだけ: **サーバーに秘密鍵を設定しておくこと**。普段
AWS や vast.ai に SSH 接続している鍵と同じ秘密鍵を、一度だけ Fly.io の
シークレットとして設定します:

```sh
fly secrets set OPENBENCH_SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ed25519)"
```

- AWS / vast.ai 側に対応する公開鍵が既に入っているなら、追加の秘密鍵指定は
  不要です。以後は SSH アドレスを貼り付けるだけで接続できます
- ed25519 / ECDSA / RSA に対応。**パスフレーズなしの鍵**が必要です。
- `fly secrets set` は自動で再デプロイをトリガーします。設定済みか
  どうかは `/workers/` ページに鍵のフィンガープリントとして表示されます
- 環境変数の代わりに、ボリューム上の `/data/ssh_key` にファイルとして
  置くこともできます(`OPENBENCH_SSH_PRIVATE_KEY_FILE` で場所を変更可)

進行状況はインスタンス側の `~/shogibench-worker.log` に記録され、
成功すれば数十秒〜数分で `/machines/` にマシンが現れます。

### 3-3. vast.ai テンプレートの設定(全自動にしたい場合)

普段使っているテンプレートに次を追加します。

**Environment Variables:**

```
OPENBENCH_SERVER=https://<あなたのサーバー>/
OPENBENCH_USERNAME=<ユーザー名>
OPENBENCH_PASSWORD=<ワーカーキーのトークン>
```

**On-start Script:**

```sh
curl -sSL https://raw.githubusercontent.com/keinoda/ShogiBench/shogi/Deploy/worker/setup_worker.sh -o /root/setup_worker.sh
chmod +x /root/setup_worker.sh
nohup /root/setup_worker.sh > /root/shogibench-worker.log 2>&1 &
```

(`Deploy/worker/onstart.sh` と同じ内容です。ブランチ構成を変えた場合は URL の
`shogi` 部分を合わせてください)

イメージは Ubuntu 系なら何でも動きます (`ubuntu:24.04`、vast.ai の標準イメージ等)。
必要なパッケージ (git / clang / make / python3) はスクリプトが自動で入れます。
毎回の apt install を省きたい場合は `Deploy/worker/Dockerfile` をビルドして
Docker Hub に push し、それをテンプレートのイメージに指定してください。

### 3-4. 起動済みインスタンスに手動で追加する場合

SSH して以下を実行するだけです (`/workers/` ページのスニペットをコピペでも可):

```sh
export OPENBENCH_SERVER=https://<あなたのサーバー>/
export OPENBENCH_USERNAME=<ユーザー名>
export OPENBENCH_PASSWORD=<ワーカーキーのトークン>
curl -sSL https://raw.githubusercontent.com/keinoda/ShogiBench/shogi/Deploy/worker/setup_worker.sh | bash
```

チューニング用の環境変数:

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SHOGIBENCH_THREADS` | 全コア | ワーカーが使うスレッド数 |
| `SHOGIBENCH_SOCKETS` | 1 | CPU ソケット数 |
| `SHOGIBENCH_REPO_URL` | このリポジトリ | クライアント取得元 |
| `SHOGIBENCH_REPO_REF` | `shogi` | 取得するブランチ |

### 3-5. マシンの一時停止(計算資源を返したいとき)

`/workers/` の「稼働中のマシン」で **停止** を押すだけです。ワーカーは
約30秒ごとに必ず通信してくるので、次の通信で実行中の対局が中断され、
以後そのマシンには仕事が配られなくなります(SSHは不要)。

- 未完の対局は他のマシンに配り直されるため、テストは壊れません
- **再開**も同じ場所のボタンから(インスタンス側の操作は不要)
- ワーカーのプロセスが再起動して新しいセッションになると停止要求は
  引き継がれないので、**長期間止める場合はワーカーキーの無効化**も
  あわせて行ってください(キーを再度有効化すれば自動で仕事を取り始めます)
- インスタンス上で手動で完全停止したい場合:
  `touch ~/shogibench-worker/Client/openbench.exit` のあと
  `pkill -f shogibench_setup; pkill -f 'client.py'`。
  エンジンの殺し残し掃除が必要なときは、**他の用途のエンジンを巻き込まない**よう
  作業ディレクトリで絞り込むこと:
  ```sh
  for p in $(pgrep -f 'YaneuraOu-'); do
    case "$(readlink /proc/$p/cwd)" in
      "$HOME/shogibench-worker/Client"|"$HOME/shogibench-worker/Client/Engines") kill -9 "$p";;
    esac
  done
  ```

### 3-6. 動作確認

- サーバーの `/machines/` に数十秒以内にマシンが現れます
- `/workers/` の Last Used が更新されます
- テストを作成 (`/test/new/`) すると、対応エンジンをビルドして対局が始まります

インスタンスを破棄すればワーカーは消えます。サーバー側の後始末は不要です
(マシン一覧は最近アクティブなものだけが表示されます)。

---

## 4. セキュリティ上の注意

- ページ自体は公開ですが、書き込み系 (テスト作成・承認・ネット管理・ワーカー
  API) はすべて認証必須です
- vast.ai インスタンスは第三者のハードウェアです。**アカウントパスワードや
  GitHub トークンを置かず、ワーカーキーだけを渡してください**
- プライベートエンジンを扱う場合 (engine config の `private: true`) は
  ワーカーに GitHub PAT が必要になるため、レンタルインスタンスでの利用は
  推奨しません
- `OPENBENCH_SECRET_KEY` を変更するとログインセッションが無効になります
  (データは消えません)

---

## 5. ローカル開発

環境変数なしで従来どおり動きます (`DEBUG=True`、DB はリポジトリ直下):

```sh
pip install -r requirements.txt
python manage.py migrate
python manage.py invite dev --approver
python manage.py runserver
```

テスト実行:

```sh
python manage.py test OpenBench
```

---

## 6. ビルドバリアント(同一ブランチでビルド違いの対戦)

ブランチを分けなくても、**make引数の違い**で別エンジンとして対戦させられます。

バリアントの定義方法は3つあります:

1. **Web UI(推奨)**: サイドバーの「ビルド設定」(`/builds/`)でビルドコマンドを
   貼り付けるだけ。`make` 本体・`-j`・`EXE=`・`EVALFILE=`・`CXX=` は自動除去され、
   make引数として登録されます。登録後すぐテスト作成フォームに現れます
2. **テスト作成時にその場で入力**: フォームの「ビルド引数」欄に直接make引数を
   書くと、ドロップダウンの選択を上書きします(1回限りの試行に便利)
3. **JSON**: `Engines/<エンジン>.json` の `build.variants`(組み込みのシード用)

- テスト作成フォームの **Dev ビルド / Base ビルド** ドロップダウンで選択します
- ワーカーは `(コミットsha, ネットワーク, ビルド引数)` ごとに別バイナリとして
  ビルド・キャッシュするので衝突しません
- バリアントごとに bench 値が異なる場合は、フォームの Bench 欄に手入力してください

例(YaneuraOu):

```json
"variants" : {
    "NNUE"     : "normal COMPILER=clang++ TARGET_CPU=AVX2 YANEURAOU_EDITION=YANEURAOU_ENGINE_NNUE",
    "KPPT"     : "normal COMPILER=clang++ TARGET_CPU=AVX2 YANEURAOU_EDITION=YANEURAOU_ENGINE_KPPT",
    "MATERIAL" : "normal COMPILER=clang++ TARGET_CPU=AVX2 YANEURAOU_EDITION=YANEURAOU_ENGINE_MATERIAL"
}
```

Network(評価関数)違いの対戦は従来どおり: `/networks/` にファイルを登録し、
テスト作成時に Dev/Base で別のネットワークを選ぶだけです(ブランチは同一でOK)。

### OpenBench非対応Makefileのエンジン(YaneuraOu等)向けフック

- `build.binary`: Makefileが `EXE=` を無視して固定名のバイナリを出力する場合、
  その名前を指定するとワーカーがリネームして扱います(例: `"YaneuraOu-by-gcc"`)
- `build.network_option`: `EVALFILE=` での埋め込みに非対応のエンジンは、
  ネットワークを実行時のUSIオプションとして渡します(例: `"EvalFile"`)

### YaneuraOu 対応(実ビルドで検証済み)

以下は keinoda/YaneuraOu master を実際にビルド・実行して確認済みです:

1. **未知のNNUEアーキテクチャ**(例 `HALFKP_768X2_16_64`)は Makefile が
   `nnue_arch_gen.py` でヘッダを動的生成するため、ビルドコマンドを
   そのまま渡すだけで動きます
2. **bench**: 既定の `bench` は時間ベース(≈60秒・非決定的)なので、
   engine config の `build.bench_args = "16 1 100000 default nodes"` により
   ノード制限モードで実行します(決定的・約0.3秒、実測で確認済み)。
   **Bench 欄は空欄でかまいません**(空欄=照合なし。NPS計測のための
   bench 自体は常に実行されます)。ビルド・評価関数の読み込みまで厳密に
   検証したい場合のみ数値を入れます。値はビルド+評価関数の組ごとに
   異なり、手元で `./YaneuraOu-by-gcc bench 16 1 100000 default nodes` を
   実行した際の `Nodes searched`、またはわざと `1` を入れてワーカーの
   `Wrong Bench: <実際の値>` エラーで知ることができます
3. **ネットワーク配布**: YaneuraOu は `EvalFile` ではなく `EvalDir`+固定名
   `nn.bin` 方式のため、ワーカーが `Networks/<sha>-dir/nn.bin` を自動で
   用意して `EvalDir` オプションで渡します(`build.network_option` +
   `build.network_filename` で設定済み)
4. **補助ファイル(複数可)**: ネット登録時に「補助ファイル」欄で何個でも
   一緒にアップロードできます。ワーカーは**すべての補助ファイルを nn.bin と
   同じディレクトリに元のファイル名で配置**します。さらに:
   - engine config の `build.network_aux_options` に載っている名前のファイル
     (例: `"progress.bin": "LS_PROGRESS_COEFF"`)は、そのパスが対応する
     USIオプションとして渡されます(絶対パス)。オプション名は全YaneuraOu系
     エンジンで `LS_PROGRESS_COEFF` に統一済み
   - 補助ファイルは登録後でも `/networks/` → 該当ネットのEDITページで
     追加・削除できます(再アップロード不要)
   - 診断: インスタンス上で `Scripts/progress_check.sh` を実行すると、
     配置→オプション存在→読み込み→効果を自動判定します
   - **`eval_options.txt`** という名前のファイルは特別扱いで、`Name=Value` を
     1行ずつ書いておくと **bench と対局の両方で必ず setoption として適用**
     されます(`#` 以降はコメント)。評価関数ごとに指定が必須のオプション
     (バケット選択方式など)は、ここに書いてネットと一緒に登録しておけば
     指定漏れが起きません。**値に空白は使えません**
   - パスを取るオプションは、値の中の **`{DIR}`** が配置先ディレクトリに
     置換されます。対象ファイル自体も補助ファイルとして一緒に登録します。
     例: `LS_PROGRESS_COEFF={DIR}/coeff.bin`(coeff.bin も補助ファイルで
     アップロード)。EvalDir 等ワーカーが管理するオプションの上書きは無視
     されます
   - `{DIR}` は**テスト作成フォームのオプション欄でも**使えます(Dev/Base
     それぞれ自分のネットの配置先に展開)。eval_options.txt を使わず
     `Threads=1 Hash=256 LS_BUCKET_MODE=... LS_PROGRESS_COEFF={DIR}/coeff.bin`
     のようにテスト側で指定する運用も可能です。ただしオプション欄の内容は
     **対局にのみ**適用され、bench には適用されません(eval_options.txt は
     両方に適用)
   - 注意: `eval_options.txt` の内容は bench 結果にも影響するため、
     オプションを変えると bench 値も変わります(Bench欄は空欄=照合なしが
     便利です)
5. MATERIAL エディションは評価ファイル不要なので、パイプラインの動作確認に便利です

## 7. ネットワーク(評価関数)のアップロード

100〜200MB級のファイルはストリーミング処理されるため、512MBの小さな
サーバーでも問題ありません。時間のかかるアップロードはサーバーを
占有しません(他のページは並行して応答します)。

ブラウザを開いたままにしたくない場合は、**手元のマシンからCLIで
バックグラウンドアップロード**できます:

```sh
nohup curl -sS -X POST https://<あなたのサーバー>/scripts/ \
  -F action=UPLOAD_NETWORK \
  -F engine=YaneuraOu-nagisa \
  -F name=mynet.bin \
  -F username=<ユーザー名> -F password=<アカウントのパスワード> \
  -F netfile=@/path/to/nn.bin \
  -F auxfiles=@/path/to/eval_options.txt \
  -F auxfiles=@/path/to/progress.bin > upload.log 2>&1 &
```

- ここは Web ログインと同じ扱いのため、ワーカーキーではなく
  **アカウントのパスワード**が必要です(approver 権限も必要)
- Fly.io のディスク容量に注意: ネットは `/data` のボリュームに保存されます。
  足りなくなったら `fly volumes extend <volume-id> -s <GB>` で拡張できます

## 8. 互角局面集(開始局面ブック)

よく使われる互角局面集を同梱しています。テスト作成フォームの Book 欄で
選択でき、ワーカーは `Books/dist/` の zip をリポジトリから自動取得して
sha256 を検証します。いずれも shogitest が読める「1行1局面のSFEN
(盤面 手番 持駒 手数 の4フィールド)」に変換済みです。

| ブック名 | 局面数 | 出典 |
|---|---|---|
| `taya36_shogi_sfen.epd` | 1,952 | たややん互角局面集(36手目)。水匠系/TanukiColiseum の事実上の標準。floodgate R3800+ の対局から評価値±100以内で抽出 |
| `yaneuraou2025_ply24_shogi_sfen.epd` | 30,014 | やねうら王互角局面集2025(24手目)。水匠10・2億ノードで検証、\|評価値\|≤50。MITライセンス |
| `yaneuraou2025_ply32_shogi_sfen.epd` | 26,209 | 同上(32手目) |
| `dlshogi_gokaku_sfen.epd` | 5,233 | 山岡忠夫氏(dlshogi)の互角局面集(36手目)。floodgate R3800+ から抽出 |
| `dlshogi_floodgate32to80_sfen.epd` | 8,071 | 同氏の中盤互角局面集(32〜80手目、評価値±150以内)。中盤力の測定向け |
| `4moves/6moves_v1_shogi_sfen.epd` | - | 旧来の浅い局面集(互換用に残置) |

- 「startpos moves ...」形式の原本は python-shogi で局面を再生して
  SFEN化しています(重複局面=合流は原本どおり保持)
- 原本より局面数がわずかに少ないのは、**手番側に王手がかかっている
  局面を除外**しているため(shogitest は王手つき開始局面を扱えない)
- 出典: たややん氏(@tayayan_ts)、やねうら王
  (github.com/yaneurao/YaneuraOu Releases "BalancedPositions2025")、
  山岡忠夫氏(tadaoyamaoka.hatenablog.com)。再配布にあたっては各氏の
  公開条件に従います
- 新しいブックを足すには: SFEN化した `.epd` を単体で zip し
  `Books/dist/` に置き、`Books/<名前>.json` に sha256(zip内ファイルの
  中身のsha)と source URL を書いて `config.json` の books に追加。
  **ファイル名に "shogi" を含めること**(ワーカーが将棋対局と判定する条件)

デフォルトプリセットのブックは `taya36_shogi_sfen.epd` です。
参考: 一般的な測定条件は、水匠/tanuki-系が「持ち時間300秒+1手2秒加算・
1スレッド・5000局」、やねうら王系が「1手1〜4秒の短時間・数千局」、
dlshogi系が「持ち時間400秒+2秒加算」など。ShogiBench のプリセット
(VSTC〜VLTC、SMP、固定ノード、秒読み風)はこれらを参考にしています。

## 9. shogitest フォーク(keinoda/shogitest)

対局実行には `keinoda/shogitest` の `shogibench` ブランチを使用します(v0.1.2)。
本家からの主な変更:

- **成績表示を先頭エンジン(=Dev)基準に修正**: 従来は「A vs B」と表示しつつ
  Elo/勝敗がB基準で計算されており、直感と逆でした
- ヘッダーに `(score for A)` と基準を明示
- `tc=inf` を許容(固定ノード指定の互換性)
- 警告・エラー出力の改行修正
