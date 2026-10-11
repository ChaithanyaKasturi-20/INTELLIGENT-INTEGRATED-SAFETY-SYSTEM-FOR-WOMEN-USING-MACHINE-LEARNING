import os
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, Client

from .models import EmergencyContact, SOSEvent, SOSEmailDelivery
from .views import get_register_db_connection


class EmergencyContactTestCase(TestCase):
    def setUp(self):
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

    def test_user_isolation(self):
        EmergencyContact.objects.create(user='bob', name='Other User', phone_number='+91 1111111111')
        response = self.client.get('/EmergencyContacts')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Other User')


class SOSDeliveryTestCase(TestCase):
    def setUp(self):
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

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_uses_signup_email_when_no_contact_record_exists(self, mock_smtp):
        from .views import get_register_db_connection

        with get_register_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                (self.username, 'password', '9999999999', 'registered@gmail.com', 'Test address')
            )
            conn.commit()

        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        self.assertEqual(response.status_code, 302)
        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertTrue(EmergencyContact.objects.filter(user=self.username, email='registered@gmail.com').exists())
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event).exists())
        mock_smtp.assert_called_once()

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_sends_email_to_eligible_active_contact(self, mock_smtp):
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 1111111111',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )

        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        self.assertEqual(response.status_code, 302)
        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=contact, status='sent').exists())
        mock_smtp.assert_called_once()

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_skips_inactive_contact(self, mock_smtp):
        active_contact = EmergencyContact.objects.create(user=self.username, name='Active', phone_number='+91 1111111111', email='active@gmail.com', is_active=True)
        inactive_contact = EmergencyContact.objects.create(user=self.username, name='Inactive', phone_number='+91 2222222222', email='inactive@gmail.com', is_active=False)

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=active_contact, status='sent').exists())
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=inactive_contact).exists())
        self.assertEqual(mock_smtp.call_count, 1)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_contacts_without_email_are_excluded_from_email_delivery(self, mock_smtp):
        with get_register_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                (self.username, 'password', '9999999999', 'registered@gmail.com', 'Test address')
            )
            conn.commit()

        active_with_email = EmergencyContact.objects.create(user=self.username, name='Active Mail', phone_number='+91 1111111111', email='active@gmail.com', is_active=True)
        active_without_email = EmergencyContact.objects.create(user=self.username, name='No Email', phone_number='+91 2222222222', email='', is_active=True)
        inactive_contact = EmergencyContact.objects.create(user=self.username, name='Inactive', phone_number='+91 3333333333', email='inactive@gmail.com', is_active=False)

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=active_with_email, status='sent').exists())
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=active_without_email).exists())
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=inactive_contact).exists())
        self.assertEqual(mock_smtp.call_count, 1)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_multiple_contacts_are_handled_independently(self, mock_smtp):
        first = EmergencyContact.objects.create(user=self.username, name='First', phone_number='+91 1111111111', email='first@gmail.com', is_active=True)
        second = EmergencyContact.objects.create(user=self.username, name='Second', phone_number='+91 2222222222', email='second@gmail.com', is_active=True)

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(SOSEmailDelivery.objects.filter(sos_event=sos_event, status='sent').count(), 2)
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=first, status='sent').exists())
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=second, status='sent').exists())

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_one_failed_email_does_not_stop_other_recipients(self, mock_smtp):
        first = EmergencyContact.objects.create(user=self.username, name='First', phone_number='+91 1111111111', email='first@gmail.com', is_active=True)
        second = EmergencyContact.objects.create(user=self.username, name='Second', phone_number='+91 2222222222', email='second@gmail.com', is_active=True)
        connection = mock_smtp.return_value.__enter__.return_value
        connection.sendmail.side_effect = [Exception('SMTP failed'), None]

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=first, status='failed').exists())
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=second, status='sent').exists())
        self.assertEqual(SOSEmailDelivery.objects.filter(sos_event=sos_event).count(), 2)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_duplicate_email_is_prevented_for_same_sos_event_contact(self, mock_smtp):
        contact = EmergencyContact.objects.create(user=self.username, name='Mom', phone_number='+91 1111111111', email='mom@safealarm.mail', is_active=True)

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})
        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=contact).count(), 1)
        self.assertEqual(mock_smtp.call_count, 1)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_users_cannot_trigger_emails_to_another_users_contacts(self, mock_smtp):
        other_user = 'bob'
        other_contact = EmergencyContact.objects.create(user=other_user, name='Other', phone_number='+91 4444444444', email='other@gmail.com', is_active=True)
        my_contact = EmergencyContact.objects.create(user=self.username, name='Mine', phone_number='+91 5555555555', email='mine@gmail.com', is_active=True)

        self.client.post('/ActivateSOS', {'latitude': '10', 'longitude': '20'})

        sos_event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertTrue(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=my_contact, status='sent').exists())
        self.assertFalse(SOSEmailDelivery.objects.filter(sos_event=sos_event, emergency_contact=other_contact).exists())

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

        no_coords = SOSEvent.objects.create(user=self.username, latitude=None, longitude=None, status=SOSEvent.STATUS_ACTIVE)
        body_no_coords = build_sos_email_body(no_coords, self.username)
        self.assertNotIn('https://www.google.com/maps', body_no_coords)


class PanicSOSIntegrationTestCase(TestCase):
    def setUp(self):
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
        self.assertContains(response, 'Emergency contacts notified:')
        self.assertContains(response, 'Emergency alerts:')
        self.assertContains(response, 'https://www.google.com/maps?q=12.34,56.78')

    def test_update_sos_status_rejects_other_users_event(self):
        other_event = SOSEvent.objects.create(user='bob', latitude=10.0, longitude=20.0, status=SOSEvent.STATUS_ACTIVE)
        response = self.client.post('/UpdateSOSStatus', {'status': SOSEvent.STATUS_CANCELLED})
        self.assertEqual(response.status_code, 302)
        other_event.refresh_from_db()
        self.assertEqual(other_event.status, SOSEvent.STATUS_ACTIVE)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_sos_state_remains_active_when_email_delivery_fails(self, mock_smtp):
        EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )
        connection = mock_smtp.return_value.__enter__.return_value
        connection.sendmail.side_effect = Exception('SMTP failed')

        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})
        self.assertEqual(response.status_code, 302)

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertIsNotNone(event)
        self.assertEqual(SOSEmailDelivery.objects.filter(sos_event=event, status=SOSEmailDelivery.STATUS_FAILED).count(), 1)
        self.assertEqual(event.status, SOSEvent.STATUS_ACTIVE)
