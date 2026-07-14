# テスト API

現在のテスト状況の取得と、ブラウザの `/test/new/` と同じ検証・作成処理を利用できます。

## エンドポイントと認証

```text
GET  https://shogibench.fly.dev/api/tests/
POST https://shogibench.fly.dev/api/tests/
```

HTTP Basic 認証を推奨します。通常のアカウント名とアカウントパスワードを使います。
Worker Key はワーカー専用であり、この API には利用できません。

POST のフォーム形式では、本文の `username` と `password` でも認証できます。JSON と GET では資格情報を本文やクエリへ入れず、Basic 認証を使ってください。

## テスト状況の取得（GET）

パラメータなしでは、削除されていない未完了テストを最大50件、新しい順に返します。

```sh
curl --fail-with-body \
  --user "$SHOGIBENCH_USERNAME:$SHOGIBENCH_PASSWORD" \
  https://shogibench.fly.dev/api/tests/
```

クエリパラメータは次のとおりです。

| パラメータ | 既定値 | 内容 |
|---|---:|---|
| `status` | `current` | `current`、`pending`、`awaiting`、`active`、`completed`、`all` |
| `author` | 指定なし | 作成者名との完全一致で絞り込み |
| `limit` | `50` | 1回に返す件数。1〜200 |
| `offset` | `0` | 先頭から読み飛ばす件数。0以上 |

状態の意味は次のとおりです。状態は排他的で、`current` は `pending`、`awaiting`、`active` をまとめた未完了テストです。

- `pending`: 未承認で、ビルド成果物待ちではない
- `awaiting`: ビルド成果物待ち
- `active`: 承認済みで実行可能または実行中
- `completed`: 完了済み
- `all`: 削除済みを除くすべて

たとえば Agent-AI が作成した完了テストの2ページ目を50件取得する場合は、次のように指定します。

```sh
curl --fail-with-body \
  --user "$SHOGIBENCH_USERNAME:$SHOGIBENCH_PASSWORD" \
  'https://shogibench.fly.dev/api/tests/?status=completed&author=Agent-AI&limit=50&offset=50'
```

応答には、指定した検索条件、状態別件数、ページング情報、テスト一覧が入ります。`summary` は `author` 指定後の全状態を集計し、`tests` だけが `status` とページングの対象です。

```json
{
  "query": {
    "status": "current",
    "author": null
  },
  "summary": {
    "all": 12,
    "current": 3,
    "pending": 1,
    "awaiting": 0,
    "active": 2,
    "completed": 9
  },
  "pagination": {
    "total": 3,
    "limit": 50,
    "offset": 0,
    "returned": 3,
    "has_more": false
  },
  "tests": [
    {
      "id": 123,
      "url": "https://shogibench.fly.dev/test/123/",
      "author": "Agent-AI",
      "status": "active",
      "workload_type": "test",
      "test_mode": "SPRT",
      "created_at": "2026-07-15T00:00:00+00:00",
      "updated_at": "2026-07-15T01:00:00+00:00",
      "flags": {
        "approved": true,
        "awaiting": false,
        "finished": false,
        "passed": false,
        "failed": false,
        "error": false
      },
      "engines": {
        "dev": {
          "engine": "YaneuraOu-nagisa",
          "repo": "https://github.com/keinoda/YaneuraOu",
          "branch": "feature-branch",
          "sha": "0123456789abcdef0123456789abcdef01234567",
          "bench": 1234567,
          "display": "",
          "network": { "sha256": "", "name": "" },
          "build": { "name": "default", "args": "" },
          "options": "Threads=1 Hash=256",
          "time_control": "8.0+0.08",
          "ponder_mode": "off"
        },
        "base": {
          "engine": "YaneuraOu-nagisa",
          "repo": "https://github.com/keinoda/YaneuraOu",
          "branch": "master",
          "sha": "fedcba9876543210fedcba9876543210fedcba98",
          "bench": 1234567,
          "display": "",
          "network": { "sha256": "", "name": "" },
          "build": { "name": "default", "args": "" },
          "options": "Threads=1 Hash=256",
          "time_control": "8.0+0.08",
          "ponder_mode": "off"
        }
      },
      "settings": {
        "book_name": "yaneuraou2025_ply24_shogi_sfen.epd",
        "upload_pgns": "FALSE",
        "priority": 0,
        "throughput": 1000,
        "workload_size": 8,
        "scale_method": "BASE",
        "scale_nps": 1000000,
        "syzygy_wdl": "DISABLED",
        "syzygy_adj": "DISABLED",
        "win_adj": "None",
        "draw_adj": "None"
      },
      "mode_config": {
        "elo_bounds": [0.0, 5.0],
        "confidence": { "alpha": 0.05, "beta": 0.05 },
        "llr": { "lower": -2.9444, "current": 0.25, "upper": 2.9444 }
      },
      "results": {
        "games": 100,
        "wins": 40,
        "losses": 35,
        "draws": 25,
        "pentanomial": { "LL": 10, "LD": 5, "DD": 20, "DW": 8, "WW": 7 }
      }
    }
  ]
}
```

