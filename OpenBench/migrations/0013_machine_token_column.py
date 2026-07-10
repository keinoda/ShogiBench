# Machine.machine_token を JSON (info) から実カラムへ昇格する。
#
# バックフィルは (user, token) ごとに最新の行だけに入れる: 多重登録バグの
# 時代に同一トークンの行が量産されているため、全行に入れると直後の部分
# unique 制約が張れない。古い行はカラム空のまま残し、履歴としてのみ扱う。

from django.db import migrations, models


def backfill_machine_token(apps, schema_editor):

    Machine = apps.get_model('OpenBench', 'Machine')

    seen = set()
    for machine in Machine.objects.order_by('-id').iterator():
        token = (machine.info or {}).get('machine_token') or ''
        if not token or (machine.user_id, token) in seen:
            continue
        seen.add((machine.user_id, token))
        machine.machine_token = token
        machine.save(update_fields=['machine_token'])


class Migration(migrations.Migration):

    dependencies = [
        ("OpenBench", "0012_tunekit"),
    ]

    operations = [
        migrations.AddField(
            model_name="machine",
            name="machine_token",
            field=models.CharField(blank=True, db_index=True, default="", max_length=32),
        ),
        migrations.RunPython(backfill_machine_token, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="machine",
            constraint=models.UniqueConstraint(
                condition=models.Q(("machine_token", ""), _negated=True),
                fields=("user", "machine_token"),
                name="unique_machine_token_per_user",
            ),
        ),
    ]
