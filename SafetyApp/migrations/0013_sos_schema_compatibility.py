from django.db import migrations

from SafetyApp.migration_support_0011 import EnsureSOSWorkflowSchema


class Migration(migrations.Migration):

    dependencies = [
        ('SafetyApp', '0012_emergency_contact_consent_verification'),
    ]

    operations = [
        EnsureSOSWorkflowSchema(),
    ]
