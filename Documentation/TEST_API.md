# テスト作成 API

ブラウザの `/test/new/` と同じ検証・作成処理を、JSON またはフォーム POST から利用できます。

## エンドポイントと認証

```text
POST https://shogibench.fly.dev/api/tests/
```

HTTP Basic 認証を推奨します。通常のアカウント名とアカウントパスワードを使います。
Worker Key はワーカー専用であり、テスト作成には利用できません。

フォーム形式では、本文の `username` と `password` でも認証できます。JSON では資格情報を本文へ入れず、Basic 認証を使ってください。

## JSON 例

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

## フィールド

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

## 応答

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

- `400`: JSON、必須フィールド、またはテスト設定が不正
- `401`: アカウント名またはパスワードが不正
- `403`: アカウントが無効
- `405`: POST 以外のメソッド

400 応答の `details` に、不足フィールドまたは `/test/new/` と同じ検証エラーが入ります。
