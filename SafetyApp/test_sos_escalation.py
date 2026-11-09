import json
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from .models import (
    EmergencyContact,
    SOSContactAcknowledgement,
    SOSEscalationState,
    SOSEvent,
    SOSNotificationAttempt,
)
from .sos_acknowledgement import (
    generate_web_acknowledgement_token,
    record_web_acknowledgement,
)
from .sos_notifications import ProviderResult
from .sos_escalation import (
    process_due_escalations,
    process_escalation_for_event,
    start_sos_escalation,
)


def _ensure_sos_workflow_test_schema():
    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        contact_table = EmergencyContact._meta.db_table
        if contact_table in existing_tables:
            with connection.cursor() as cursor:
                cursor.execute('SELECT * FROM {}'.format(connection.ops.quote_name(contact_table)))
                existing_columns = {
                    column[0]
                    for column in cursor.description
                }
            for field_name in (
                'email_consent',
                'sms_consent',
                'voice_consent',
                'email_verified_at',
                'phone_verified_at',
                'consent_updated_at',
            ):
                field = EmergencyContact._meta.get_field(field_name)
                if field.column not in existing_columns:
                    schema_editor.add_field(EmergencyContact, field)

        for model in (
            SOSNotificationAttempt,
            SOSContactAcknowledgement,
            SOSEscalationState,
        ):
            if model._meta.db_table not in existing_tables:
                schema_editor.create_model(model)
                existing_tables.add(model._meta.db_table)


