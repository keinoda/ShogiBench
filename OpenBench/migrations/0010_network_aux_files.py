# Networks may now carry any number of auxiliary files, each keeping its
# original filename. The single aux_sha256 slot (only ever used for
# YaneuraOu's progress.bin) becomes a NetworkAuxFile row of that name.

import django.db.models.deletion
from django.db import migrations, models

def forward_aux_files(apps, schema_editor):
    network = apps.get_model('OpenBench', 'Network')
    auxfile = apps.get_model('OpenBench', 'NetworkAuxFile')
    for net in network.objects.exclude(aux_sha256=''):
        auxfile.objects.create(network=net, name='progress.bin', sha256=net.aux_sha256)

def backward_aux_files(apps, schema_editor):
    auxfile = apps.get_model('OpenBench', 'NetworkAuxFile')
    for aux in auxfile.objects.filter(name='progress.bin'):
        aux.network.aux_sha256 = aux.sha256
        aux.network.save()

class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0009_rename_engine_yaneuraou_nagisa'),
    ]

    operations = [
        migrations.CreateModel(
            name='NetworkAuxFile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=64)),
                ('sha256', models.CharField(max_length=8)),
                ('network', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='aux_files', to='OpenBench.network')),
            ],
            options={
                'unique_together': {('network', 'name')},
            },
        ),
        migrations.RunPython(forward_aux_files, backward_aux_files),
        migrations.RemoveField(
            model_name='network',
            name='aux_sha256',
        ),
    ]
