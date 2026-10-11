import hashlib
import json
import os
from datetime import timedelta
from urllib.parse import urlsplit
from unittest.mock import patch

from django.test import Client, TestCase
from django.template.loader import render_to_string
from django.utils import timezone

from .models import EmergencyContact, SafetyTimer, SOSEmailDelivery, SOSEvent, SOSLiveLocationShare
from .views import build_sos_live_location_url, create_sos_live_location_share


class SOSLiveLocationTestCase(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.dns_patcher = patch(
            'SafetyApp.views.socket.getaddrinfo',
            return_value=[(2, 1, 6, '', ('93.184.216.34', 443))],
        )
        self.dns_patcher.start()
        self.addCleanup(self.dns_patcher.stop)
        self.client = Client()
        self.username = 'sos-location-user'
        session = self.client.session
        session['username'] = self.username
        session['is_logged_in'] = True
        session.save()

    def create_active_sos(self, latitude=None, longitude=None, updated_at=None):
        event = SOSEvent.objects.create(
            user=self.username,
            latitude=latitude,
            longitude=longitude,
            location_updated_at=updated_at,
            status=SOSEvent.STATUS_ACTIVE,
        )
        token = create_sos_live_location_share(event)
        return event, token

    def create_contact(self):
        return EmergencyContact.objects.create(
            user=self.username,
            name='Trusted Contact',
            phone_number='+1 555 555 0100',
            email='trusted@gmail.com',
            is_active=True,
        )

    def test_public_url_validation_accepts_public_https_origin(self):
        with patch.dict(os.environ, {'SOS_PUBLIC_BASE_URL': 'https://sos.example.org'}, clear=False):
            link = build_sos_live_location_url('high-entropy-token')

        self.assertEqual(
            link,
            'https://sos.example.org/SOS/LiveLocation#high-entropy-token',
        )

    def test_public_url_validation_rejects_unsafe_or_non_origin_urls(self):
        invalid_urls = (
            'http://sos.example.org',
            'https://localhost',
            'https://child.localhost',
            'https://127.0.0.1',
            'https://192.168.1.10',
            'https://[::1]',
            'https://sos.example.org/app',
            'https://user:password@sos.example.org',
            'https://sos.example.org/?redirect=localhost',
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                with patch.dict(os.environ, {'SOS_PUBLIC_BASE_URL': url}, clear=False):
                    self.assertEqual(build_sos_live_location_url('token'), '')

    def test_public_url_validation_rejects_domain_resolving_to_private_address(self):
        with patch.dict(os.environ, {'SOS_PUBLIC_BASE_URL': 'https://private.example'}, clear=False):
            with patch(
                'SafetyApp.views.socket.getaddrinfo',
                return_value=[(2, 1, 6, '', ('10.20.30.40', 443))],
            ):
                self.assertEqual(build_sos_live_location_url('token'), '')

    def test_public_url_validation_rejects_unresolvable_domain(self):
        with patch.dict(os.environ, {'SOS_PUBLIC_BASE_URL': 'https://unknown.example'}, clear=False):
            with patch('SafetyApp.views.socket.getaddrinfo', side_effect=OSError('unavailable')):
                self.assertEqual(build_sos_live_location_url('token'), '')

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_activation_saves_gps_and_emails_tokenized_live_link(self, mock_smtp):
        contact = self.create_contact()
        response = self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        self.assertEqual(response.status_code, 302)
        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual((event.latitude, event.longitude), (12.34, 56.78))
        self.assertIsNotNone(event.location_updated_at)
        share = SOSLiveLocationShare.objects.get(sos_event=event)
        self.assertTrue(share.is_valid())
        self.assertEqual(SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact).status, 'sent')

        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        link = next(line for line in message.splitlines() if line.startswith('https://sos.example.org/'))
        token = urlsplit(link).fragment
        self.assertTrue(token)
        self.assertEqual(share.token_hash, hashlib.sha256(token.encode('utf-8')).hexdigest())
        self.assertNotIn(token, share.token_hash)

        self.client.post('/ActivateSOS', {'latitude': '13', 'longitude': '57'})
        self.assertEqual(SOSEvent.objects.filter(user=self.username, status=SOSEvent.STATUS_ACTIVE).count(), 1)
        mock_smtp.assert_called_once()

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': '',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_activation_without_public_url_sends_initial_email_without_external_link(self, mock_smtp):
        contact = self.create_contact()
        self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual((event.latitude, event.longitude), (12.34, 56.78))
        self.assertTrue(SOSLiveLocationShare.objects.filter(sos_event=event).exists())
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_SENT)
        self.assertEqual(delivery.error_message, '')
        mock_smtp.return_value.__enter__.return_value.login.assert_called_once_with('sender@example.com', 'test-password')
        mock_smtp.return_value.__enter__.return_value.sendmail.assert_called_once()
        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        self.assertIn('Location: 12.34, 56.78', message)
        self.assertIn('https://www.google.com/maps?q=12.34,56.78', message)
        self.assertIn('secure external live-location link is not configured', message)
        self.assertNotIn('127.0.0.1', message)
        self.assertNotIn('localhost', message)
        status_page = self.client.get('/Panic')
        self.assertContains(status_page, 'Local GPS still works here')
        self.assertNotContains(status_page, 'Email failure details:')

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_activation_with_public_url_adds_secure_link_to_initial_email(self, mock_smtp):
        contact = self.create_contact()
        self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_SENT)
        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        self.assertIn('Location: 12.34, 56.78', message)
        self.assertIn('https://www.google.com/maps?q=12.34,56.78', message)
        self.assertIn('https://sos.example.org/SOS/LiveLocation#', message)

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': '',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_missing_gps_does_not_fabricate_coordinates_or_map_link(self, mock_smtp):
        contact = self.create_contact()
        self.client.post('/ActivateSOS', {'latitude': '', 'longitude': ''})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_SENT)
        self.assertIsNone(event.latitude)
        self.assertIsNone(event.longitude)
        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        self.assertIn('Location is currently unavailable.', message)
        self.assertNotIn('Google Maps:', message)
        self.assertNotIn('0,0', message)

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_smtp_failure_is_recorded_without_resolving_sos(self, mock_smtp):
        contact = self.create_contact()
        mock_smtp.return_value.__enter__.return_value.sendmail.side_effect = OSError('SMTP unavailable')

        self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_FAILED)
        self.assertIn('Could not connect to the SMTP server', delivery.error_message)
        self.assertNotIn('SMTP unavailable', delivery.error_message)
        self.assertTrue(event.live_location_share.is_valid())

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL', side_effect=OSError('connection failed; credential=private'))
    def test_smtp_connection_failure_is_reported_without_technical_details(self, mock_smtp):
        contact = self.create_contact()
        self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_FAILED)
        self.assertEqual(
            delivery.error_message,
            'Could not connect to the SMTP server. Check the network and SMTP availability.',
        )
        self.assertNotIn('private', delivery.error_message)
        self.assertTrue(event.live_location_share.is_valid())
        mock_smtp.assert_called_once_with('smtp.gmail.com', 465)

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_invalid_recipient_is_rejected_without_attempting_smtp(self, mock_smtp):
        contact = self.create_contact()
        EmergencyContact.objects.filter(pk=contact.pk).update(email='not-an-email-address')

        self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_FAILED)
        self.assertEqual(delivery.error_message, 'This emergency contact has an invalid email address.')
        mock_smtp.assert_not_called()

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_smtp_rejection_is_not_reported_as_accepted(self, mock_smtp):
        contact = self.create_contact()
        mock_smtp.return_value.__enter__.return_value.sendmail.return_value = {
            contact.email: (550, b'recipient rejected')
        }

        self.client.post('/ActivateSOS', {'latitude': '12.34', 'longitude': '56.78'})

        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.get(sos_event=event, emergency_contact=contact)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_FAILED)
        self.assertEqual(
            delivery.error_message,
            'The SMTP server refused this recipient (response code 550).',
        )

    def test_owner_location_updates_require_session_and_valid_coordinates(self):
        event, _ = self.create_active_sos()
        unauthenticated = Client().post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 1, 'longitude': 2}),
            content_type='application/json',
        )
        self.assertEqual(unauthenticated.status_code, 401)

        another_user = Client()
        session = another_user.session
        session['username'] = 'another-user'
        session.save()
        denied = another_user.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 1, 'longitude': 2}),
            content_type='application/json',
        )
        self.assertEqual(denied.status_code, 409)

        invalid = self.client.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 91, 'longitude': 2}),
            content_type='application/json',
        )
        self.assertEqual(invalid.status_code, 400)

        updated = self.client.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 40.7, 'longitude': -74}),
            content_type='application/json',
        )
        self.assertEqual(updated.status_code, 200)
        event.refresh_from_db()
        self.assertEqual((event.latitude, event.longitude), (40.7, -74))
        self.assertIsNotNone(event.location_updated_at)

    def test_local_gps_update_does_not_depend_on_external_share_link(self):
        event, _ = self.create_active_sos()
        event.live_location_share.revoke()

        response = self.client.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 41.25, 'longitude': -72.5}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        event.refresh_from_db()
        self.assertEqual((event.latitude, event.longitude), (41.25, -72.5))
        self.assertIsNotNone(event.location_updated_at)

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_location_updates_never_trigger_another_email(self, mock_smtp):
        self.create_active_sos()

        response = self.client.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 41.25, 'longitude': -72.5}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        mock_smtp.assert_not_called()

    def test_owner_location_status_returns_latest_saved_coordinates(self):
        event, _ = self.create_active_sos(
            latitude=41.25,
            longitude=-72.5,
            updated_at=timezone.now(),
        )

        response = self.client.get('/SOS/LocationStatus')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], SOSEvent.STATUS_ACTIVE)
        self.assertEqual((response.json()['latitude'], response.json()['longitude']), (41.25, -72.5))
        self.assertTrue(response['Cache-Control'].startswith('no-store'))

    def test_latest_saved_location_is_rendered_separately_from_gps_status(self):
        event, _ = self.create_active_sos(
            latitude=41.25,
            longitude=-72.5,
            updated_at=timezone.now(),
        )
        html = render_to_string('sos_location_status.html', {
            'active_event': event,
            'delivery_summary': {
                'sent': 0,
                'failed': 0,
                'pending': 0,
                'unattempted': 0,
                'no_email': 0,
                'failures': [],
                'can_retry': False,
            },
            'public_location_link_configured': False,
        })

        self.assertIn('Live location:</strong> <span id="sosLocationState">Waiting for GPS</span>', html)
        self.assertIn('id="sosLatestCoordinates">41.25, -72.5</span>', html)
        self.assertIn('contacts cannot reach a 127.0.0.1 address', html)

    def test_location_status_rejects_another_users_session(self):
        self.create_active_sos(latitude=1, longitude=2, updated_at=timezone.now())
        another_user = Client()
        session = another_user.session
        session['username'] = 'another-user'
        session.save()

        response = another_user.get('/SOS/LocationStatus')

        self.assertEqual(response.status_code, 409)

    def test_resolved_sos_rejects_further_updates_and_revokes_share(self):
        event, token = self.create_active_sos()
        self.client.post('/UpdateSOSStatus', {'status': SOSEvent.STATUS_RESOLVED})

        event.refresh_from_db()
        self.assertEqual(event.status, SOSEvent.STATUS_RESOLVED)
        self.assertIsNotNone(event.live_location_share.revoked_at)
        update = self.client.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 1, 'longitude': 2}),
            content_type='application/json',
        )
        recipient = self.client.post(
            '/SOS/LiveLocation/Data',
            data=json.dumps({'token': token}),
            content_type='application/json',
        )
        self.assertEqual(update.status_code, 409)
        self.assertEqual(recipient.status_code, 410)

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_explicit_retry_sends_failed_delivery_once(self, mock_smtp):
        contact = self.create_contact()
        event, old_token = self.create_active_sos()
        delivery = SOSEmailDelivery.objects.create(
            sos_event=event,
            emergency_contact=contact,
            status=SOSEmailDelivery.STATUS_FAILED,
            error_message='No email was sent because SOS_PUBLIC_BASE_URL was missing.',
        )

        first_response = self.client.post('/SOS/RetryEmail')

        self.assertEqual(first_response.status_code, 302)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_SENT)
        self.assertEqual(mock_smtp.return_value.__enter__.return_value.sendmail.call_count, 1)
        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        link = next(line for line in message.splitlines() if line.startswith('https://sos.example.org/'))
        retried_token = urlsplit(link).fragment
        self.assertTrue(retried_token)
        self.assertNotEqual(retried_token, old_token)
        recipient_response = self.client.post(
            '/SOS/LiveLocation/Data',
            data=json.dumps({'token': retried_token}),
            content_type='application/json',
        )
        self.assertEqual(recipient_response.status_code, 200)
        old_recipient_access = self.client.post(
            '/SOS/LiveLocation/Data',
            data=json.dumps({'token': old_token}),
            content_type='application/json',
        )
        self.assertEqual(old_recipient_access.status_code, 404)

        self.client.post('/SOS/RetryEmail')
        self.assertEqual(mock_smtp.return_value.__enter__.return_value.sendmail.call_count, 1)

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_ambiguous_smtp_failure_is_not_safe_to_retry(self, mock_smtp):
        contact = self.create_contact()
        event, _ = self.create_active_sos()
        delivery = SOSEmailDelivery.objects.create(
            sos_event=event,
            emergency_contact=contact,
            status=SOSEmailDelivery.STATUS_FAILED,
            error_message='The SMTP provider did not accept this email. Sensitive technical details are hidden.',
        )

        self.client.post('/SOS/RetryEmail')

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_FAILED)
        mock_smtp.assert_not_called()

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': '',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_retry_without_public_https_origin_sends_email_without_live_link(self, mock_smtp):
        contact = self.create_contact()
        event, _ = self.create_active_sos()
        delivery = SOSEmailDelivery.objects.create(
            sos_event=event,
            emergency_contact=contact,
            status=SOSEmailDelivery.STATUS_FAILED,
            error_message='SOS_PUBLIC_BASE_URL must be configured as an externally reachable HTTPS origin.',
        )

        response = self.client.post('/SOS/RetryEmail')

        delivery.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_SENT)
        self.assertEqual(delivery.error_message, '')
        mock_smtp.return_value.__enter__.return_value.sendmail.assert_called_once()
        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        self.assertNotIn('https://sos.example.org/SOS/LiveLocation#', message)
        self.assertNotIn('127.0.0.1', message)
        self.assertNotIn('localhost', message)

    def test_recipient_can_read_latest_location_and_staleness(self):
        event, token = self.create_active_sos(
            latitude=10,
            longitude=20,
            updated_at=timezone.now() - timedelta(minutes=3),
        )
        url = '/SOS/LiveLocation/Data'
        stale = self.client.post(url, data=json.dumps({'token': token}), content_type='application/json')
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.json()['status'], 'stale')

        self.client.post(
            '/SOS/LocationUpdate',
            data=json.dumps({'latitude': 11, 'longitude': 21}),
            content_type='application/json',
        )
        latest = self.client.post(url, data=json.dumps({'token': token}), content_type='application/json')
        self.assertEqual(latest.json()['status'], 'sharing')
        self.assertEqual((latest.json()['latitude'], latest.json()['longitude']), (11, 21))
        self.assertTrue(latest['Cache-Control'].startswith('no-store'))
        page = self.client.get('/SOS/LiveLocation')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Last location update')
        self.assertEqual(event.status, SOSEvent.STATUS_ACTIVE)

    def test_share_token_reads_only_its_own_event_and_response_has_no_private_profile_fields(self):
        first_event, first_token = self.create_active_sos(
            latitude=10,
            longitude=20,
            updated_at=timezone.now(),
        )
        second_event, second_token = self.create_active_sos(
            latitude=30,
            longitude=40,
            updated_at=timezone.now(),
        )

        first_response = self.client.post(
            '/SOS/LiveLocation/Data',
            data=json.dumps({'token': first_token}),
            content_type='application/json',
        )
        second_response = self.client.post(
            '/SOS/LiveLocation/Data',
            data=json.dumps({'token': second_token}),
            content_type='application/json',
        )

        self.assertEqual((first_response.json()['latitude'], first_response.json()['longitude']), (10, 20))
        self.assertEqual((second_response.json()['latitude'], second_response.json()['longitude']), (30, 40))
        self.assertEqual(
            set(first_response.json()),
            {'status', 'latitude', 'longitude', 'updated_at', 'stale_after_seconds'},
        )
        self.assertNotIn(self.username, first_response.content.decode('utf-8'))
        self.assertNotIn('email', first_response.content.decode('utf-8').lower())
        self.assertNotEqual(first_event.pk, second_event.pk)

    def test_public_location_endpoint_rate_limits_excessive_polls(self):
        _, token = self.create_active_sos(latitude=10, longitude=20, updated_at=timezone.now())
        responses = [
            self.client.post(
                '/SOS/LiveLocation/Data',
                data=json.dumps({'token': token}),
                content_type='application/json',
            )
            for _ in range(31)
        ]

        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(responses[-1]['Retry-After'], '60')

    def test_invalid_expired_and_revoked_tokens_are_rejected(self):
        event, token = self.create_active_sos()
        url = '/SOS/LiveLocation/Data'

        invalid = self.client.post(url, data=json.dumps({'token': 'invalid'}), content_type='application/json')
        self.assertEqual(invalid.status_code, 404)
        self.assertTrue(invalid['Cache-Control'].startswith('no-store'))
        self.assertEqual(invalid['Referrer-Policy'], 'no-referrer')

        share = event.live_location_share
        share.expires_at = timezone.now() - timedelta(seconds=1)
        share.save(update_fields=['expires_at'])
        expired = self.client.post(url, data=json.dumps({'token': token}), content_type='application/json')
        self.assertEqual(expired.status_code, 410)
        self.assertTrue(expired['Cache-Control'].startswith('no-store'))

        share.expires_at = timezone.now() + timedelta(hours=1)
        share.save(update_fields=['expires_at'])
        share.revoke()
        revoked = self.client.post(url, data=json.dumps({'token': token}), content_type='application/json')
        self.assertEqual(revoked.status_code, 410)
        self.assertTrue(revoked['Cache-Control'].startswith('no-store'))

    @patch.dict(os.environ, {
        'PANIC_EMAIL_USER': 'sender@example.com',
        'PANIC_EMAIL_PASS': 'test-password',
        'SOS_PUBLIC_BASE_URL': 'https://sos.example.org',
    }, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_expired_safety_timer_uses_the_same_sos_share_workflow(self, mock_smtp):
        self.create_contact()
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.get('/SafetyTimer')
        event = SOSEvent.objects.get(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(event.live_location_share.is_valid())
        self.assertContains(response, 'Live location:')
        message = mock_smtp.return_value.__enter__.return_value.sendmail.call_args.kwargs['msg']
        self.assertIn('https://sos.example.org/SOS/LiveLocation#', message)
