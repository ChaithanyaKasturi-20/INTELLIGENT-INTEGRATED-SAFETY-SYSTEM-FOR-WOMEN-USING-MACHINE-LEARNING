import os
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase, Client
from django.utils import timezone

from .models import (
    EmergencyContact,
    SOSEvent,
    SOSEmailDelivery,
    SOSEscalationState,
    SOSNotificationAttempt,
)
from .test_utils import isolate_register_test_database
from .views import ensure_register_table, get_register_db_connection
from .sos_notifications import ProviderResult
from .sos_escalation import process_due_escalations


class EmergencyContactTestCase(TestCase):
    def setUp(self):
        isolate_register_test_database(self)
        self.client = Client()
        self.username = 'alice'
        self.email = 'alice@gmail.com'

        session = self.client.session
        session['username'] = self.username
        session['email'] = self.email
        session['is_logged_in'] = True
        session.save()

    def test_add_contact(self):
        response = self.client.post('/EmergencyContactAdd', {
            'name': 'Mom',
            'phone_number': '+91 9876543210',
            'email': 'mom@safealarm.mail',
            'relationship': 'Mother',
            'is_primary': 'on',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EmergencyContact.objects.filter(user=self.username, name='Mom').exists())

    def test_primary_contact_behavior(self):
        first = EmergencyContact.objects.create(user=self.username, name='A', phone_number='+91 1111111111', is_primary=True, is_active=True)
        second = EmergencyContact.objects.create(user=self.username, name='B', phone_number='+91 2222222222', email='b@safealarm.mail', is_primary=False, is_active=True)

        second.is_primary = True
        second.save()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_primary)
        self.assertTrue(second.is_primary)

    def test_toggle_active(self):
        contact = EmergencyContact.objects.create(user=self.username, name='Guardian', phone_number='+91 5555555555', is_active=True)
        self.client.get(f'/EmergencyContactToggleActive/{contact.pk}')
        contact.refresh_from_db()
        self.assertFalse(contact.is_active)

    def test_phone_validation(self):
        contact = EmergencyContact(user=self.username, name='Test', phone_number='invalid', email='test@safealarm.mail')
        with self.assertRaises(ValidationError):
            contact.full_clean()

    def test_email_validation(self):
        contact = EmergencyContact(user=self.username, name='Test', phone_number='+91 1234567890', email='not-valid-mail')
        with self.assertRaises(ValidationError):
            contact.full_clean()

    def test_changing_email_revokes_email_consent_and_verification(self):
        verified_at = timezone.now()
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Trusted Contact',
            phone_number='+1 202 555 0100',
            email='trusted@example.org',
            email_consent=True,
            sms_consent=True,
            voice_consent=True,
            email_verified_at=verified_at,
            phone_verified_at=verified_at,
        )

        contact.email = 'replacement@example.org'
        contact.save(update_fields=('email',))
        contact.refresh_from_db()

        self.assertFalse(contact.email_consent)
        self.assertIsNone(contact.email_verified_at)
        self.assertIsNotNone(contact.consent_updated_at)
        self.assertTrue(contact.sms_consent)
        self.assertTrue(contact.voice_consent)
        self.assertEqual(contact.phone_verified_at, verified_at)

    def test_changing_phone_revokes_phone_consents_and_verification(self):
        verified_at = timezone.now()
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Trusted Contact',
            phone_number='+1 202 555 0100',
            email='trusted@example.org',
            email_consent=True,
            sms_consent=True,
            voice_consent=True,
            email_verified_at=verified_at,
            phone_verified_at=verified_at,
        )

        contact.phone_number = '+1 202 555 0101'
        contact.save(update_fields=('phone_number',))
        contact.refresh_from_db()

        self.assertFalse(contact.sms_consent)
        self.assertFalse(contact.voice_consent)
        self.assertIsNone(contact.phone_verified_at)
        self.assertIsNotNone(contact.consent_updated_at)
        self.assertTrue(contact.email_consent)
        self.assertEqual(contact.email_verified_at, verified_at)

    def test_user_isolation(self):
        EmergencyContact.objects.create(user='bob', name='Other User', phone_number='+91 1111111111')
        response = self.client.get('/EmergencyContacts')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Other User')


