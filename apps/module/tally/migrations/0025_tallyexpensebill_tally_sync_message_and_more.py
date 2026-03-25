from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tally', '0024_tallyvendoranalyzedbill_cess_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tallyexpensebill',
            name='tally_sync_message',
            field=models.TextField(blank=True, help_text='Message from Tally about sync result (success or error reason)', null=True),
        ),
        migrations.AddField(
            model_name='tallyvendorbill',
            name='tally_sync_message',
            field=models.TextField(blank=True, help_text='Message from Tally about sync result (success or error reason)', null=True),
        ),
    ]
