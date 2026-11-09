from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('SafetyApp', '0009_community_report_submission_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='emergencycontact',
            name='alternative_phone_number',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.CreateModel(
            name='PersonalSafetyProfile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(db_index=True, max_length=150, unique=True)),
                ('full_name', models.CharField(blank=True, max_length=150)),
                ('date_of_birth', models.DateField(blank=True, null=True)),
                ('gender', models.CharField(blank=True, choices=[('', 'Prefer not to say'), ('female', 'Female'), ('male', 'Male'), ('non_binary', 'Non-binary'), ('other', 'Other')], max_length=20)),
                ('medical_consent', models.BooleanField(default=False)),
                ('blood_group', models.CharField(blank=True, choices=[('', 'Not specified'), ('A+', 'A+'), ('A-', 'A-'), ('B+', 'B+'), ('B-', 'B-'), ('AB+', 'AB+'), ('AB-', 'AB-'), ('O+', 'O+'), ('O-', 'O-')], max_length=3)),
                ('medical_conditions', models.TextField(blank=True)),
                ('allergies', models.TextField(blank=True)),
                ('medications', models.TextField(blank=True)),
                ('medical_notes', models.TextField(blank=True)),
                ('disability_information', models.TextField(blank=True)),
                ('emergency_instructions', models.TextField(blank=True)),
                ('safety_notes', models.TextField(blank=True)),
                ('additional_emergency_information', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
