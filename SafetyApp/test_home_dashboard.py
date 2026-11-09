import sqlite3
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone
import pandas as pd

from .models import (
    CommunityReport,
    EmergencyContact,
    Journey,
    JourneyActivity,
    SafetyTimer,
    SOSEvent,
)


class HomeDashboardTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.username = 'dashboard-test-user'
        session = self.client.session
        session['username'] = self.username
        session['is_logged_in'] = True
        session.save()

    def test_guest_navigation_offers_login_and_registration_only(self):
        guest_client = Client()
        response = guest_client.get('/index.html')
        navigation = response.content.decode().split('<main class="site-main">', 1)[0]

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/UserLogin.html">Login</a>', navigation)
        self.assertIn('href="/Register.html">Register</a>', navigation)
        self.assertNotIn('href="/Logout"', navigation)
        self.assertNotIn('href="/Dashboard"', navigation)
        self.assertNotContains(response, 'id="snapshotTitle"')
        self.assertNotContains(response, 'dashboardLocationMap')
        self.assertContains(response, 'href="/UserLogin.html">Login</a>')
        self.assertContains(response, 'href="/Register.html">Register</a>')
        self.assertContains(response, 'href="/CrimePredict"')
        self.assertContains(response, 'href="/Heatmap"')
        self.assertContains(response, 'href="/CommunityReports"')
        self.assertNotIn('class="home-feature-card" href="/Route"', response.content.decode())
        self.assertNotIn('class="home-feature-card home-feature-card-emergency" href="/Panic"', response.content.decode())
        self.assertNotIn('class="home-feature-card" href="/SafetyTimer"', response.content.decode())
        for protected_url in ('/Route', '/Panic', '/SafetyTimer'):
            self.assertRedirects(guest_client.get(protected_url), '/UserLogin.html')
        self.assertEqual(guest_client.get('/CrimePredict').status_code, 200)
        self.assertEqual(guest_client.get('/Heatmap').status_code, 200)
        self.assertEqual(guest_client.get('/CommunityReports').status_code, 200)

    def test_guest_and_authenticated_home_share_the_same_visual_shell(self):
        guest_response = Client().get('/')
        authenticated_response = self.client.get('/')

        self.assertEqual(guest_response.status_code, 200)
        self.assertEqual(authenticated_response.status_code, 200)
        for response in (guest_response, authenticated_response):
            self.assertContains(response, 'class="site-body home-dashboard"')
            self.assertContains(response, 'class="home-hero" id="account-overview"')
            self.assertContains(response, 'images/womens-safety-hero.png')
            self.assertContains(response, 'class="dashboard-footer-inner"')

        self.assertNotContains(guest_response, 'class="home-dashboard-panel')
        self.assertNotContains(guest_response, 'id="dashboardLocationMap"')
        self.assertNotContains(guest_response, 'id="safetyCheckInNotice"')
        self.assertNotIn('journey_count', guest_response.context)
        self.assertContains(authenticated_response, 'class="home-dashboard-panel')
        self.assertContains(authenticated_response, 'id="dashboardLocationMap"')
        self.assertContains(authenticated_response, 'id="safetyCheckInNotice"')
        self.assertContains(authenticated_response, 'Notification.requestPermission()')

    def test_authenticated_dropdowns_separate_features_from_account_links(self):
        response = self.client.get('/index.html')
        navigation = response.content.decode().split('<main class="site-main">', 1)[0]

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/SafetyTimer">Safety Timer</a>', navigation)
        self.assertIn('href="/CommunityReports">Community Reports</a>', navigation)
        self.assertIn('href="/TrainML">Train Neural Network</a>', navigation)
        self.assertIn('href="/MyAccount">My Profile</a>', navigation)
        self.assertIn('href="/EmergencyContacts">Emergency Contacts</a>', navigation)
        self.assertIn('href="/Journeys">My Journeys</a>', navigation)
        self.assertIn('href="/Logout">Logout</a>', navigation)
        self.assertNotIn('>Settings</a>', navigation)
        self.assertNotIn('>My Account</a>', navigation)

        more_features, account = navigation.split(
            '<summary class="site-account-summary">', 1
        )
        self.assertNotIn('Emergency Contacts', more_features)
        self.assertNotIn('My Journeys', more_features)
        self.assertNotIn('Safety Timer', account)
        self.assertNotIn('Community Reports', account)

    def test_authenticated_home_shows_owned_stats_and_records(self):
        now = timezone.now()
        EmergencyContact.objects.create(
            user=self.username,
            name='Synthetic contact',
            phone_number='+1 555 010 1234',
            email='contact@example.net',
        )
        Journey.objects.create(
            user=self.username,
            origin_label='Synthetic origin',
            origin_latitude=10,
            origin_longitude=20,
            destination_label='Synthetic destination',
            destination_latitude=11,
            destination_longitude=21,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=now + timedelta(minutes=5),
            expected_arrival=now + timedelta(minutes=15),
        )
        CommunityReport.objects.create(
            reporter=self.username,
            category=CommunityReport.CATEGORY_LIGHTING,
            description='Synthetic test report',
            approximate_latitude=10,
            approximate_longitude=20,
            incident_at=now - timedelta(minutes=20),
        )
        SOSEvent.objects.create(user=self.username, status=SOSEvent.STATUS_ACTIVE)
        SafetyTimer.objects.create(
            user=self.username,
            duration=15,
            started_at=now,
            expires_at=now + timedelta(minutes=15),
            status=SafetyTimer.STATUS_ACTIVE,
        )
        journey = Journey.objects.get(user=self.username)
        JourneyActivity.objects.create(
            journey=journey,
            action=JourneyActivity.ACTION_STARTED,
            occurred_at=now,
        )
        EmergencyContact.objects.create(
            user='another-dashboard-user',
            name='Other contact',
            phone_number='+1 555 010 4321',
            email='other@example.net',
        )

        response = self.client.get('/index.html')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['emergency_contact_count'], 1)
        self.assertEqual(response.context['journey_count'], 1)
        self.assertEqual(response.context['community_report_count'], 1)
        self.assertTrue(response.context['has_active_sos'])
        self.assertIsNotNone(response.context['active_safety_timer'])
        self.assertEqual(len(response.context['recent_activities']), 5)
        self.assertContains(response, 'Welcome back, <span>{}</span>'.format(self.username), html=True)
        self.assertContains(response, 'Journey: Started')
        self.assertContains(response, 'Active alert')
        self.assertNotContains(response, 'Synthetic test report')
        self.assertNotContains(response, 'another-dashboard-user')

    def test_dashboard_alias_redirects_to_home(self):
        response = self.client.get('/Dashboard')

        self.assertRedirects(response, '/index.html')

    def test_crime_prediction_image_is_centered_and_responsive(self):
        response = self.client.get('/CrimePredict')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'width: min(100%, 800px)')
        self.assertContains(response, 'height: auto')
        self.assertContains(response, 'object-fit: contain')
        self.assertContains(response, 'images/tra.jpg')
        self.assertContains(response, 'name="t1"')
        self.assertContains(response, 'action="/CrimePredictAction"')

    def test_crime_prediction_result_is_not_a_second_dashboard(self):
        test_dataset = pd.DataFrame({
            'State': ['Synthetic State'],
            'Rape': [12],
        })
        with patch('SafetyApp.views.dataset', test_dataset):
            response = self.client.post('/CrimePredictAction', {'t1': 'Synthetic State'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Prediction result')
        self.assertContains(response, 'Synthetic State')
        self.assertContains(response, '12')
        self.assertContains(response, 'href="/CrimePredict">Run another prediction</a>')
        self.assertNotContains(response, 'id="featureTitle"')
        self.assertNotContains(response, 'id="dashboardTimerTitle"')

    def test_invalid_crime_prediction_state_shows_an_escaped_error(self):
        with patch('SafetyApp.views.dataset', pd.DataFrame({
            'State': ['Synthetic State'],
            'Rape': [12],
        })):
            response = self.client.post('/CrimePredictAction', {'t1': '<script>bad()</script>'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose a valid state from the available options.')
        self.assertNotContains(response, '<script>bad()</script>')

    def test_login_logout_and_refresh_update_account_navigation_without_real_database(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute(
            'CREATE TABLE register (username TEXT PRIMARY KEY, password TEXT, email TEXT)'
        )
        connection.execute(
            'INSERT INTO register (username, password, email) VALUES (?, ?, ?)',
            ('login-test-user', make_password('synthetic-password'), 'test@example.net'),
        )
        self.addCleanup(connection.close)

        with patch('SafetyApp.views.ensure_register_table'), patch(
            'SafetyApp.views.get_register_db_connection',
            return_value=connection,
        ):
            login_response = self.client.post('/UserLoginAction', {
                't1': 'login-test-user',
                't2': 'synthetic-password',
            })

        self.assertRedirects(login_response, '/index.html')
        home_page = self.client.get(login_response.url)
        authenticated_navigation = home_page.content.decode().split(
            '<main class="site-main">', 1
        )[0]
        self.assertIn('>login-test-user</summary>', authenticated_navigation)
        self.assertIn('href="/MyAccount">My Profile</a>', authenticated_navigation)
        self.assertIn('href="/EmergencyContacts">Emergency Contacts</a>', authenticated_navigation)
        self.assertIn('href="/Journeys">My Journeys</a>', authenticated_navigation)
        self.assertIn('href="/Logout">Logout</a>', authenticated_navigation)
        self.assertNotIn('>Safety Timer</a>', authenticated_navigation.split(
            '<summary class="site-account-summary">', 1
        )[1])
        self.assertNotIn('>Settings</a>', authenticated_navigation)
        self.assertNotIn('>My Account</a>', authenticated_navigation)
        self.assertNotIn('href="/UserLogin.html"', authenticated_navigation)
        self.assertNotIn('href="/Register.html"', authenticated_navigation)

        logout_response = self.client.get('/Logout')
        self.assertRedirects(logout_response, '/index.html')
        refreshed_home = self.client.get('/index.html')
        logged_out_navigation = refreshed_home.content.decode().split(
            '<main class="site-main">', 1
        )[0]
        self.assertIn('href="/UserLogin.html">Login</a>', logged_out_navigation)
        self.assertIn('href="/Register.html">Register</a>', logged_out_navigation)
        self.assertNotIn('login-test-user', logged_out_navigation)
        self.assertNotIn('href="/Logout"', logged_out_navigation)
