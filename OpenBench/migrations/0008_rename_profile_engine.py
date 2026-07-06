# 0007 renamed the engine on Tests, Networks and Build Variants, but
# missed Profile.engine — the per-user default that create_workload.html
# uses to initialize the page. A stale name there broke the page's
# javascript (no presets, empty dropdowns) until the user re-picked an
# engine by hand. Point every stale profile at the renamed engine.

from django.db import migrations

NEW = 'YaneuraOu-keinoda'

def rename_profile_engine(apps, schema_editor):
    profile = apps.get_model('OpenBench', 'Profile')
    profile.objects.filter(engine__in=['YaneuraOu', 'Stoat']).update(engine=NEW)

def undo_rename_profile_engine(apps, schema_editor):
    profile = apps.get_model('OpenBench', 'Profile')
    profile.objects.filter(engine=NEW).update(engine='YaneuraOu')

class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0007_rename_engine_yaneuraou_keinoda'),
    ]

    operations = [
        migrations.RunPython(rename_profile_engine, undo_rename_profile_engine),
    ]
