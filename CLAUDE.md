# ShogiBench

将棋エンジン (やねうら王系) のテスト・SPSAチューニング基盤。OpenBench の
Shogi 向けフォークで、Django サーバー (`OpenBench/`) とワーカー (`Client/`)
の2層構成。デプロイは `shogi` ブランチから行われる (Render/ワーカーの
自動更新も `shogi` を参照)。

## AI向けの主要ドキュメント

| やりたいこと | 読む文書 |
|---|---|
| **SPSA チューニングを実際に回す** | `Documentation/SPSA_RUNBOOK.md` — 準備→スモーク→本番→焼き戻し→SPRT検証まで、操作と判定基準を [操作]→[期待される結果]→[NGなら] 形式で書いたランブック |
| SPSA の仕組み・設計 (rshogi ラッパー / .tuneキット) | `Documentation/SPSA.md` |
| サーバー/ワーカーのデプロイ・運用 | `Documentation/DEPLOYMENT.md` |

## 開発メモ

- テスト実行: `python manage.py test OpenBench` (Django) と
  `python -m unittest discover -s UnitTests` (純ロジック)。両方通すこと
- ローカル起動: `pip install -r requirements.txt && python manage.py migrate
  && python manage.py invite dev --approver && python manage.py runserver`
- `Client/` はワーカーへフラットに配布される (サブディレクトリ不可、
  ローカル import は `import x` 形式のみ)。`Client/tune.py` / `ParamLib.py` は
  `Scripts/tune/` のコピーで、変更時は両方を同期する
- ワーカー互換性を壊す変更をしたら `Client/worker.py` の `CLIENT_VERSION` と
  `Config/config.json` の `client_version` を両方上げる (ワーカーが自動更新される)
- SPSA まわりの構成: サーバー側 `OpenBench/spsa_params.py` (params解析) /
  `OpenBench/tune_kits.py` (.tuneキット操作)、ワーカー側 `Client/spsa_rshogi.py`
  (rshogi spsa の起動・監視)、共有ロジック `Client/yotune.py` (照合・追随。
  サーバーもファイルロードで共用)