class SOSDeliveryTestCase(TransactionTestCase):
    def setUp(self):
        isolate_register_test_database(self)
        ensure_register_table()
        public_url_patch = patch.dict(os.environ, {'SOS_PUBLIC_BASE_URL': 'https://sos.example.org'}, clear=False)
        public_url_patch.start()
        self.addCleanup(public_url_patch.stop)
        dns_patch = patch(
            'SafetyApp.views.socket.getaddrinfo',
            return_value=[(2, 1, 6, '', ('93.184.216.34', 443))],
        )
        dns_patch.start()
        self.addCleanup(dns_patch.stop)
        self.client = Client()
        self.username = 'alice'
        self.email = 'alice@gmail.com'

        session = self.client.session
        session['username'] = self.username
        session['email'] = self.email
        session['is_logged_in'] = True
        session.save()

    def create_eligible_contact(
        self,
        name,
        phone_number,
        email,
        active=True,
        primary=False,
    ):
        verified_at = timezone.now()
        return EmergencyContact.objects.create(
            user=self.username,
            name=name,
            phone_number=phone_number,
            email=email,
            is_active=active,
            is_primary=primary,
            email_consent=True,
            sms_consent=True,
            voice_consent=True,
            email_verified_at=verified_at,
            phone_verified_at=verified_at,
        )

    def test_registration_email_becomes_primary_emergency_contact(self):
        username = 'charlie'
        with get_register_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                (username, 'password', '9999999999', 'charlie@gmail.com', 'Test address')
            )
            conn.commit()

        EmergencyContact.objects.create(user=username, name='Existing Contact', phone_number='+91 7777777777', email='existing@gmail.com', is_active=True, is_primary=False)
        from .views import sync_signup_emergency_contact_for_user
        contact = sync_signup_emergency_contact_for_user(username, 'charlie@gmail.com', '9999999999')

        self.assertIsNotNone(contact)
        self.assertEqual(contact.email, 'charlie@gmail.com')
        self.assertTrue(EmergencyContact.objects.filter(user=username, email='charlie@gmail.com', is_primary=True).exists())
        self.assertTrue(EmergencyContact.objects.filter(user=username, email='existing@gmail.com').exists())

    def test_no_hardcoded_placeholder_email_is_created(self):
        username = 'placeholder-user'
        from .views import sync_signup_emergency_contact_for_user

        contact = sync_signup_emergency_contact_for_user(username, 'signupemergency@example.com', '9999999999')

        self.assertIsNone(contact)
        self.assertFalse(EmergencyContact.objects.filter(user=username, email='signupemergency@example.com').exists())

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_signup_contact_without_consent_is_not_notified(self, mock_smtp):
        from .views import get_register_db_connection

        with get_register_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                (self.username, 'password', '9999999999', 'registered@gmail.com', 'Test address')
            )
            conn.commit()

        self.client.get('/Panic')
        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        self.assertEqual(response.status_code, 302)
        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        contact = EmergencyContact.objects.get(user=self.username, email='registered@gmail.com')
        self.assertFalse(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=contact,
        ).exists())
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=sos_event).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_starts_mock_notifications_for_eligible_contact(self, mock_smtp):
        contact = self.create_eligible_contact(
            'Mom',
            '+91 1111111111',
            'mom@gmail.com',
            primary=True,
        )

        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        self.assertEqual(response.status_code, 302)
        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        attempts = SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=contact,
        )
        self.assertEqual(attempts.count(), 3)
        self.assertFalse(attempts.exclude(status=SOSNotificationAttempt.STATUS_ACCEPTED).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_skips_inactive_contact(self, mock_smtp):
        active_contact = self.create_eligible_contact(
            'Active',
            '+91 1111111111',
            'active@gmail.com',
        )
        inactive_contact = self.create_eligible_contact(
            'Inactive',
            '+91 2222222222',
            'inactive@gmail.com',
            active=False,
        )

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(
                sos_event=sos_event,
                emergency_contact=active_contact,
            ).count(),
            3,
        )
        self.assertFalse(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=inactive_contact,
        ).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_contacts_without_email_are_excluded_from_email_channel_only(self, mock_smtp):
        with get_register_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                (self.username, 'password', '9999999999', 'registered@gmail.com', 'Test address')
            )
            conn.commit()

        active_with_email = self.create_eligible_contact(
            'Active Mail',
            '+91 1111111111',
            'active@gmail.com',
        )
        active_without_email = self.create_eligible_contact(
            'No Email',
            '+91 2222222222',
            '',
            primary=True,
        )
        inactive_contact = self.create_eligible_contact(
            'Inactive',
            '+91 3333333333',
            'inactive@gmail.com',
            active=False,
        )

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        no_email_channels = set(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=active_without_email,
        ).values_list('channel', flat=True))
        self.assertEqual(no_email_channels, {
            SOSNotificationAttempt.CHANNEL_SMS,
            SOSNotificationAttempt.CHANNEL_VOICE,
        })
        self.assertFalse(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=active_with_email,
        ).exists())
        self.assertFalse(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=inactive_contact,
        ).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_multiple_contacts_are_escalated_sequentially(self, mock_smtp):
        first = self.create_eligible_contact(
            'First',
            '+91 1111111111',
            'first@gmail.com',
            primary=True,
        )
        second = self.create_eligible_contact(
            'Second',
            '+91 2222222222',
            'second@gmail.com',
        )

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(sos_event=sos_event).count(),
            3,
        )
        self.assertEqual(
            set(SOSNotificationAttempt.objects.filter(
                sos_event=sos_event,
            ).values_list('status', flat=True)),
            {SOSNotificationAttempt.STATUS_ACCEPTED},
        )
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(
                sos_event=sos_event,
                emergency_contact=first,
            ).count(),
            3,
        )
        state = SOSEscalationState.objects.get(sos_event=sos_event)
        self.assertEqual(state.current_contact_id, first.pk)
        past_deadline = timezone.now() - timedelta(seconds=1)
        state.acknowledgement_deadline = past_deadline
        state.next_action_at = past_deadline
        state.save(update_fields=('acknowledgement_deadline', 'next_action_at'))

        process_due_escalations()

        self.assertEqual(
            SOSNotificationAttempt.objects.filter(
                sos_event=sos_event,
                emergency_contact=second,
            ).count(),
            3,
        )
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_uncertain_channel_does_not_retry_or_advance_to_another_contact(self, mock_smtp):
        first = self.create_eligible_contact(
            'First',
            '+91 1111111111',
            'first@gmail.com',
        )
        second = self.create_eligible_contact(
            'Second',
            '+91 2222222222',
            'second@gmail.com',
        )

        class SelectiveEmailProvider(object):
            channel = SOSNotificationAttempt.CHANNEL_EMAIL

            def send(self, attempt, event, contact):
                if contact.pk == first.pk:
                    raise OSError('synthetic provider failure')
                return ProviderResult(
                    status=SOSNotificationAttempt.STATUS_ACCEPTED,
                    provider_reference='mock-email-success',
                )

        with patch.dict(
            'SafetyApp.sos_notifications.DEFAULT_PROVIDERS',
            {SOSNotificationAttempt.CHANNEL_EMAIL: SelectiveEmailProvider()},
        ):
            self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        first_email = SOSNotificationAttempt.objects.get(
            sos_event=sos_event,
            emergency_contact=first,
            channel=SOSNotificationAttempt.CHANNEL_EMAIL,
        )
        accepted_secondary_channel = SOSNotificationAttempt.objects.get(
            sos_event=sos_event,
            emergency_contact=first,
            channel=SOSNotificationAttempt.CHANNEL_SMS,
        )
        self.assertEqual(first_email.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(accepted_secondary_channel.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertFalse(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=second,
        ).exists())
        self.assertEqual(SOSNotificationAttempt.objects.filter(sos_event=sos_event).count(), 3)
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_repeated_activation_does_not_duplicate_contact_channel_attempts(self, mock_smtp):
        contact = self.create_eligible_contact(
            'Mom',
            '+91 1111111111',
            'mom@safealarm.mail',
        )

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})
        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(
                sos_event=sos_event,
                emergency_contact=contact,
            ).count(),
            3,
        )
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_users_cannot_trigger_notifications_to_another_users_contacts(self, mock_smtp):
        other_user = 'bob'
        other_contact = self.create_eligible_contact(
            'Other',
            '+91 4444444444',
            'other@gmail.com',
        )
        other_contact.user = other_user
        other_contact.save()
        my_contact = self.create_eligible_contact(
            'Mine',
            '+91 5555555555',
            'mine@gmail.com',
        )

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(
            SOSNotificationAttempt.objects.filter(
                sos_event=sos_event,
                emergency_contact=my_contact,
            ).count(),
            3,
        )
        self.assertFalse(SOSNotificationAttempt.objects.filter(
            sos_event=sos_event,
            emergency_contact=other_contact,
        ).exists())
        mock_smtp.assert_not_called()

    def test_missing_location_is_handled_correctly(self):
        event = SOSEvent.objects.create(user=self.username, latitude=None, longitude=None, status=SOSEvent.STATUS_ACTIVE)
        from .views import build_sos_email_body
        body = build_sos_email_body(event, self.username)
        self.assertIn('Location is currently unavailable.', body)

    def test_google_maps_link_generated_only_with_valid_coordinates(self):
        from .views import build_sos_email_body
        event = SOSEvent.objects.create(user=self.username, latitude=12.34, longitude=56.78, status=SOSEvent.STATUS_ACTIVE)
        body = build_sos_email_body(event, self.username)
        self.assertIn('https://www.google.com/maps?q=12.34,56.78', body)

        event.status = SOSEvent.STATUS_RESOLVED
        event.save(update_fields=['status'])
        no_coords = SOSEvent.objects.create(user=self.username, latitude=None, longitude=None, status=SOSEvent.STATUS_ACTIVE)
        body_no_coords = build_sos_email_body(no_coords, self.username)
        self.assertNotIn('https://www.google.com/maps', body_no_coords)


