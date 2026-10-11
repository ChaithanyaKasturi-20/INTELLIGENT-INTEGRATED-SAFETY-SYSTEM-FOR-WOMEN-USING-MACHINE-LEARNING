import os
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, Client
from django.utils import timezone

from .models import EmergencyContact, SafetyTimer, SOSEvent, SOSEmailDelivery


class SafetyTimerTestCase(TestCase):
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

    def test_user_can_start_a_timer(self):
        response = self.client.post('/SafetyTimerAction', {'duration': '15'})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SafetyTimer.objects.filter(user=self.username, status=SafetyTimer.STATUS_ACTIVE).exists())

    def test_timer_is_associated_with_the_correct_user(self):
        created = SafetyTimer.objects.create(
            user=self.username,
            duration=30,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=30),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        self.assertEqual(created.user, self.username)
        self.assertTrue(SafetyTimer.objects.filter(pk=created.pk, user=self.username).exists())

    def test_user_cannot_access_another_users_timer(self):
        SafetyTimer.objects.create(
            user='bob',
            duration=15,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=15),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.get('/SafetyTimer')
        self.assertEqual(response.status_code, 302)

    def test_only_one_active_timer_can_exist(self):
        self.client.post('/SafetyTimerAction', {'duration': '15'})
        self.client.post('/SafetyTimerAction', {'duration': '30'})

        self.assertEqual(SafetyTimer.objects.filter(user=self.username, status=SafetyTimer.STATUS_ACTIVE).count(), 1)

    def test_i_am_safe_completes_the_timer(self):
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=15),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.post('/SafetyTimerSafe')

        self.assertEqual(response.status_code, 302)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_COMPLETED)

    def test_cancel_marks_the_timer_cancelled(self):
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=30,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=30),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.post('/SafetyTimerCancel')

        self.assertEqual(response.status_code, 302)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_CANCELLED)

    def test_expired_timer_becomes_expired(self):
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.get('/SafetyTimer')

        self.assertEqual(response.status_code, 200)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_EXPIRED)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_expired_timer_triggers_existing_sos_workflow(self, mock_smtp):
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.get('/SafetyTimer')

        self.assertEqual(response.status_code, 200)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_EXPIRED)
        self.assertTrue(SOSEvent.objects.filter(user=self.username, status=SOSEvent.STATUS_ACTIVE).exists())
        self.assertTrue(SOSEmailDelivery.objects.filter(emergency_contact=contact).exists())
        self.assertEqual(mock_smtp.call_count, 1)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_expiration_cannot_trigger_duplicate_sos_events(self, mock_smtp):
        EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        self.client.get('/SafetyTimer')
        self.client.get('/SafetyTimer')

        self.assertEqual(SOSEvent.objects.filter(user=self.username).count(), 1)
        self.assertEqual(SOSEmailDelivery.objects.filter(sos_event__user=self.username).count(), 1)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_timer_page_shows_active_sos_after_expiry(self, mock_smtp):
        EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.get('/SafetyTimer')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Status:')
        self.assertContains(response, 'SOS ACTIVE')
        self.assertContains(response, 'I Am Safe')
        self.assertNotContains(response, 'CANCEL TIMER')

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_i_am_safe_resolves_existing_sos_from_timer_page(self, mock_smtp):
        EmergencyContact.objects.create(
            user=self.username,
            name='Mom',
            phone_number='+91 9876543210',
            email='mom@gmail.com',
            is_active=True,
            is_primary=True,
        )
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        self.client.get('/SafetyTimer')
        response = self.client.post('/SafetyTimerSafe')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SOSEvent.objects.filter(user=self.username, status=SOSEvent.STATUS_RESOLVED).exists())
        self.assertTrue(SafetyTimer.objects.filter(user=self.username, status=SafetyTimer.STATUS_COMPLETED).exists())


class SafeRouteTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.username = 'alice'
        self.email = 'alice@gmail.com'

        session = self.client.session
        session['username'] = self.username
        session['email'] = self.email
        session['is_logged_in'] = True
        session.save()

    def test_route_page_loads_for_authenticated_user(self):
        response = self.client.get('/Route')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Safe Places')
        self.assertContains(response, 'Detecting your current location...')
        self.assertContains(response, 'leaflet')
        self.assertContains(response, 'tile.openstreetmap.org')
        self.assertContains(response, 'START NAVIGATION')
        self.assertContains(response, 'Driving')
        self.assertContains(response, 'ETA')
        self.assertNotContains(response, 'google.com/maps/dir')

    def test_route_requires_login(self):
        client = Client()
        response = client.get('/Route')

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, '/UserLogin.html')

    def test_route_action_renders_leaflet_osm_route_state(self):
        response = self.client.post('/RouteAction', {
            'destination': 'Connaught Place, New Delhi',
            'origin_lat': '28.6200',
            'origin_lng': '77.2090',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Connaught Place, New Delhi')
        self.assertContains(response, 'leaflet')
        self.assertContains(response, 'tile.openstreetmap.org')
        self.assertNotContains(response, 'google.com/maps/embed/v1/directions')

    def test_route_page_shows_missing_location_message_when_coordinates_are_missing(self):
        response = self.client.post('/RouteAction', {
            'destination': 'Connaught Place, New Delhi',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Unable to access your current location. Please allow location access to use Safe Places.')
        self.assertNotContains(response, 'GOOGLE_MAPS_EMBED_API_KEY')

    def test_route_page_includes_journey_end_and_timer_controls(self):
        response = self.client.get('/Route')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Start Journey Monitoring')
        self.assertContains(response, 'End Journey')
        self.assertContains(response, 'Start Safety Timer')
        self.assertContains(response, 'SafetyTimerAction')
