import os
from io import StringIO
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, Client
from django.utils import timezone

from .models import (
    EmergencyContact,
    SafetyTimer,
    SafetyCheckIn,
    SafetyCheckInAttempt,
    SOSEvent,
    SOSEmailDelivery,
    SOSNotificationAttempt,
)
from .test_utils import isolate_register_test_database


class SafetyTimerTestCase(TransactionTestCase):
    def setUp(self):
        isolate_register_test_database(self)
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

    def create_eligible_contact(self, name='Mom', email='mom@gmail.com'):
        verified_at = timezone.now()
        return EmergencyContact.objects.create(
            user=self.username,
            name=name,
            phone_number='+91 9876543210',
            email=email,
            is_active=True,
            is_primary=True,
            email_consent=True,
            sms_consent=True,
            voice_consent=True,
            email_verified_at=verified_at,
            phone_verified_at=verified_at,
        )

    def test_user_can_start_a_timer(self):
        response = self.client.post('/SafetyTimerAction', {'duration': '15'})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SafetyTimer.objects.filter(user=self.username, status=SafetyTimer.STATUS_ACTIVE).exists())

    @patch('SafetyApp.views.send_sos_emails_for_event')
    def test_followed_mode_starts_a_fixed_five_minute_timer_without_sos(self, mock_send_sos_emails):
        response = self.client.post('/SafetyTimerAction', {
            'mode': 'FOLLOWED',
            'duration': '99',
        })

        timer = SafetyTimer.objects.get(user=self.username, status=SafetyTimer.STATUS_ACTIVE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(timer.duration, 5)
        self.assertEqual(self.client.session.get('safety_timer_mode'), 'FOLLOWED')
        self.assertFalse(SOSEvent.objects.filter(user=self.username).exists())
        mock_send_sos_emails.assert_not_called()

    @patch('SafetyApp.views.get_signup_emergency_contact_for_user', return_value=None)
    def test_panic_page_offers_accessible_post_form_for_followed_mode(self, mock_signup_contact):
        response = self.client.get('/Panic')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<h3 id=\"followedModeTitle\">I'm Being Followed</h3>", html=True)
        self.assertContains(response, 'action="/SafetyTimerAction"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'name="mode" value="FOLLOWED"')
        self.assertContains(response, 'name="duration" value="5"')
        self.assertContains(response, 'not by background monitoring')

    @patch('SafetyApp.views.process_expired_safety_timer_for_user', return_value=None)
    @patch('SafetyApp.views.send_sos_emails_for_event')
    def test_followed_timer_page_shows_mode_status_countdown_and_existing_actions(
        self, mock_send_sos_emails, mock_process_expiry
    ):
        self.client.post('/SafetyTimerAction', {'mode': 'FOLLOWED'})

        response = self.client.get('/SafetyTimer')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "I'm Being Followed mode active")
        self.assertContains(response, 'Five-minute safety timer')
        self.assertContains(response, "I'M SAFE")
        self.assertContains(response, 'CANCEL TIMER')
        self.assertContains(response, 'does not provide background phone monitoring')
        mock_process_expiry.assert_called_once()
        mock_send_sos_emails.assert_not_called()

    def test_normal_timer_start_clears_followed_mode_and_keeps_requested_duration(self):
        session = self.client.session
        session['safety_timer_mode'] = 'FOLLOWED'
        session.save()

        response = self.client.post('/SafetyTimerAction', {'duration': '15'})

        timer = SafetyTimer.objects.get(user=self.username, status=SafetyTimer.STATUS_ACTIVE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(timer.duration, 15)
        self.assertNotIn('safety_timer_mode', self.client.session)

    def test_safe_confirmation_clears_followed_mode(self):
        session = self.client.session
        session['safety_timer_mode'] = 'FOLLOWED'
        session.save()
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=5,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=5),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.post('/SafetyTimerSafe')

        self.assertEqual(response.status_code, 302)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_COMPLETED)
        self.assertNotIn('safety_timer_mode', self.client.session)

    def test_sos_safe_confirmation_clears_followed_mode_and_resolves_event(self):
        session = self.client.session
        session['safety_timer_mode'] = 'FOLLOWED'
        session.save()
        event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )

        response = self.client.post('/UpdateSOSStatus', {'status': 'RESOLVED'})

        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, SOSEvent.STATUS_RESOLVED)
        self.assertNotIn('safety_timer_mode', self.client.session)

    def test_cancel_timer_clears_followed_mode(self):
        session = self.client.session
        session['safety_timer_mode'] = 'FOLLOWED'
        session.save()
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=5,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=5),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        response = self.client.post('/SafetyTimerCancel')

        self.assertEqual(response.status_code, 302)
        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_CANCELLED)
        self.assertNotIn('safety_timer_mode', self.client.session)

    def test_starting_followed_mode_requires_login(self):
        response = Client().post('/SafetyTimerAction', {
            'mode': 'FOLLOWED',
            'duration': '5',
        })

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SafetyTimer.objects.exists())

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

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_expired_timer_enters_checkin_grace_without_automatic_sos(self, mock_smtp):
        self.create_eligible_contact()
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
        self.assertEqual(timer.safety_checkins.get().status, SafetyCheckIn.STATUS_DUE)
        self.assertFalse(SOSEvent.objects.filter(user=self.username).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_scheduler_processes_expiry_without_page_revisit_or_automatic_sos(self, mock_smtp):
        self.create_eligible_contact(name='Emergency Contact', email='contact@gmail.com')
        timer = SafetyTimer.objects.create(
            user=self.username,
            duration=5,
            started_at=timezone.now() - timedelta(minutes=7),
            expires_at=timezone.now() - timedelta(minutes=2),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        call_command('process_expired_safety_timers', stdout=StringIO())

        timer.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_EXPIRED)
        self.assertEqual(timer.safety_checkins.get().status, SafetyCheckIn.STATUS_DUE)
        self.assertFalse(SOSEvent.objects.filter(user=self.username).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL', side_effect=OSError('private SMTP detail'))
    def test_scheduler_does_not_send_guardian_email_before_grace(self, mock_smtp):
        self.create_eligible_contact(name='Emergency Contact', email='contact@gmail.com')
        SafetyTimer.objects.create(
            user=self.username,
            duration=5,
            started_at=timezone.now() - timedelta(minutes=7),
            expires_at=timezone.now() - timedelta(minutes=2),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        call_command('process_expired_safety_timers', stdout=StringIO())

        timer = SafetyTimer.objects.get(user=self.username)
        self.assertEqual(timer.safety_checkins.get().status, SafetyCheckIn.STATUS_DUE)
        self.assertFalse(SOSEvent.objects.filter(user=self.username).exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_repeated_scheduler_runs_do_not_duplicate_checkin_reminders(self, mock_smtp):
        self.create_eligible_contact(
            name='Emergency Contact',
            email='contact@gmail.com',
        )
        SafetyTimer.objects.create(
            user=self.username,
            duration=5,
            started_at=timezone.now() - timedelta(minutes=7),
            expires_at=timezone.now() - timedelta(minutes=2),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        call_command('process_expired_safety_timers', stdout=StringIO())
        call_command('process_expired_safety_timers', stdout=StringIO())

        self.assertEqual(SOSEvent.objects.filter(user=self.username).count(), 0)
        self.assertEqual(SafetyCheckInAttempt.objects.filter(
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
        ).count(), 1)
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_timer_scheduler_does_not_dispatch_legacy_pending_email_rows(self, mock_smtp):
        contact = EmergencyContact.objects.create(
            user=self.username,
            name='Emergency Contact',
            phone_number='0000000000',
            email='contact@gmail.com',
            is_active=True,
            is_primary=True,
        )
        event = SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        delivery = SOSEmailDelivery.objects.create(
            sos_event=event,
            emergency_contact=contact,
            status=SOSEmailDelivery.STATUS_PENDING,
        )

        call_command('process_expired_safety_timers', stdout=StringIO())

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, SOSEmailDelivery.STATUS_PENDING)
        self.assertEqual(SOSEvent.objects.filter(user=self.username).count(), 1)
        self.assertFalse(SOSNotificationAttempt.objects.exists())
        mock_smtp.assert_not_called()

    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_expiration_cannot_duplicate_checkin_reminders(self, mock_smtp):
        self.create_eligible_contact()
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now() - timedelta(minutes=20),
            expires_at=timezone.now() - timedelta(minutes=1),
            status=SafetyTimer.STATUS_ACTIVE,
        )

        self.client.get('/SafetyTimer')
        self.client.get('/SafetyTimer')

        self.assertFalse(SOSEvent.objects.filter(user=self.username).exists())
        self.assertEqual(SafetyCheckInAttempt.objects.filter(
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
        ).count(), 1)
        mock_smtp.assert_not_called()

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.com', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('SafetyApp.views.smtplib.SMTP_SSL')
    def test_timer_page_shows_checkin_due_after_expiry_without_sos(self, mock_smtp):
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
        self.assertContains(response, 'CHECK-IN DUE')
        self.assertContains(response, "I'M SAFE — CONFIRM")
        self.assertContains(response, 'CANCEL TIMER')
        self.assertFalse(SOSEvent.objects.filter(user=self.username).exists())

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
        SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)

        self.client.get('/SafetyTimer')
        response = self.client.post('/SafetyTimerSafe')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SOSEvent.objects.filter(user=self.username, status=SOSEvent.STATUS_RESOLVED).exists())
        self.assertTrue(SafetyTimer.objects.filter(user=self.username, status=SafetyTimer.STATUS_COMPLETED).exists())


class SafeRouteTestCase(TestCase):
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

    def test_dashboard_layout_preserves_navigation_and_safety_features(self):
        legacy_dashboard_response = self.client.get('/Dashboard')
        self.assertRedirects(legacy_dashboard_response, '/index.html')
        response = self.client.get('/index.html')

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        navigation = content.split('<main class="site-main">', 1)[0]
        self.assertIn('<header id="header" class="site-header">', navigation)
        self.assertIn('<summary>More Features</summary>', navigation)
        self.assertIn('>{}</summary>'.format(self.username), navigation)
        for destination in (
            '/index.html', '/Panic', '/SafetyTimer',
            '/EmergencyContacts', '/Journeys', '/Route', '/Heatmap',
            '/CrimePredict', '/TrainML', '/CommunityReports',
            '/Logout',
        ):
            self.assertIn('href="{}"'.format(destination), navigation)
        self.assertNotIn('href="/Dashboard"', navigation)
        self.assertNotIn('href="/UserLogin.html"', navigation)
        self.assertNotIn('href="/Register.html"', navigation)
        self.assertContains(response, 'class="home-hero"')
        self.assertContains(
            response,
            'Welcome back, <span>{}</span>'.format(self.username),
        )
        self.assertContains(response, 'id="featureTitle"')
        self.assertContains(response, 'id="snapshotTitle"')
        self.assertContains(response, 'id="activityTitle"')
        self.assertContains(response, 'href="/SafetyTimer"')
        self.assertContains(response, 'href="/Journeys"')
        self.assertContains(response, 'href="/CommunityReports"')

    def test_train_neural_network_uses_a_centered_page_title(self):
        response = self.client.get('/TrainML')

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('<body class="site-body train-page">', content)
        self.assertIn('href="/static/site-layout.css', content)
        self.assertIn('class="train-title" id="trainTitle">Neural Network Accuracy Graph</h1>', content)
        self.assertNotIn('class="train-hero"', content)
        self.assertContains(response, 'class="train-timer-card"')
        self.assertContains(response, 'Quick access to the personal safety timer and its existing SOS escalation flow.')
        self.assertContains(response, 'Open Safety Timer')
        self.assertContains(response, 'My Journeys')
        self.assertContains(response, 'Community Reports')
        self.assertContains(response, 'class="train-chart"')
        self.assertContains(response, 'data:image/png;base64,')
        self.assertContains(response, 'Neural network training and validation accuracy by epoch')
        self.assertLess(content.index('id="trainTitle"'), content.index('class="train-timer-card"'))
        self.assertLess(content.index('class="train-timer-card"'), content.index('class="train-chart"'))
        navigation = content.split('<main class="site-main">', 1)[0]
        for destination in ('/TrainML', '/Route', '/CrimePredict', '/Panic', '/Heatmap', '/Logout'):
            self.assertIn('href="{}"'.format(destination), navigation)

    def test_route_page_explains_nearby_facilities_loading_retry_and_distance(self):
        response = self.client.get('/Route')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="nearbyFacilitiesStatus"')
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'id="retryNearbyFacilitiesButton"')
        self.assertContains(response, 'Searching OpenStreetMap for nearby safety facilities')
        self.assertContains(response, 'nearby-facility search timed out')
        self.assertContains(response, 'No mapped safety facilities were found within 5 km')
        self.assertContains(response, 'Approx. straight-line distance')
        self.assertContains(response, 'Choose a category above to filter map markers')
        self.assertContains(response, "distanceText.textContent = 'Approx. straight-line distance: '")
        self.assertContains(response, "directionsButton.textContent = 'DIRECTIONS'")
        self.assertContains(response, "police: '#8c0209'")

    def test_route_requires_login(self):
        client = Client()
        response = client.get('/Route')

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, '/UserLogin.html')

    def test_shared_header_and_navigation_render_on_dashboard_timer_route_and_reports(self):
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=15),
            status=SafetyTimer.STATUS_ACTIVE,
        )
        responses = (
            self.client.get('/index.html'),
            self.client.get('/SafetyTimer'),
            self.client.get('/Route'),
            self.client.get('/CommunityReports'),
        )
        expected_destinations = (
            '/index.html', '/Panic', '/SafetyTimer',
            '/EmergencyContacts', '/Journeys', '/Route', '/Heatmap',
            '/CrimePredict', '/TrainML', '/CommunityReports',
            '/Logout',
        )

        for response in responses:
            self.assertEqual(response.status_code, 200)
            content = response.content.decode()
            navigation = content.split('<main class="site-main">', 1)[0]
            self.assertIn('href="/static/default.css"', navigation)
            self.assertIn('href="/static/site-layout.css', navigation)
            self.assertIn('<header id="header" class="site-header">', navigation)
            self.assertIn('<summary>More Features</summary>', navigation)
            for destination in expected_destinations:
                self.assertIn('href="{}"'.format(destination), content)
            self.assertNotIn('href="/UserLogin.html"', navigation)
            self.assertNotIn('href="/Register.html"', navigation)

    def test_shared_header_styles_use_wrapping_and_responsive_container_geometry(self):
        from django.conf import settings

        stylesheet_path = Path(settings.BASE_DIR) / 'SafetyApp' / 'static' / 'site-layout.css'
        stylesheet = stylesheet_path.read_text(encoding='utf-8')

        self.assertIn('.site-header', stylesheet)
        self.assertIn('--site-shell-max: 1320px;', stylesheet)
        self.assertIn('--site-shell-gutter: 48px;', stylesheet)
        self.assertIn(
            'width: min(var(--site-shell-max), calc(100% - var(--site-shell-gutter)));',
            stylesheet,
        )
        self.assertIn('.site-brand h1', stylesheet)
        self.assertIn('.site-navigation #main', stylesheet)
        self.assertIn('flex-wrap: wrap;', stylesheet)
        self.assertIn('grid-template-columns: repeat(2, minmax(0, 1fr));', stylesheet)
        self.assertIn('.site-main', stylesheet)

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
