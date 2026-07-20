# 棋譜をワーカー終了時の一括送信ではなく、回収済みの差分ごとに保持する。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("OpenBench", "0014_test_ponder_modes"),
    ]

    operations = [
        migrations.AddField(
            model_name="pgn",
            name="part",
            field=models.IntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="pgn",
            constraint=models.UniqueConstraint(
                fields=("test_id", "result_id", "book_index", "part"),
                name="unique_pgn_archive_part",
            ),
        ),
    ]
