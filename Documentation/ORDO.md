# Ordo向け集計の復元

ShogiBenchは棋譜を保存していないテストでも、Test行にTrinomialのW/D/Lと
Pentanomialの5分類を保持しています。`export_ordo` は両方を照合し、各ペアを
先後1局ずつの最小PGNとして復元します。削除操作は論理削除なので、削除済みの
テストも既定で含まれます。

```sh
python manage.py export_ordo --output shogibench.pgn
ordo -a 0 -D -s 10000 -p shogibench.pgn -o ratings.txt -c ratings.csv
```

`SPSA` は対局中にパラメータが変わるため対象外です。コミット・評価関数・
ビルドが完全に同じ自己対局も、相対レーティングの情報を持たないため除外します。

## テスト119・120・122の3者比較

```sh
python manage.py export_ordo \
  --test-id 119 \
  --test-id 120 \
  --test-id 122 \
  --time-control-override 120=8.0+0.08 \
  --output shogibench-119-120-122.pgn
```

テスト120のDB上の記録は `40.0+0.40` のまま変更しません。解析上の条件だけを
`8.0+0.08` とし、PGNには有効条件と記録条件を両方出力します。また、テスト120の
`Hash=256` も119・122の `Hash=64` へ置換せず、そのままPGNのオプションタグへ
残します。

実際の各勝敗が先手・後手のどちらで発生したかは集計値から復元できません。
非対称なペアの向きを交互に割り当てるため、点推定用の先後数は均等になりますが、
Ordoのシミュレーション誤差はShogiBenchのペア単位の不確実性を表しません。

## 旧32手開始局面集による3者比較

旧計測条件の比較には、テスト103・109・110を使用します。104・105では
ありません。

```sh
python manage.py export_ordo \
  --test-id 103 \
  --test-id 109 \
  --test-id 110 \
  --output shogibench-103-109-110.pgn
```

3テストとも `8.0+0.08`、`Threads=1 Hash=64`、開始局面集は
`yaneuraou2025_ply32_shogi_sfen.epd` です。