`mode_config` は `SPRT`、`GAMES`、`SPSA`、`DATAGEN` ごとに必要な進捗設定を返します。

## テスト作成（POST）

### JSON 例

すべての値は文字列で送ります。ネットワークなしは空文字です。

```sh
curl --fail-with-body \
  --user "$SHOGIBENCH_USERNAME:$SHOGIBENCH_PASSWORD" \
  --header 'Content-Type: application/json' \
  --data @- \
  https://shogibench.fly.dev/api/tests/ <<'JSON'
{
  "dev_engine": "YaneuraOu-nagisa",
  "dev_repo": "https://github.com/keinoda/YaneuraOu",
  "dev_branch": "feature-branch",
  "dev_bench": "",
  "dev_network": "",
  "dev_build": "default",
  "dev_options": "Threads=1 Hash=256",
  "dev_time_control": "8.0+0.08",
  "dev_ponder_mode": "off",

  "base_engine": "YaneuraOu-nagisa",
  "base_repo": "https://github.com/keinoda/YaneuraOu",
  "base_branch": "master",
  "base_bench": "",
  "base_network": "",
  "base_build": "default",
  "base_options": "Threads=1 Hash=256",
  "base_time_control": "8.0+0.08",
  "base_ponder_mode": "off",

  "book_name": "yaneuraou2025_ply24_shogi_sfen.epd",
  "upload_pgns": "FALSE",
  "test_mode": "SPRT",
  "test_bounds": "[0.00, 5.00]",
  "test_confidence": "[0.05, 0.05]",

  "priority": "0",
  "throughput": "1000",
  "workload_size": "8",
  "scale_method": "BASE",
  "scale_nps": "1000000",
  "syzygy_wdl": "DISABLED",
  "syzygy_adj": "DISABLED",
  "win_adj": "None",
  "draw_adj": "None"
}
JSON
```

固定対局数では `test_mode` を `GAMES` にし、`test_bounds` と `test_confidence` の代わりに `test_max_games` を指定します。

```json
{
  "test_mode": "GAMES",
  "test_max_games": "2000"
}
```

### フィールド

Dev/Base それぞれに次を指定します。

| フィールド | 必須 | 内容 |
|---|---:|---|
| `<side>_engine` | はい | `Config/config.json` に登録されたエンジン名 |
| `<side>_repo` | はい | GitHub リポジトリ URL |
| `<side>_branch` | はい | ブランチ、タグ、または40桁コミット SHA |
| `<side>_bench` | いいえ | 空文字なら bench 値の照合を省略 |
| `<side>_display` | いいえ | 詳細画面用の表示名 |
| `<side>_network` | はい | ネットワーク SHA256。未使用時は空文字 |
| `<side>_build` | いいえ | 登録済みビルド名。省略時は `default` |
| `<side>_build_custom` | いいえ | 自由入力の make 引数。指定時は `<side>_build` より優先 |
| `<side>_options` | はい | `Threads` と `Hash` を含む USI オプション |
| `<side>_time_control` | はい | 例: `8.0+0.08` |
| `<side>_ponder_mode` | いいえ | `off`、`standard`、`early`。省略時は `off` |

`<side>` は `dev` または `base` です。共通設定には次が必要です。

- `book_name`
- `upload_pgns`: `FALSE`、`COMPACT`、`VERBOSE`
- `test_mode`: `SPRT` または `GAMES`
- `priority`、`throughput`、`workload_size`
- `scale_method`: `DEV`、`BASE`、`BOTH`
- `scale_nps`
- `syzygy_wdl`、`syzygy_adj`
- `win_adj`、`draw_adj`

エンジン・開始局面集は認証付き `GET /api/config/`、ネットワークは認証付き `GET /api/networks/<engine>/` で確認できます。どちらも同じ HTTP Basic 認証を利用できます。

### 応答

成功時は HTTP 201 です。Approver でないユーザーが作ったテストは `approved: false` で、承認されるまでワーカーへ配信されません。

```json
{
  "test": {
    "id": 123,
    "url": "https://shogibench.fly.dev/test/123/",
    "author": "Agent-AI",
    "approved": false,
    "awaiting": false
  }
}
```

主なエラーコードは次のとおりです。

- `400`: GET のクエリ、JSON、必須フィールド、またはテスト設定が不正
- `401`: アカウント名またはパスワードが不正
- `403`: アカウントが無効
- `405`: GET、POST 以外のメソッド

400 応答の `details` に、不足フィールドまたは `/test/new/` と同じ検証エラーが入ります。
