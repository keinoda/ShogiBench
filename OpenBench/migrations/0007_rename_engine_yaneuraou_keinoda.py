# Rename the engine "YaneuraOu" to "YaneuraOu-keinoda" everywhere the
# engine name is stored as a string. Many people run their own YaneuraOu
# forks, so the engine entry carries the fork owner's name to avoid
# confusion. Rows referencing removed engines (eg "Stoat") are left
# untouched: the server skips unknown engines when handing out workloads.

from django.db import migrations

OLD, NEW = 'YaneuraOu', 'YaneuraOu-keinoda'

def rename_engine(apps, schema_editor):
    for model_name, fields in [
        ('Test',         ['dev_engine', 'base_engine']),
        ('Network',      ['engine']),
        ('BuildVariant', ['engine']),
    ]:
        model = apps.get_model('OpenBench', model_name)
        for field in fields:
            model.objects.filter(**{field: OLD}).update(**{field: NEW})

def undo_rename_engine(apps, schema_editor):
    for model_name, fields in [
        ('Test',         ['dev_engine', 'base_engine']),
        ('Network',      ['engine']),
        ('BuildVariant', ['engine']),
    ]:
        model = apps.get_model('OpenBench', model_name)
        for field in fields:
            model.objects.filter(**{field: NEW}).update(**{field: OLD})

class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0006_network_aux_sha256'),
    ]

    operations = [
        migrations.RunPython(rename_engine, undo_rename_engine),
    ]
