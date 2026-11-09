import uuid

from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone

from SafetyApp.migration_support_0011 import EnsureSOSWorkflowSchema


class Migration(migrations.Migration):

    dependencies = [
        ('SafetyApp', '0010_personal_safety_profile'),
    ]

    _sos_workflow_state = [
        migrations.CreateModel(
            name='SOSNotificationAttempt',
            fields=[
                ('id', models.AutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('channel', models.CharField(
                    choices=[('EMAIL', 'Email'), ('SMS', 'SMS'), ('VOICE', 'Voice')],
                    max_length=5,
                )),
                ('status', models.CharField(
                    choices=[
                        ('QUEUED', 'Queued'),
                        ('SENDING', 'Sending'),
                        ('ACCEPTED', 'Accepted'),
                        ('DELIVERED', 'Delivered'),
                        ('FAILED', 'Failed'),
                        ('NO_ANSWER', 'No answer'),
                        ('BUSY', 'Busy'),
                        ('UNCERTAIN', 'Uncertain'),
                        ('ACKNOWLEDGED', 'Acknowledged'),
                        ('CANCELLED', 'Cancelled'),
                    ],
                    default='QUEUED',
                    max_length=14,
                )),
                ('attempt_number', models.PositiveIntegerField()),
                ('idempotency_key', models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    unique=True,
                )),
                ('claim_token', models.UUIDField(blank=True, null=True)),
                ('claim_generation', models.PositiveIntegerField(default=0)),
                ('lease_expires_at', models.DateTimeField(blank=True, null=True)),
                ('provider_reference', models.CharField(blank=True, default='', max_length=255)),
                ('scheduled_at', models.DateTimeField(default=timezone.now)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, default='', max_length=100)),
                ('error_message', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('emergency_contact', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sos_notification_attempts',
                    to='SafetyApp.EmergencyContact',
                )),
                ('sos_event', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='notification_attempts',
                    to='SafetyApp.SOSEvent',
                )),
            ],
            options={
                'unique_together': {
                    ('sos_event', 'emergency_contact', 'channel', 'attempt_number'),
                },
                'indexes': [
                    models.Index(
                        fields=['sos_event', 'status', 'scheduled_at'],
                        name='sos_attempt_due_idx',
                    ),
                    models.Index(
                        fields=['emergency_contact', 'channel', 'status'],
                        name='sos_attempt_contact_idx',
                    ),
                    models.Index(
                        fields=['status', 'lease_expires_at'],
                        name='sos_attempt_lease_idx',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='SOSContactAcknowledgement',
            fields=[
                ('id', models.AutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('method', models.CharField(
                    choices=[
                        ('VOICE_DTMF', 'Voice keypad'),
                        ('WEB_LINK', 'Web link'),
                    ],
                    max_length=10,
                )),
                ('acknowledged_at', models.DateTimeField(blank=True, null=True)),
                ('token_hash', models.CharField(
                    blank=True,
                    max_length=64,
                    null=True,
                    unique=True,
                )),
                ('token_expires_at', models.DateTimeField(blank=True, null=True)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('emergency_contact', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sos_acknowledgements',
                    to='SafetyApp.EmergencyContact',
                )),
                ('sos_event', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='contact_acknowledgements',
                    to='SafetyApp.SOSEvent',
                )),
            ],
            options={
                'unique_together': {('sos_event', 'emergency_contact')},
                'indexes': [
                    models.Index(
                        fields=['sos_event', 'acknowledged_at'],
                        name='sos_ack_event_time_idx',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='SOSEscalationState',
            fields=[
                ('id', models.AutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('status', models.CharField(
                    choices=[
                        ('RUNNING', 'Running'),
                        ('ACKNOWLEDGED', 'Acknowledged'),
                        ('CANCELLED', 'Cancelled'),
                        ('RESOLVED', 'Resolved'),
                        ('EXHAUSTED', 'Exhausted'),
                    ],
                    default='RUNNING',
                    max_length=14,
                )),
                ('current_contact_order', models.PositiveIntegerField(blank=True, null=True)),
                ('contact_sequence', models.TextField(default='[]')),
                ('acknowledgement_timeout_seconds', models.PositiveIntegerField(default=60)),
                ('max_retries_per_channel', models.PositiveIntegerField(default=1)),
                ('acknowledgement_deadline', models.DateTimeField(blank=True, null=True)),
                ('next_action_at', models.DateTimeField(blank=True, null=True)),
                ('lease_owner', models.CharField(blank=True, default='', max_length=100)),
                ('lease_token', models.UUIDField(blank=True, null=True)),
                ('lease_expires_at', models.DateTimeField(blank=True, null=True)),
                ('lease_generation', models.PositiveIntegerField(default=0)),
                ('policy_version', models.CharField(default='v1', max_length=32)),
                ('started_at', models.DateTimeField(default=timezone.now)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('current_contact', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='current_sos_escalations',
                    to='SafetyApp.EmergencyContact',
                )),
                ('sos_event', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='escalation_state',
                    to='SafetyApp.SOSEvent',
                )),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['status', 'next_action_at'],
                        name='sos_escalation_next_idx',
                    ),
                    models.Index(
                        fields=['status', 'acknowledgement_deadline'],
                        name='sos_escalation_deadline_idx',
                    ),
                    models.Index(
                        fields=['status', 'lease_expires_at'],
                        name='sos_escalation_lease_idx',
                    ),
                ],
            },
        ),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                EnsureSOSWorkflowSchema(
                    state_operations=_sos_workflow_state,
                ),
            ],
            state_operations=_sos_workflow_state,
        ),
    ]