class SOSEscalationTestCase(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super(SOSEscalationTestCase, cls).setUpClass()
        _ensure_sos_workflow_test_schema()

    def setUp(self):
        self.username = 'escalation-user'
        self.event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )
        self.primary = self.create_contact(
            name='Primary',
            phone='+1 202 555 0101',
            email='primary@example.org',
            is_primary=True,
        )
        self.secondary = self.create_contact(
            name='Secondary',
            phone='+1 202 555 0102',
            email='secondary@example.org',
        )
        self.third = self.create_contact(
            name='Third',
            phone='+1 202 555 0103',
            email='third@example.org',
        )

    def create_contact(self, name, phone, email, is_primary=False, channels=None):
        channels = channels or ('EMAIL',)
        now = timezone.now()
        return EmergencyContact.objects.create(
            user=self.username,
            name=name,
            phone_number=phone,
            email=email,
            is_primary=is_primary,
            is_active=True,
            email_consent='EMAIL' in channels,
            sms_consent='SMS' in channels,
            voice_consent='VOICE' in channels,
            email_verified_at=now if 'EMAIL' in channels else None,
            phone_verified_at=now if 'SMS' in channels or 'VOICE' in channels else None,
        )

    def create_attempt(self, contact, channel, status, number=1):
        return SOSNotificationAttempt.objects.create(
            sos_event=self.event,
            emergency_contact=contact,
            channel=channel,
            attempt_number=number,
            status=status,
        )

    def make_current_contact_due(self, event=None):
        event = event or self.event
        SOSEscalationState.objects.filter(sos_event=event).update(
            acknowledgement_deadline=timezone.now() - timedelta(seconds=1),
            next_action_at=timezone.now() - timedelta(seconds=1),
        )

    def test_primary_contact_is_first_in_snapshot(self):
        from unittest.mock import patch

        with patch('SafetyApp.sos_escalation.dispatch_contact_notifications'):
            state = start_sos_escalation(self.event)

        self.assertEqual(
            json.loads(state.contact_sequence),
            [self.primary.pk, self.secondary.pk, self.third.pk],
        )
        self.assertEqual(state.current_contact_id, self.primary.pk)
        self.assertEqual(state.current_contact_order, 1)

    def test_all_eligible_channels_are_dispatched_for_current_contact(self):
        from unittest.mock import patch

        all_channels = self.create_contact(
            name='All Channels',
            phone='+1 202 555 0199',
            email='all@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)

        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[1], all_channels)
        self.assertEqual(
            set(dispatch.call_args.kwargs['channels']),
            {
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            },
        )

    def test_contacts_are_processed_sequentially(self):
        from unittest.mock import patch

        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(dispatch.call_args.args[1], self.primary)

            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_ACCEPTED,
            )
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(dispatch.call_args.args[1], self.secondary)
        self.assertEqual(state.current_contact_id, self.secondary.pk)
        self.assertEqual(state.current_contact_order, 2)

    def test_timeout_retries_only_failed_channels_once(self):
        from unittest.mock import patch

        all_channels = self.create_contact(
            name='All Channels',
            phone='+1 202 555 0188',
            email='retry@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)
            SOSEscalationState.objects.filter(sos_event=self.event).update(
                max_retries_per_channel=5,
            )
            self.create_attempt(
                all_channels,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_FAILED,
            )
            self.create_attempt(
                all_channels,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.STATUS_NO_ANSWER,
            )
            self.create_attempt(
                all_channels,
                SOSNotificationAttempt.CHANNEL_VOICE,
                SOSNotificationAttempt.STATUS_DELIVERED,
            )
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(dispatch.call_count, 2)
        retry_call = dispatch.call_args
        self.assertEqual(
            set(retry_call.kwargs['channels']),
            {
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
            },
        )
        self.assertEqual(
            set(retry_call.kwargs['retry_channels']),
            {
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
            },
        )
        self.assertEqual(retry_call.kwargs['max_retries_per_channel'], 1)
        self.assertEqual(state.current_contact_id, all_channels.pk)

    def test_retry_batch_gets_one_new_fixed_acknowledgement_window(self):
        from . import sos_notifications

        initial_time = timezone.now()
        clock = [initial_time]
        deadlines_seen_by_provider = []

        class RetryProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, attempt, event, contact):
                state = SOSEscalationState.objects.get(sos_event=event)
                deadlines_seen_by_provider.append(state.acknowledgement_deadline)
                status = (
                    SOSNotificationAttempt.STATUS_FAILED
                    if attempt.attempt_number == 1
                    else SOSNotificationAttempt.STATUS_ACCEPTED
                )
                return ProviderResult(status=status)

        providers = {
            channel: RetryProvider()
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers), patch(
            'SafetyApp.sos_escalation.timezone.now',
            side_effect=lambda: clock[0],
        ):
            start_sos_escalation(self.event)
            clock[0] += timedelta(seconds=61)
            state = process_escalation_for_event(self.event)

        self.assertEqual(
            deadlines_seen_by_provider,
            [
                initial_time + timedelta(seconds=60),
                initial_time + timedelta(seconds=121),
            ],
        )
        self.assertEqual(
            state.acknowledgement_deadline,
            deadlines_seen_by_provider[1],
        )
        self.assertEqual(state.current_contact_id, self.primary.pk)

    def test_uncertain_attempt_holds_contact_without_retry_or_advance(self):
        from unittest.mock import patch

        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)
            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_UNCERTAIN,
            )
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(state.current_contact_id, self.primary.pk)
        self.assertGreater(state.next_action_at, timezone.now())

    def test_sending_attempt_holds_contact_without_retry_or_advance(self):
        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)
            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_SENDING,
            )
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(state.current_contact_id, self.primary.pk)
        self.assertGreater(state.next_action_at, timezone.now())

    def test_inactive_contact_is_not_skipped_while_attempt_is_uncertain(self):
        from unittest.mock import patch

        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)
            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_UNCERTAIN,
            )
            self.primary.is_active = False
            self.primary.save()
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(state.current_contact_id, self.primary.pk)
        self.assertGreater(state.next_action_at, timezone.now())

    def test_retry_limit_exhausts_before_advancing_contact(self):
        from unittest.mock import patch

        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            start_sos_escalation(self.event)
            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_FAILED,
                number=1,
            )
            self.make_current_contact_due()
            process_escalation_for_event(self.event)
            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_FAILED,
                number=2,
            )
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(state.current_contact_id, self.secondary.pk)
        self.assertEqual(dispatch.call_count, 3)
        self.assertEqual(dispatch.call_args.kwargs['retry_channels'], [])

    def test_exhausted_contacts_produce_exhausted_state(self):
        from unittest.mock import patch

        self.secondary.is_active = False
        self.secondary.save()
        self.third.is_active = False
        self.third.save()
        with patch('SafetyApp.sos_escalation.dispatch_contact_notifications'):
            start_sos_escalation(self.event)
            self.create_attempt(
                self.primary,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_ACCEPTED,
            )
            self.make_current_contact_due()
            state = process_escalation_for_event(self.event)

        self.assertEqual(state.status, SOSEscalationState.STATUS_EXHAUSTED)
        self.assertIsNotNone(state.completed_at)

    def test_cancelled_and_resolved_events_stop_due_escalations(self):
        from unittest.mock import patch

        with patch('SafetyApp.sos_escalation.dispatch_contact_notifications'):
            cancelled_state = start_sos_escalation(self.event)
            cancelled_event = cancelled_state.sos_event
            cancelled_event.mark_cancelled()

            resolved_event = SOSEvent.objects.create(
                user='resolved-user',
                status=SOSEvent.STATUS_ACTIVE,
            )
            resolved_contact = EmergencyContact.objects.create(
                user='resolved-user',
                name='Resolved Contact',
                phone_number='+1 202 555 0190',
                email='resolved@example.org',
                email_consent=True,
                email_verified_at=timezone.now(),
            )
            resolved_state = SOSEscalationState.objects.create(
                sos_event=resolved_event,
                current_contact=resolved_contact,
                current_contact_order=1,
                contact_sequence=json.dumps([resolved_contact.pk]),
                next_action_at=timezone.now(),
            )
            resolved_event.mark_resolved()
            process_due_escalations()

        cancelled_state.refresh_from_db()
        resolved_state.refresh_from_db()
        self.assertEqual(cancelled_state.status, SOSEscalationState.STATUS_CANCELLED)
        self.assertEqual(resolved_state.status, SOSEscalationState.STATUS_RESOLVED)

    def test_acknowledgement_stops_escalation_and_queued_attempts(self):
        with patch('SafetyApp.sos_escalation.dispatch_contact_notifications'):
            state = start_sos_escalation(self.event)
        queued = self.create_attempt(
            self.primary,
            SOSNotificationAttempt.CHANNEL_SMS,
            SOSNotificationAttempt.STATUS_QUEUED,
        )
        token = generate_web_acknowledgement_token(self.event, self.primary)
        token_record = SOSContactAcknowledgement.objects.get(
            sos_event=self.event,
            emergency_contact=self.primary,
        )
        acknowledgement = record_web_acknowledgement(token)
        state.refresh_from_db()
        queued.refresh_from_db()
        token_record.refresh_from_db()

        self.assertIsNotNone(acknowledgement.acknowledged_at)
        self.assertEqual(state.status, SOSEscalationState.STATUS_ACKNOWLEDGED)
        self.assertEqual(queued.status, SOSNotificationAttempt.STATUS_CANCELLED)
        self.assertEqual(token_record.used_at, acknowledgement.acknowledged_at)
        self.assertEqual(token_record.token_expires_at, acknowledgement.acknowledged_at)

    def test_cancelled_event_cannot_continue_dispatching(self):
        with patch(
            'SafetyApp.sos_escalation.dispatch_contact_notifications',
        ) as dispatch:
            state = start_sos_escalation(self.event)
            self.event.mark_cancelled()
            state = process_escalation_for_event(self.event)

        self.assertEqual(state.status, SOSEscalationState.STATUS_CANCELLED)
        self.assertEqual(dispatch.call_count, 1)

    def test_all_channel_attempts_are_prepared_before_provider_submission(self):
        all_channels = self.create_contact(
            name='All Channels',
            phone='+1 202 555 0177',
            email='parallel@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        observed = []
        from . import sos_notifications

        class InspectingProvider(object):
            def __init__(self, channel):
                self.channel = channel

            def send(self, attempt, event, contact):
                state = SOSEscalationState.objects.get(sos_event=event)
                prepared = list(SOSNotificationAttempt.objects.filter(
                    sos_event=event,
                    emergency_contact=contact,
                ))
                observed.append((
                    self.channel,
                    connection.in_atomic_block,
                    state.acknowledgement_deadline,
                    len(prepared),
                ))
                return ProviderResult(status=SOSNotificationAttempt.STATUS_ACCEPTED)

        providers = {
            channel: InspectingProvider(channel)
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers):
            state = start_sos_escalation(self.event)

        self.assertEqual(len(observed), 3)
        self.assertEqual({entry[0] for entry in observed}, set(providers))
        for channel, in_atomic_block, deadline, prepared_count in observed:
            with self.subTest(channel=channel):
                self.assertFalse(in_atomic_block)
                self.assertIsNotNone(deadline)
                self.assertEqual(prepared_count, 3)
        state.refresh_from_db()
        self.assertTrue(all(entry[2] == state.acknowledgement_deadline for entry in observed))
        self.assertGreater(state.acknowledgement_deadline, timezone.now())

    def test_slow_provider_does_not_extend_acknowledgement_deadline(self):
        from . import sos_notifications

        initial_time = timezone.now()
        clock = [initial_time]
        deadlines_seen_by_provider = []

        class SlowProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, attempt, event, contact):
                state = SOSEscalationState.objects.get(sos_event=event)
                deadlines_seen_by_provider.append(state.acknowledgement_deadline)
                clock[0] += timedelta(seconds=90)
                return ProviderResult(status=SOSNotificationAttempt.STATUS_ACCEPTED)

        providers = {
            channel: SlowProvider()
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers), patch(
            'SafetyApp.sos_escalation.timezone.now',
            side_effect=lambda: clock[0],
        ):
            state = start_sos_escalation(self.event)

        self.assertEqual(deadlines_seen_by_provider, [initial_time + timedelta(seconds=60)])
        self.assertEqual(state.acknowledgement_deadline, deadlines_seen_by_provider[0])
        self.assertLess(state.acknowledgement_deadline, clock[0])

    def test_acknowledgement_during_provider_call_blocks_later_channels(self):
        from . import sos_notifications

        all_channels = self.create_contact(
            name='Acknowledging Contact',
            phone='+1 202 555 0144',
            email='ack-in-flight@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        submitted = []

        class AcknowledgingProvider(object):
            def __init__(self, channel):
                self.channel = channel

            def send(self, attempt, event, contact):
                submitted.append(self.channel)
                token = generate_web_acknowledgement_token(event, contact)
                record_web_acknowledgement(token)
                return ProviderResult(status=SOSNotificationAttempt.STATUS_ACCEPTED)

        providers = {
            channel: AcknowledgingProvider(channel)
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers):
            state = start_sos_escalation(self.event)

        state.refresh_from_db()
        attempts = SOSNotificationAttempt.objects.filter(
            sos_event=self.event,
            emergency_contact=all_channels,
        )
        self.assertEqual(submitted, [SOSNotificationAttempt.CHANNEL_EMAIL])
        self.assertEqual(state.status, SOSEscalationState.STATUS_ACKNOWLEDGED)
        self.assertEqual(
            attempts.get(channel=SOSNotificationAttempt.CHANNEL_EMAIL).status,
            SOSNotificationAttempt.STATUS_ACCEPTED,
        )
        self.assertEqual(
            attempts.get(channel=SOSNotificationAttempt.CHANNEL_SMS).status,
            SOSNotificationAttempt.STATUS_CANCELLED,
        )
        self.assertEqual(
            attempts.get(channel=SOSNotificationAttempt.CHANNEL_VOICE).status,
            SOSNotificationAttempt.STATUS_CANCELLED,
        )

    def test_cancellation_during_provider_call_preserves_actual_outcome(self):
        from . import sos_notifications

        all_channels = self.create_contact(
            name='Cancelling Contact',
            phone='+1 202 555 0145',
            email='cancel-in-flight@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        submitted = []

        class CancellingProvider(object):
            def __init__(self, channel):
                self.channel = channel

            def send(self, attempt, event, contact):
                submitted.append(self.channel)
                SOSEvent.objects.filter(pk=event.pk).update(
                    status=SOSEvent.STATUS_CANCELLED,
                )
                return ProviderResult(
                    status=SOSNotificationAttempt.STATUS_DELIVERED,
                    provider_reference='synthetic-in-flight-call',
                )

        providers = {
            channel: CancellingProvider(channel)
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers):
            state = start_sos_escalation(self.event)
            state = process_escalation_for_event(self.event)

        state.refresh_from_db()
        attempts = SOSNotificationAttempt.objects.filter(
            sos_event=self.event,
            emergency_contact=all_channels,
        )
        self.assertEqual(submitted, [SOSNotificationAttempt.CHANNEL_EMAIL])
        self.assertEqual(state.status, SOSEscalationState.STATUS_CANCELLED)
        self.assertEqual(
            attempts.get(channel=SOSNotificationAttempt.CHANNEL_EMAIL).status,
            SOSNotificationAttempt.STATUS_DELIVERED,
        )
        self.assertEqual(
            attempts.get(channel=SOSNotificationAttempt.CHANNEL_SMS).status,
            SOSNotificationAttempt.STATUS_CANCELLED,
        )

    def test_cancellation_after_preparation_prevents_provider_submission(self):
        all_channels = self.create_contact(
            name='All Channels',
            phone='+1 202 555 0166',
            email='cancel-before-send@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        sent = []

        class RecordingProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, attempt, event, contact):
                sent.append(attempt.pk)
                return ProviderResult(status=SOSNotificationAttempt.STATUS_ACCEPTED)

        from . import sos_notifications
        providers = {
            channel: RecordingProvider()
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        for channel, provider in providers.items():
            provider.channel = channel
        def cancel_before_dispatch(*args):
            SOSEvent.objects.filter(pk=self.event.pk).update(
                status=SOSEvent.STATUS_CANCELLED,
            )
            return False

        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers), patch(
            'SafetyApp.sos_escalation._dispatch_guard',
            side_effect=cancel_before_dispatch,
        ):
            start_sos_escalation(self.event)

        self.assertEqual(sent, [])
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(
                sos_event=self.event,
                emergency_contact=all_channels,
                status=SOSNotificationAttempt.STATUS_CANCELLED,
            ).count(),
            3,
        )

    def test_acknowledgement_after_preparation_prevents_provider_submission(self):
        all_channels = self.create_contact(
            name='All Channels',
            phone='+1 202 555 0155',
            email='ack-before-send@example.org',
            is_primary=True,
            channels=('EMAIL', 'SMS', 'VOICE'),
        )
        sent = []

        class RecordingProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, attempt, event, contact):
                sent.append(attempt.pk)
                return ProviderResult(status=SOSNotificationAttempt.STATUS_ACCEPTED)

        from . import sos_notifications
        providers = {
            channel: RecordingProvider()
            for channel in (
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )
        }
        for channel, provider in providers.items():
            provider.channel = channel
        with patch.dict(sos_notifications.DEFAULT_PROVIDERS, providers):
            with patch('SafetyApp.sos_escalation.dispatch_contact_notifications'):
                state = start_sos_escalation(self.event)
            token = generate_web_acknowledgement_token(self.event, all_channels)

            def acknowledge_before_dispatch(*args):
                record_web_acknowledgement(token)
                return False

            with patch(
                'SafetyApp.sos_escalation._dispatch_guard',
                side_effect=acknowledge_before_dispatch,
            ):
                process_escalation_for_event(self.event)

        self.assertEqual(sent, [])
        state.refresh_from_db()
        self.assertEqual(state.status, SOSEscalationState.STATUS_ACKNOWLEDGED)
