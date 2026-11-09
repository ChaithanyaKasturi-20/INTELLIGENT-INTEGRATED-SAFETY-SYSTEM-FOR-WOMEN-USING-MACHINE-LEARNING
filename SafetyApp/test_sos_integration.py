import json
import os
import sqlite3
import smtplib
import tempfile
from datetime import timedelta
from email.parser import Parser
from io import StringIO

from django.conf import settings
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import Client, TransactionTestCase
from django.test import override_settings
from django.utils import timezone
from unittest.mock import MagicMock, patch

from .models import (
    EmergencyContact,
    SafetyTimer,
    SOSEmailDelivery,
    SOSEscalationState,
    SOSEvent,
    SOSContactAcknowledgement,
    SOSNotificationAttempt,
)
from .sos_acknowledgement import (
    generate_web_acknowledgement_token,
    record_web_acknowledgement,
)
from .sos_notifications import (
    ProviderReconciliationResult,
    ProviderResult,
    _claim_attempt,
    _record_result,
    dispatch_contact_notifications,
    dispatch_notification_attempt,
    reconcile_uncertain_attempt,
    recover_interrupted_attempts,
    renew_attempt_lease,
)
from .sos_escalation import (
    acquire_event_lease,
    process_due_escalations,
    process_escalation_for_event,
    release_event_lease,
    renew_event_lease,
    start_sos_escalation,
    stop_sos_escalation,
)
from .views import (
    get_register_db_connection,
    get_sos_delivery_summary,
    resolve_sos_event,
    send_sos_emails_for_event,
)
from .test_utils import isolate_register_test_database


def _ensure_sos_workflow_test_schema():
    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        contact_table = EmergencyContact._meta.db_table
        if contact_table in existing_tables:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT * FROM {}'.format(
                        connection.ops.quote_name(contact_table)
                    )
                )
                existing_columns = {column[0] for column in cursor.description}
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


