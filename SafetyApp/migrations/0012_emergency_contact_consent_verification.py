from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('SafetyApp', '0011_sos_cross_process_leases'),
    ]

    operations = [
        migrations.AddField(
            model_name='emergencycontact',
            name='email_consent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='emergencycontact',
            name='sms_consent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='emergencycontact',
            name='voice_consent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='emergencycontact',
            name='consent_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='emergencycontact',
            name='email_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='emergencycontact',
            name='phone_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
