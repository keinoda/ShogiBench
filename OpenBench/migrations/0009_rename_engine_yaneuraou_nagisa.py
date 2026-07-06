# The engine registered as "YaneuraOu-keinoda" is now "YaneuraOu-nagisa",
# and a second engine "YaneuraOu" (the upstream yaneurao/YaneuraOu) exists
# alongside it. Rename every stored reference, Profile included this time.

from django.db import migrations

OLD, NEW = 'YaneuraOu-keinoda', 'YaneuraOu-nagisa'

def rename_engine(apps, schema_editor):
    for model_name, fields in [
        ('Test',         ['dev_engine', 'base_engine']),
        ('Network',      ['engine']),
        ('BuildVariant', ['engine']),
        ('Profile',      ['engine']),
    ]:
        model = apps.get_model('OpenBench', model_name)
        for field in fields:
            model.objects.filter(**{field: OLD}).update(**{field: NEW})

def undo_rename_engine(apps, schema_editor):
    for model_name, fields in [
        ('Test',         ['dev_engine', 'base_engine']),
        ('Network',      ['engine']),
        ('BuildVariant', ['engine']),
        ('Profile',      ['engine']),
    ]:
        model = apps.get_model('OpenBench', model_name)
        for field in fields:
            model.objects.filter(**{field: NEW}).update(**{field: OLD})

class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0008_rename_profile_engine'),
    ]

    operations = [
        migrations.RunPython(rename_engine, undo_rename_engine),
    ]