@patch.dict(
    os.environ,
    {
        'SOS_SMTP_EMAIL_ENABLED': 'false',
        'SOS_MOCK_EMAIL_ENABLED': 'true',
    },
    clear=False,
)
class SOSEscalationIntegrationTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super(SOSEscalationIntegrationTests, cls).setUpClass()
        _ensure_sos_workflow_test_schema()

    def setUp(self):
        isolate_register_test_database(self)
        self.username = 'phase-four-user'
        self.client = Client()
        session = self.client.session
        session['username'] = self.username
        session['is_logged_in'] = True
        session.save()

    def create_contact(self, name, primary=False, channels=('EMAIL', 'SMS', 'VOICE')):
        now = timezone.now()
        return EmergencyContact.objects.create(
            user=self.username,
            name=name,
            phone_number='+1 202 555 01{}'.format(10 + EmergencyContact.objects.count()),
            email='{}@example.org'.format(name.lower().replace(' ', '.')),
            is_active=True,
            is_primary=primary,
            email_consent='EMAIL' in channels,
            sms_consent='SMS' in channels,
            voice_consent='VOICE' in channels,
            email_verified_at=now if 'EMAIL' in channels else None,
            phone_verified_at=now if 'SMS' in channels or 'VOICE' in channels else None,
        )

    def activate(self):
        response = self.client.post('/ActivateSOS', {'latitude': '12.3', 'longitude': '45.6'})
        self.assertEqual(response.status_code, 302)
        return SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)

    def smtp_environment(self, **extra):
        environment = {
            'SOS_SMTP_EMAIL_ENABLED': 'true',
            'SOS_MOCK_EMAIL_ENABLED': 'false',
            'PANIC_EMAIL_USER': 'sender@example.org',
            'PANIC_EMAIL_PASS': 'test-only-password',
        }
        environment.update(extra)
        return environment

    def test_smtp_dispatch_is_independent_of_exotel_and_public_url(self):
        contact = self.create_contact('Primary', primary=True, channels=('EMAIL',))
        location_time = timezone.now() - timedelta(minutes=10)
        event = SOSEvent.objects.create(
            user=self.username,
            latitude=17.503103491293437,
            longitude=78.42720027134641,
            location_updated_at=location_time,
            status=SOSEvent.STATUS_ACTIVE,
        )
        smtp = MagicMock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp
        smtp.sendmail.return_value = {}

        with patch.dict(
            os.environ,
            self.smtp_environment(SOS_PUBLIC_BASE_URL='http://127.0.0.1:8000/private'),
            clear=True,
        ), patch(
            'SafetyApp.sos_notifications.smtplib.SMTP_SSL',
            return_value=smtp_context,
        ) as smtp_ssl:
            start_sos_escalation(event)
            start_sos_escalation(event)

        smtp_ssl.assert_called_once_with(
            'smtp.gmail.com',
            465,
            timeout=20,
        )
        smtp.login.assert_called_once_with(
            'sender@example.org',
            'test-only-password',
        )
        smtp.sendmail.assert_called_once()
        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_ACCEPTED)

        message = Parser().parsestr(smtp.sendmail.call_args[0][2])
        body = message.get_payload(decode=True).decode('utf-8')
        self.assertIn('17.503103491293437, 78.42720027134641', body)
        self.assertIn('https://www.google.com/maps?q=17.503103491293437,78.42720027134641', body)
        self.assertIn(
            timezone.localtime(location_time).strftime('%Y-%m-%d %H:%M:%S %Z'),
            body,
        )
        self.assertIn('may be stale', body)
        self.assertNotIn('127.0.0.1', body)
        self.assertNotIn('Live location sharing link', body)
        summary = get_sos_delivery_summary(event)
        self.assertEqual(summary['email_accepted'], 1)
        self.assertEqual(summary['email_delivered'], 0)
        self.assertEqual(summary['email_unattempted'], 0)

    def test_missing_smtp_configuration_is_recorded_as_failed(self):
        contact = self.create_contact('Primary', primary=True, channels=('EMAIL',))
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )

        with patch.dict(
            os.environ,
            {
                'SOS_SMTP_EMAIL_ENABLED': 'true',
                'SOS_MOCK_EMAIL_ENABLED': 'false',
            },
            clear=True,
        ), patch(
            'SafetyApp.views.get_panic_email_config',
            return_value=('', ''),
        ), patch(
            'SafetyApp.sos_notifications.smtplib.SMTP_SSL'
        ) as smtp_ssl:
            start_sos_escalation(event)

        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_FAILED)
        self.assertEqual(attempt.error_code, 'SMTP_NOT_CONFIGURED')
        smtp_ssl.assert_not_called()

    def test_incomplete_exotel_configuration_does_not_block_email(self):
        contact = self.create_contact('Primary', primary=True, channels=('EMAIL', 'SMS'))
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )
        smtp = MagicMock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp
        smtp.sendmail.return_value = {}

        with patch.dict(
            os.environ,
            self.smtp_environment(
                SOS_EXOTEL_SMS_ENABLED='true',
                SOS_MOCK_SMS_ENABLED='false',
            ),
            clear=True,
        ), patch(
            'SafetyApp.sos_notifications.smtplib.SMTP_SSL',
            return_value=smtp_context,
        ) as smtp_ssl, patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen'
        ) as exotel_request:
            start_sos_escalation(event)

        email_attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        sms_attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_SMS,
        )
        self.assertEqual(email_attempt.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertEqual(sms_attempt.status, SOSNotificationAttempt.STATUS_FAILED)
        self.assertEqual(sms_attempt.error_code, 'PROVIDER_CONFIGURATION_INVALID')
        smtp_ssl.assert_called_once()
        exotel_request.assert_not_called()

    def test_smtp_authentication_failure_is_recorded_without_secret_details(self):
        contact = self.create_contact('Primary', primary=True, channels=('EMAIL',))
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )
        smtp = MagicMock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(
            535,
            b'authentication failed',
        )

        with patch.dict(os.environ, self.smtp_environment(), clear=True), patch(
            'SafetyApp.sos_notifications.smtplib.SMTP_SSL',
            return_value=smtp_context,
        ):
            start_sos_escalation(event)

        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_FAILED)
        self.assertEqual(attempt.error_code, 'SMTP_AUTHENTICATION_FAILED')
        self.assertNotIn('credentials must not be stored', attempt.error_message)

    def test_smtp_connection_failure_is_recorded(self):
        contact = self.create_contact('Primary', primary=True, channels=('EMAIL',))
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )

        with patch.dict(os.environ, self.smtp_environment(), clear=True), patch(
            'SafetyApp.sos_notifications.smtplib.SMTP_SSL',
            side_effect=ConnectionRefusedError('connection refused'),
        ):
            start_sos_escalation(event)

        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_FAILED)
        self.assertEqual(attempt.error_code, 'ConnectionRefusedError')
        self.assertIn('SMTP server', attempt.error_message)

    def test_smtp_timeout_after_submission_begins_is_uncertain(self):
        contact = self.create_contact('Primary', primary=True, channels=('EMAIL',))
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )
        smtp = MagicMock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp
        smtp.sendmail.side_effect = TimeoutError('submission timed out')

        with patch.dict(os.environ, self.smtp_environment(), clear=True), patch(
            'SafetyApp.sos_notifications.smtplib.SMTP_SSL',
            return_value=smtp_context,
        ):
            start_sos_escalation(event)

        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(attempt.error_code, 'TimeoutError')
        self.assertEqual(get_sos_delivery_summary(event)['email_uncertain'], 1)

    def test_contact_without_email_is_not_attempted(self):
        contact = self.create_contact('No Email', channels=())
        contact.email = ''
        contact.save(update_fields=('email',))
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )

        outcomes = dispatch_contact_notifications(
            event,
            contact,
            channels=(SOSNotificationAttempt.CHANNEL_EMAIL,),
        )

        self.assertEqual(outcomes, [])
        self.assertFalse(
            SOSNotificationAttempt.objects.filter(
                sos_event=event,
                emergency_contact=contact,
                channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            ).exists()
        )
        self.assertEqual(get_sos_delivery_summary(event)['no_email'], 1)

    def test_activation_snapshots_primary_first_and_repeated_activation_is_idempotent(self):
        secondary = self.create_contact('Secondary')
        primary = self.create_contact('Primary', primary=True)

        with patch('SafetyApp.views.send_sos_emails_for_event') as legacy_sender, patch(
            'SafetyApp.views.smtplib.SMTP_SSL'
        ) as smtp:
            event = self.activate()
            legacy_sender.assert_not_called()
            smtp.assert_not_called()
        state = SOSEscalationState.objects.get(sos_event=event)
        first_attempt_count = SOSNotificationAttempt.objects.filter(sos_event=event).count()

        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=event).exists())
        self.assertEqual(json.loads(state.contact_sequence), [primary.pk, secondary.pk])
        self.assertEqual(state.current_contact_id, primary.pk)
        self.assertEqual(
            set(SOSNotificationAttempt.objects.filter(
                sos_event=event,
                emergency_contact=primary,
            ).values_list('channel', flat=True)),
            {
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.CHANNEL_VOICE,
            },
        )

        self.activate()

        self.assertEqual(SOSEvent.objects.filter(user=self.username).count(), 1)
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(sos_event=event).count(),
            first_attempt_count,
        )

    def test_contacts_without_eligible_channels_are_excluded(self):
        contact = self.create_contact('No Consent', channels=())

        event = self.activate()
        state = SOSEscalationState.objects.get(sos_event=event)

        self.assertEqual(json.loads(state.contact_sequence), [])
        self.assertEqual(state.status, SOSEscalationState.STATUS_EXHAUSTED)
        self.assertFalse(
            SOSNotificationAttempt.objects.filter(
                sos_event=event,
                emergency_contact=contact,
            ).exists()
        )

    def test_cancellation_stops_escalation_and_preserves_completed_attempts(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        accepted = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        queued = SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_SMS,
            attempt_number=2,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )

        response = self.client.post('/UpdateSOSStatus', {'status': 'CANCELLED'})

        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        accepted.refresh_from_db()
        queued.refresh_from_db()
        state = SOSEscalationState.objects.get(sos_event=event)
        self.assertEqual(event.status, SOSEvent.STATUS_CANCELLED)
        self.assertEqual(state.status, SOSEscalationState.STATUS_CANCELLED)
        self.assertEqual(accepted.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertEqual(queued.status, SOSNotificationAttempt.STATUS_CANCELLED)

        self.client.post('/UpdateSOSStatus', {'status': 'CANCELLED'})
        event.refresh_from_db()
        self.assertEqual(event.status, SOSEvent.STATUS_CANCELLED)

    def test_resolution_stops_escalation_idempotently(self):
        event = self.activate()

        resolve_sos_event(event)
        resolve_sos_event(event)

        event.refresh_from_db()
        self.assertEqual(event.status, SOSEvent.STATUS_RESOLVED)
        self.assertEqual(
            SOSEscalationState.objects.get(sos_event=event).status,
            SOSEscalationState.STATUS_RESOLVED,
        )

    def test_expired_timer_page_does_not_create_an_unconfirmed_sos(self):
        contact = self.create_contact('Primary', primary=True)
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=5,
            started_at=timezone.now() - timedelta(minutes=7),
            expires_at=timezone.now() - timedelta(minutes=2),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        with patch('SafetyApp.views.send_sos_emails_for_event') as legacy_sender:
            response = self.client.get('/SafetyTimer')
            legacy_sender.assert_not_called()

        self.assertEqual(response.status_code, 200)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_EXPIRED)
        self.assertFalse(
            SOSEvent.objects.filter(
                user=self.username,
                status=SOSEvent.STATUS_ACTIVE,
            ).exists()
        )
        self.assertFalse(SOSEscalationState.objects.exists())
        self.assertFalse(SOSEmailDelivery.objects.exists())
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(emergency_contact=contact).count(),
            0,
        )

    def test_legacy_retry_route_does_not_dispatch_smtp(self):
        self.create_contact('Primary', primary=True)
        event = self.activate()

        with patch('SafetyApp.views.smtplib.SMTP_SSL') as smtp, patch(
            'SafetyApp.views.send_sos_emails_for_event'
        ) as legacy_sender:
            response = self.client.post('/SOS/RetryEmail')

        self.assertEqual(response.status_code, 302)
        legacy_sender.assert_not_called()
        smtp.assert_not_called()
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(sos_event=event).count(),
            3,
        )

    def test_panic_action_and_legacy_sender_cannot_send_smtp(self):
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )

        with patch('SafetyApp.views.smtplib.SMTP_SSL') as smtp:
            response = self.client.post('/PanicAction', {'t1': 'synthetic location'})
            self.assertFalse(send_sos_emails_for_event(event))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'legacy email alert action is disabled',
            response.context['data'].lower(),
        )
        smtp.assert_not_called()
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=event).exists())

    def test_register_database_connection_uses_test_only_sqlite_file(self):
        development_database_path = os.path.normcase(os.path.realpath(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db.sqlite3')
        ))
        register_connection = get_register_db_connection()
        try:
            actual_test_path = register_connection.execute(
                'PRAGMA database_list'
            ).fetchone()['file']
        finally:
            register_connection.close()

        self.assertTrue(actual_test_path)
        self.assertNotEqual(
            os.path.normcase(os.path.realpath(actual_test_path)),
            development_database_path,
        )
        self.assertEqual(
            os.path.dirname(actual_test_path),
            self.register_test_base_dir,
        )
        self.assertEqual(os.path.basename(actual_test_path), 'register-test.sqlite3')

    def test_test_register_connection_refuses_missing_isolation_directory(self):
        with override_settings(REGISTER_TEST_DB_DIR=None):
            with self.assertRaises(RuntimeError):
                get_register_db_connection()

    def test_isolation_directory_prevents_fallback_when_connection_name_is_development_path(self):
        development_database_path = os.path.join(settings.BASE_DIR, 'db.sqlite3')
        with patch.dict(
            connection.settings_dict,
            {'NAME': development_database_path, 'TEST': {}},
            clear=False,
        ):
            register_connection = get_register_db_connection()
            try:
                isolated_path = register_connection.execute(
                    'PRAGMA database_list'
                ).fetchone()['file']
            finally:
                register_connection.close()

        self.assertNotEqual(
            os.path.normcase(os.path.realpath(isolated_path)),
            os.path.normcase(os.path.realpath(development_database_path)),
        )
        self.assertEqual(os.path.dirname(isolated_path), self.register_test_base_dir)

    def test_register_records_are_isolated_between_test_directories(self):
        first_connection = get_register_db_connection()
        try:
            first_connection.execute(
                'CREATE TABLE register (username TEXT PRIMARY KEY, password TEXT, '
                'contact_no TEXT, email TEXT, address TEXT)'
            )
            first_connection.execute(
                'INSERT INTO register (username) VALUES (?)',
                ('first-test-user',),
            )
            first_connection.commit()
        finally:
            first_connection.close()

        with tempfile.TemporaryDirectory(prefix='safety-register-second-test-') as second_base_dir:
            with override_settings(REGISTER_TEST_DB_DIR=second_base_dir):
                second_connection = get_register_db_connection()
                try:
                    second_connection.execute(
                        'CREATE TABLE register (username TEXT PRIMARY KEY, password TEXT, '
                        'contact_no TEXT, email TEXT, address TEXT)'
                    )
                    self.assertIsNone(second_connection.execute(
                        'SELECT username FROM register WHERE username = ?',
                        ('first-test-user',),
                    ).fetchone())
                    second_path = second_connection.execute(
                        'PRAGMA database_list'
                    ).fetchone()['file']
                finally:
                    second_connection.close()

        first_path = os.path.join(
            self.register_test_base_dir,
            'register-test.sqlite3',
        )
        self.assertNotEqual(
            os.path.normcase(os.path.realpath(first_path)),
            os.path.normcase(os.path.realpath(second_path)),
        )

    def test_register_temporary_directory_cleanup_removes_test_database(self):
        temporary_path = self.register_test_base_dir
        self.assertTrue(os.path.isdir(temporary_path))
        self.register_test_directory.cleanup()
        self.assertFalse(os.path.exists(temporary_path))

    def test_register_connection_context_closes_the_sqlite_handle(self):
        register_connection = get_register_db_connection()
        with register_connection as isolated_connection:
            isolated_connection.execute('CREATE TABLE connection_close_check (id INTEGER)')

        with self.assertRaises(sqlite3.ProgrammingError):
            register_connection.execute('SELECT 1')

    def test_non_test_register_connections_keep_production_path_behavior(self):
        with override_settings(REGISTER_TEST_DB_DIR=None), patch.dict(
            connection.settings_dict,
            {'NAME': 'production.sqlite3', 'TEST': {}},
            clear=False,
        ), patch('SafetyApp.views.sqlite3.connect') as mock_connect:
            register_connection = get_register_db_connection()
            register_connection.close()

        mock_connect.assert_called_once()
        self.assertEqual(
            os.path.normcase(os.path.realpath(mock_connect.call_args[0][0])),
            os.path.normcase(os.path.realpath(
                os.path.join(settings.BASE_DIR, 'db.sqlite3')
            )),
        )

    def test_dashboard_summary_with_no_attempts(self):
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )
        contact = self.create_contact('No Email', channels=())
        contact.email = ''
        contact.save(update_fields=('email',))

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['source'], 'none')
        self.assertFalse(summary['legacy_only'])
        self.assertEqual(summary['workflow_attempt_count'], 0)
        self.assertEqual(summary['sent'], 0)
        self.assertEqual(summary['accepted'], 0)
        self.assertEqual(summary['no_email'], 1)

    def test_dashboard_summary_separates_ineligible_from_unattempted_contacts(self):
        self.create_contact('Consent Required', channels=())
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['eligible_contacts'], 0)
        self.assertEqual(summary['ineligible_email_contacts'], 1)
        self.assertEqual(summary['email_unattempted'], 0)

    def test_dashboard_summary_counts_queued_and_sending_attempts(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        queued_contact = self.create_contact('Queued Contact')
        sending_contact = self.create_contact('Sending Contact')
        for contact, channel, status in (
            (
                queued_contact,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.STATUS_QUEUED,
            ),
            (
                sending_contact,
                SOSNotificationAttempt.CHANNEL_SMS,
                SOSNotificationAttempt.STATUS_SENDING,
            ),
        ):
            SOSNotificationAttempt.objects.create(
                sos_event=event,
                emergency_contact=contact,
                channel=channel,
                status=status,
                attempt_number=1,
            )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['queued'], 1)
        self.assertEqual(summary['sending'], 1)
        self.assertEqual(summary['pending'], 2)

    def test_dashboard_summary_uses_latest_attempt_per_contact_and_channel(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        contact = self.create_contact('Summary Contact')
        SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            status=SOSNotificationAttempt.STATUS_QUEUED,
            attempt_number=1,
        )
        SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            attempt_number=2,
        )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['source'], 'workflow')
        self.assertEqual(summary['workflow_attempt_count'], 1)
        self.assertEqual(summary['queued'], 0)
        self.assertEqual(summary['accepted'], 1)
        self.assertEqual(summary['sent'], 0)
        self.assertEqual(summary['unattempted'], 0)

    def test_dashboard_summary_distinguishes_accepted_and_delivered(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        accepted_contact = self.create_contact('Accepted Contact')
        delivered_contact = self.create_contact('Delivered Contact')
        for contact, status in (
            (accepted_contact, SOSNotificationAttempt.STATUS_ACCEPTED),
            (delivered_contact, SOSNotificationAttempt.STATUS_DELIVERED),
        ):
            SOSNotificationAttempt.objects.create(
                sos_event=event,
                emergency_contact=contact,
                channel=SOSNotificationAttempt.CHANNEL_EMAIL,
                status=status,
                attempt_number=1,
            )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['accepted'], 1)
        self.assertEqual(summary['delivered'], 1)
        self.assertEqual(summary['sent'], 1)

    def test_dashboard_summary_separates_failed_and_uncertain(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        failed_contact = self.create_contact('Private Failed Name')
        uncertain_contact = self.create_contact('Private Uncertain Name')
        for contact, status in (
            (failed_contact, SOSNotificationAttempt.STATUS_FAILED),
            (uncertain_contact, SOSNotificationAttempt.STATUS_UNCERTAIN),
        ):
            SOSNotificationAttempt.objects.create(
                sos_event=event,
                emergency_contact=contact,
                channel=SOSNotificationAttempt.CHANNEL_EMAIL,
                status=status,
                attempt_number=1,
                error_message='synthetic-provider-secret',
            )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['uncertain'], 1)
        self.assertEqual(len(summary['failures']), 1)
        self.assertEqual(len(summary['uncertain_outcomes']), 1)
        self.assertIn('uncertain', summary['uncertain_outcomes'][0]['reason'])
        self.assertNotIn('Private Failed Name', repr(summary))
        self.assertNotIn('synthetic-provider-secret', repr(summary))

    def test_dashboard_summary_distinguishes_acknowledged_and_cancelled(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        for name, status in (
            ('Acknowledged Contact', SOSNotificationAttempt.STATUS_ACKNOWLEDGED),
            ('Cancelled Contact', SOSNotificationAttempt.STATUS_CANCELLED),
        ):
            contact = self.create_contact(name)
            SOSNotificationAttempt.objects.create(
                sos_event=event,
                emergency_contact=contact,
                channel=SOSNotificationAttempt.CHANNEL_SMS,
                status=status,
                attempt_number=1,
            )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['acknowledged'], 1)
        self.assertEqual(summary['cancelled'], 1)

    def test_dashboard_summary_marks_legacy_only_events(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        for name, status in (
            ('Legacy Accepted', SOSEmailDelivery.STATUS_SENT),
            ('Legacy Failed', SOSEmailDelivery.STATUS_FAILED),
            ('Legacy Uncertain', SOSEmailDelivery.STATUS_UNCERTAIN),
        ):
            contact = self.create_contact(name)
            SOSEmailDelivery.objects.create(
                sos_event=event,
                emergency_contact=contact,
                status=status,
                error_message='synthetic-private-provider-error',
            )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['source'], 'legacy')
        self.assertTrue(summary['legacy_only'])
        self.assertEqual(summary['legacy_records'], 3)
        self.assertEqual(summary['legacy_accepted'], 1)
        self.assertEqual(summary['legacy_failed'], 1)
        self.assertEqual(summary['legacy_uncertain'], 1)
        self.assertEqual(summary['legacy_failures'][0]['reason'], 'The legacy email submission failed.')
        self.assertEqual(summary['sent'], 0)
        self.assertEqual(summary['failed'], 0)
        self.assertEqual(summary['uncertain'], 0)
        self.assertEqual(summary['pending'], 0)
        self.assertEqual(summary['failures'], [])
        self.assertNotIn('Legacy Accepted', repr(summary))
        self.assertNotIn('synthetic-private-provider-error', repr(summary))

    def test_dashboard_summary_prefers_workflow_over_legacy_records(self):
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        contact = self.create_contact('Mixed Source Contact')
        SOSEmailDelivery.objects.create(
            sos_event=event,
            emergency_contact=contact,
            status=SOSEmailDelivery.STATUS_SENT,
        )
        SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            attempt_number=1,
        )

        summary = get_sos_delivery_summary(event)

        self.assertEqual(summary['source'], 'workflow')
        self.assertFalse(summary['legacy_only'])
        self.assertEqual(summary['legacy_records'], 1)
        self.assertEqual(summary['legacy_accepted'], 1)
        self.assertEqual(summary['accepted'], 1)
        self.assertEqual(summary['sent'], 0)

    def test_recovery_marks_only_stale_attempts_uncertain_and_is_idempotent(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        now = timezone.now()
        stale = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        stale.status = SOSNotificationAttempt.STATUS_SENDING
        stale.started_at = now - timedelta(minutes=10)
        stale.save(update_fields=('status', 'started_at'))
        fresh = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_SMS,
        )
        fresh.status = SOSNotificationAttempt.STATUS_SENDING
        fresh.started_at = now
        fresh.save(update_fields=('status', 'started_at'))

        self.assertEqual(recover_interrupted_attempts(), 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(stale.error_code, 'WORKER_INTERRUPTED')
        self.assertEqual(fresh.status, SOSNotificationAttempt.STATUS_SENDING)
        self.assertEqual(recover_interrupted_attempts(), 0)

    def test_uncertain_attempt_is_not_retried_by_subsequent_escalation_processing(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        state = SOSEscalationState.objects.get(sos_event=event)
        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        attempt.status = SOSNotificationAttempt.STATUS_SENDING
        attempt.started_at = timezone.now() - timedelta(minutes=10)
        attempt.save(update_fields=('status', 'started_at'))
        self.assertEqual(recover_interrupted_attempts(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_UNCERTAIN)

        state.acknowledgement_deadline = timezone.now() - timedelta(seconds=1)
        state.next_action_at = timezone.now() - timedelta(seconds=1)
        state.save(update_fields=('acknowledgement_deadline', 'next_action_at'))
        count_before = SOSNotificationAttempt.objects.filter(sos_event=event).count()
        start_sos_escalation(event)
        state.refresh_from_db()
        attempt.refresh_from_db()

        self.assertEqual(
            SOSNotificationAttempt.objects.filter(sos_event=event).count(),
            count_before,
        )
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(state.current_contact_id, contact.pk)
        self.assertEqual(state.status, SOSEscalationState.STATUS_RUNNING)

    def test_recovery_skips_an_attempt_while_its_provider_call_is_active(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        attempt = SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            attempt_number=2,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )

        class SlowSyntheticProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, claimed_attempt, _event, _contact):
                SOSNotificationAttempt.objects.filter(pk=claimed_attempt.pk).update(
                    started_at=timezone.now() - timedelta(minutes=10)
                )
                self.recovered_count = recover_interrupted_attempts()
                claimed_attempt.refresh_from_db()
                self.status_during_call = claimed_attempt.status
                return ProviderResult(status=SOSNotificationAttempt.STATUS_ACCEPTED)

        provider = SlowSyntheticProvider()
        dispatch_notification_attempt(event, contact, attempt.channel, provider=provider, attempt=attempt)

        self.assertEqual(provider.recovered_count, 0)
        self.assertEqual(provider.status_during_call, SOSNotificationAttempt.STATUS_SENDING)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_ACCEPTED)

    def test_event_lease_is_exclusive_renewable_and_fenced_after_expiry(self):
        self.create_contact('Primary', primary=True)
        event = self.activate()
        state = SOSEscalationState.objects.get(sos_event=event)

        first = acquire_event_lease(event.pk, owner='worker-one')
        self.assertIsNotNone(first)
        self.assertIsNone(acquire_event_lease(event.pk, owner='worker-two'))
        self.assertTrue(renew_event_lease(event.pk, first))

        SOSEscalationState.objects.filter(pk=state.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        second = acquire_event_lease(event.pk, owner='worker-two')
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)
        self.assertFalse(renew_event_lease(event.pk, first))
        self.assertFalse(release_event_lease(event.pk, first))
        self.assertTrue(release_event_lease(event.pk, second))

    def test_attempt_claim_is_exclusive_and_expired_result_is_fenced(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        attempt = SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            attempt_number=2,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )

        first_claim = _claim_attempt(attempt.pk)
        self.assertIsNotNone(first_claim)
        self.assertIsNone(_claim_attempt(attempt.pk))
        self.assertTrue(
            renew_attempt_lease(
                attempt.pk,
                first_claim.claim_token,
                first_claim.claim_generation,
            )
        )

        SOSNotificationAttempt.objects.filter(pk=attempt.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(recover_interrupted_attempts(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertGreater(attempt.claim_generation, first_claim.claim_generation)
        self.assertFalse(renew_attempt_lease(
            attempt.pk,
            first_claim.claim_token,
            first_claim.claim_generation,
        ))
        self.assertFalse(_record_result(
            first_claim,
            ProviderResult(
                status=SOSNotificationAttempt.STATUS_DELIVERED,
                provider_reference='synthetic-late-result',
            ),
        ))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(attempt.provider_reference, '')

    def test_recovery_leaves_a_fresh_attempt_lease_untouched(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        attempt = SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            attempt_number=2,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )
        claimed = _claim_attempt(attempt.pk)

        self.assertEqual(recover_interrupted_attempts(), 0)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_SENDING)
        self.assertEqual(attempt.claim_token, claimed.claim_token)

    def test_reconciliation_resolves_only_confirmed_outcomes(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()

        class SyntheticReconciler(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def __init__(self, outcome):
                self.outcome = outcome

            def reconcile(self, _attempt):
                return ProviderReconciliationResult(outcome=self.outcome)

        outcomes = (
            ('DELIVERED', SOSNotificationAttempt.STATUS_DELIVERED),
            ('ACCEPTED', SOSNotificationAttempt.STATUS_ACCEPTED),
            ('NOT_DELIVERED', SOSNotificationAttempt.STATUS_FAILED),
            ('UNKNOWN', SOSNotificationAttempt.STATUS_UNCERTAIN),
        )
        for index, (outcome, expected_status) in enumerate(outcomes, 2):
            with self.subTest(outcome=outcome):
                attempt = SOSNotificationAttempt.objects.create(
                    sos_event=event,
                    emergency_contact=contact,
                    channel=SOSNotificationAttempt.CHANNEL_EMAIL,
                    attempt_number=index,
                    status=SOSNotificationAttempt.STATUS_UNCERTAIN,
                )
                resolved = reconcile_uncertain_attempt(
                    attempt.pk,
                    provider=SyntheticReconciler(outcome),
                )
                self.assertEqual(resolved.status, expected_status)
                self.assertIsNone(resolved.claim_token)
                self.assertIsNone(resolved.lease_expires_at)

    def test_acknowledgement_invalidates_active_event_lease(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        lease = acquire_event_lease(event.pk, owner='acknowledgement-race')
        token = generate_web_acknowledgement_token(event, contact)

        record_web_acknowledgement(token)

        state = SOSEscalationState.objects.get(sos_event=event)
        self.assertEqual(state.status, SOSEscalationState.STATUS_ACKNOWLEDGED)
        self.assertFalse(renew_event_lease(event.pk, lease))
        self.assertFalse(release_event_lease(event.pk, lease))
        attempts_before = SOSNotificationAttempt.objects.filter(sos_event=event).count()
        process_escalation_for_event(event)
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(sos_event=event).count(),
            attempts_before,
        )

    def test_cancellation_during_provider_call_keeps_workflow_terminal(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        attempt = SOSNotificationAttempt.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
            attempt_number=2,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )

        class CancellingProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, _attempt, _event, _contact):
                SOSEvent.objects.filter(pk=event.pk).update(
                    status=SOSEvent.STATUS_CANCELLED,
                )
                stop_sos_escalation(event, SOSEvent.STATUS_CANCELLED)
                return ProviderResult(
                    status=SOSNotificationAttempt.STATUS_ACCEPTED,
                    provider_reference='synthetic-accepted-before-cancel',
                )

        result = dispatch_notification_attempt(
            event,
            contact,
            attempt.channel,
            provider=CancellingProvider(),
            attempt=attempt,
        )

        self.assertEqual(result.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertEqual(
            SOSEscalationState.objects.get(sos_event=event).status,
            SOSEscalationState.STATUS_CANCELLED,
        )
        self.assertEqual(
            SOSEvent.objects.get(pk=event.pk).status,
            SOSEvent.STATUS_CANCELLED,
        )

    def test_scheduler_skips_event_owned_by_another_worker(self):
        self.create_contact('Primary', primary=True)
        event = self.activate()
        state = SOSEscalationState.objects.get(sos_event=event)
        due = timezone.now() - timedelta(seconds=1)
        SOSEscalationState.objects.filter(pk=state.pk).update(
            acknowledgement_deadline=due,
            next_action_at=due,
        )
        lease = acquire_event_lease(event.pk, owner='first-scheduler')
        attempts_before = SOSNotificationAttempt.objects.filter(sos_event=event).count()

        self.assertEqual(process_due_escalations(), 0)

        self.assertEqual(
            SOSNotificationAttempt.objects.filter(sos_event=event).count(),
            attempts_before,
        )
        self.assertTrue(release_event_lease(event.pk, lease))

    def test_acknowledgement_stops_further_escalation(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        token = generate_web_acknowledgement_token(event, contact)

        acknowledgement = record_web_acknowledgement(token)

        self.assertEqual(acknowledgement.sos_event_id, event.pk)
        self.assertEqual(
            SOSEscalationState.objects.get(sos_event=event).status,
            SOSEscalationState.STATUS_ACKNOWLEDGED,
        )
        self.assertIsNotNone(acknowledgement.used_at)

    def test_management_command_processes_due_events_without_duplicate_attempts(self):
        contact = self.create_contact('Primary', primary=True)
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        state = start_sos_escalation(event)
        state.acknowledgement_deadline = timezone.now() - timedelta(seconds=1)
        state.next_action_at = timezone.now() - timedelta(seconds=1)
        state.save(update_fields=('acknowledgement_deadline', 'next_action_at'))
        initial_attempt_count = SOSNotificationAttempt.objects.filter(sos_event=event).count()

        output = StringIO()
        call_command('process_sos_escalations', stdout=output)
        after_first_run = SOSNotificationAttempt.objects.filter(sos_event=event).count()
        call_command('process_sos_escalations', stdout=StringIO())
        after_second_run = SOSNotificationAttempt.objects.filter(sos_event=event).count()

        self.assertIn('SOS escalation events processed:', output.getvalue())
        self.assertEqual(initial_attempt_count, 3)
        self.assertEqual(after_first_run, after_second_run)
        self.assertEqual(
            SOSEscalationState.objects.get(sos_event=event).status,
            SOSEscalationState.STATUS_EXHAUSTED,
        )

    def test_management_command_recovers_and_reports_stale_attempts(self):
        contact = self.create_contact('Primary', primary=True)
        event = self.activate()
        attempt = SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        attempt.status = SOSNotificationAttempt.STATUS_SENDING
        attempt.started_at = timezone.now() - timedelta(minutes=10)
        attempt.save(update_fields=('status', 'started_at'))

        output = StringIO()
        call_command('process_sos_escalations', stdout=output)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertIn('Stale SENDING attempts marked UNCERTAIN: 1', output.getvalue())

    def test_management_command_reports_event_failures_and_exits_nonzero(self):
        def fail_one_event(on_error):
            on_error(987, RuntimeError('synthetic failure'))
            return 0

        with patch(
            'SafetyApp.management.commands.process_sos_escalations.process_due_escalations',
            side_effect=fail_one_event,
        ):
            with self.assertRaises(CommandError):
                call_command('process_sos_escalations', stdout=StringIO(), stderr=StringIO())