class PanicSOSIntegrationTestCase(TransactionTestCase):
    def setUp(self):
        isolate_register_test_database(self)
        self.client = Client()
        self.username = 'alice'
        self.email = 'alice@gmail.com'
        session = self.client.session
        session['username'] = self.username
        session['email'] = self.email
        session['is_logged_in'] = True
        session.save()

    def test_panic_page_contains_sos_section_and_existing_form(self):
        response = self.client.get('/Panic')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Emergency SOS')
        self.assertContains(response, 'class="emergency"')
        self.assertContains(response, '>SOS</span>')
        self.assertNotContains(response, '>Activate SOS<')
        self.assertContains(response, 'Press the button below')

    def test_panic_page_contains_quick_add_contact_form_and_active_contacts(self):
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            relationship='Mother',
            is_active=True,
            is_primary=True,
        )

        response = self.client.get('/Panic')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '+ Add Emergency Contact')
        self.assertContains(response, 'Manage All Contacts')
        self.assertContains(response, contact.name)
        self.assertContains(response, contact.relationship)
        self.assertContains(response, contact.phone_number)
        self.assertContains(response, contact.email)
        active_contacts = EmergencyContact.objects.filter(user=self.username, is_active=True)
        self.assertEqual(response.context['emergency_contact_count'], active_contacts.count())
        self.assertEqual(
            response.context['email_contact_count'],
            active_contacts.exclude(email__isnull=True).exclude(email='').count(),
        )

    def test_quick_add_contact_reuses_existing_contact_flow_and_returns_to_panic(self):
        response = self.client.post('/EmergencyContactAdd', {
            'name': 'Sister',
            'phone_number': '+91 9999999999',
            'email': 'sister@gmail.com',
            'relationship': 'Sister',
            'is_primary': 'on',
            'return_to': 'Panic',
        })

        self.assertRedirects(response, '/Panic')
        contact = EmergencyContact.objects.get(user=self.username, name='Sister')
        self.assertTrue(contact.is_active)
        panic_response = self.client.get('/Panic')
        self.assertContains(panic_response, 'Sister')
        self.assertEqual(
            panic_response.context['emergency_contact_count'],
            EmergencyContact.objects.filter(user=self.username, is_active=True).count(),
        )

    def test_active_sos_is_displayed_on_panic_page(self):
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )
        event = SOSEvent.objects.create(user=self.username, latitude=12.34, longitude=56.78, status=SOSEvent.STATUS_ACTIVE)
        SOSEmailDelivery.objects.create(sos_event=event, emergency_contact=contact, status=SOSEmailDelivery.STATUS_SENT)

        response = self.client.get('/Panic')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SOS ACTIVE')
        self.assertContains(response, 'Email status:')
        self.assertContains(response, 'Emergency alerts:')
        self.assertContains(response, 'https://www.google.com/maps?q=12.34,56.78')
        summary = response.context['delivery_summary']
        self.assertEqual(summary['source'], 'legacy')
        self.assertTrue(summary['legacy_only'])
        self.assertEqual(summary['legacy_accepted'], 1)
        self.assertEqual(summary['sent'], 0)

    def test_update_sos_status_rejects_other_users_event(self):
        other_event = SOSEvent.objects.create(user='bob', latitude=10.0, longitude=20.0, status=SOSEvent.STATUS_ACTIVE)
        response = self.client.post('/UpdateSOSStatus', {'status': SOSEvent.STATUS_CANCELLED})
        self.assertEqual(response.status_code, 302)
        other_event.refresh_from_db()
        self.assertEqual(other_event.status, SOSEvent.STATUS_ACTIVE)

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_state_remains_active_when_smtp_is_unavailable(self, mock_smtp):
        verified_at = timezone.now()
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
            email_consent=True,
            sms_consent=True,
            voice_consent=True,
            email_verified_at=verified_at,
            phone_verified_at=verified_at,
        )
        mock_smtp.side_effect = OSError('synthetic SMTP failure')

        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})
        self.assertEqual(response.status_code, 302)

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(event.status, SOSEvent.STATUS_ACTIVE)
        attempts = SOSNotificationAttempt.objects.filter(
            sos_event=event,
            emergency_contact=contact,
        )
        self.assertEqual(attempts.count(), 3)
        self.assertFalse(attempts.exclude(
            status=SOSNotificationAttempt.STATUS_ACCEPTED
        ).exists())
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=event).exists())
        mock_smtp.assert_not_called()
        self.assertEqual(event.status, SOSEvent.STATUS_ACTIVE)
