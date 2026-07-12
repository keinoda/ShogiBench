# Ponder方式を対局のDev/Baseごとに保持する。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("OpenBench", "0013_machine_token_column"),
    ]

    operations = [
        migrations.AddField(
            model_name="test",
            name="base_ponder_mode",
            field=models.CharField(
                choices=[("off", "無効"), ("standard", "通常 Ponder"), ("early", "早期 Ponder")],
                default="off",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="test",
            name="dev_ponder_mode",
            field=models.CharField(
                choices=[("off", "無効"), ("standard", "通常 Ponder"), ("early", "早期 Ponder")],
                default="off",
                max_length=16,
            ),
        ),
    ]
