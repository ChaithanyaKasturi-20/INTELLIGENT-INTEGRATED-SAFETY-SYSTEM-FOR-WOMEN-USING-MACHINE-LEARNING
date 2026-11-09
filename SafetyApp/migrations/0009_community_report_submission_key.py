from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('SafetyApp', '0008_sos_unique_active_event'),
    ]

    operations = [
        migrations.AddField(
            model_name='communityreport',
            name='submission_key',
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
    ]
