import uuid

from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.test import Client, TestCase

from .models import EmergencyContact
from .test_utils import isolate_register_test_database
from .views import ensure_register_table, get_register_db_connection


class RegistrationFlowTestCase(TestCase):
    def setUp(self):
        from . import views

        isolate_register_test_database(self)

        self.client = Client()
        self.username = 'registration-test-' + uuid.uuid4().hex
        ensure_register_table()
        self.addCleanup(setattr, views, 'username', None)
        self.addCleanup(setattr, views, 'email', None)
        self.addCleanup(self.delete_test_user)

    def delete_test_user(self):
        with get_register_db_connection() as connection:
            connection.execute('DELETE FROM register WHERE username = ?', (self.username,))
        EmergencyContact.objects.filter(user=self.username).delete()

    def registration_data(self, username=None):
        return {
            't1': username or self.username,
            't2': 'demo-password',
            't3': '1234567890',
            't4': 'registration-test@gmail.com',
            't5': 'Demo address',
        }

    def test_new_account_redirects_to_login_with_one_success_message(self):
        response = self.client.post('/RegisterAction', self.registration_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/UserLogin.html')
        login_page = self.client.get(response.url)
        success_message = 'Account created successfully! Please log in with your new credentials.'
        self.assertContains(login_page, success_message)
        self.assertEqual(login_page.content.decode('utf-8').count(success_message), 1)
        self.assertNotContains(login_page, 'already exists')

        subsequent_login_page = self.client.get('/UserLogin.html')
        self.assertNotContains(subsequent_login_page, success_message)

    def test_existing_username_reports_duplicate_without_creating_another_account(self):
        with get_register_db_connection() as connection:
            connection.execute(
                'INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)',
                (self.username, 'existing-password', '1234567890', 'existing@gmail.com', 'Existing address'),
            )

        response = self.client.post('/RegisterAction', self.registration_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/Register.html')
        registration_page = self.client.get(response.url)
        self.assertContains(
            registration_page,
            'An account with this username already exists. Please log in or choose another username.',
        )
        with get_register_db_connection() as connection:
            rows = connection.execute(
                'SELECT username, password FROM register WHERE username = ?',
                (self.username,),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['password'], 'existing-password')

    def test_incomplete_registration_does_not_create_an_account(self):
        data = self.registration_data()
        data['t5'] = ''

        response = self.client.post('/RegisterAction', data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/Register.html')
        registration_page = self.client.get(response.url)
        self.assertContains(registration_page, 'Please complete all registration fields.')
        with get_register_db_connection() as connection:
            row = connection.execute(
                'SELECT 1 FROM register WHERE username = ?',
                (self.username,),
            ).fetchone()
        self.assertIsNone(row)

    def test_successful_registration_syncs_emergency_contact(self):
        self.client.post('/RegisterAction', self.registration_data())

        contact = EmergencyContact.objects.get(user=self.username, email='registration-test@gmail.com')
        self.assertTrue(contact.is_active)
        self.assertTrue(contact.is_primary)
        self.assertEqual(contact.phone_number, '1234567890')

    def test_newly_registered_credentials_are_hashed(self):
        self.client.post('/RegisterAction', self.registration_data())

        with get_register_db_connection() as connection:
            row = connection.execute(
                'SELECT password FROM register WHERE username = ?',
                (self.username,),
            ).fetchone()

        self.assertNotEqual(row['password'], 'demo-password')
        self.assertTrue(check_password('demo-password', row['password']))
        identify_hasher(row['password'])

    def test_newly_registered_credentials_can_log_in(self):
        self.client.post('/RegisterAction', self.registration_data())

        response = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'demo-password',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/index.html')
        home_page = self.client.get(response.url)
        self.assertContains(home_page, 'Welcome back, <span>{}</span>'.format(self.username), html=True)
        self.assertContains(home_page, 'href="/Logout"')
        self.assertEqual(self.client.session['username'], self.username)

    def test_incorrect_password_does_not_create_session(self):
        self.client.post('/RegisterAction', self.registration_data())

        response = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'wrong-password',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid username')
        self.assertNotIn('username', self.client.session)
        self.assertNotIn('is_logged_in', self.client.session)

    def test_login_and_register_pages_show_guest_navigation_with_stale_username(self):
        session = self.client.session
        session['username'] = 'stale-user'
        session['is_logged_in'] = False
        session.save()

        for url in ('/UserLogin.html', '/Register.html'):
            response = self.client.get(url)
            navigation = response.content.decode('utf-8').split('<main class="site-main">', 1)[0]
            self.assertContains(response, 'href="/UserLogin.html"')
            self.assertContains(response, 'href="/Register.html"')
            self.assertNotIn('Panic / SOS', navigation)
            self.assertNotIn('Heatmap', navigation)
            self.assertNotIn('More Features', navigation)
            self.assertNotIn('My Account', navigation)
            self.assertNotIn('Emergency Contacts', navigation)
            self.assertNotIn('My Journeys', navigation)
            self.assertNotIn('Safety Timer', navigation)
            self.assertNotIn('href="/Logout"', navigation)
            self.assertEqual(response['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0, private')

    def test_logout_clears_session_protected_routes_and_login_again_restores_navigation(self):
        with get_register_db_connection() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)',
                (self.username, make_password('demo-password'), '1234567890', 'profile@example.org', 'Demo address'),
            )
            connection.commit()

        login_response = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'demo-password',
        })
        self.assertRedirects(login_response, '/index.html')
        self.assertTrue(self.client.session['is_logged_in'])

        logout_response = self.client.get('/Logout')
        self.assertRedirects(logout_response, '/index.html')
        self.assertNotIn('username', self.client.session)
        self.assertNotIn('is_logged_in', self.client.session)
        self.assertEqual(logout_response['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0, private')

        guest_home = self.client.get('/index.html')
        self.assertEqual(guest_home.status_code, 200)
        guest_navigation = guest_home.content.decode('utf-8').split('<main class="site-main">', 1)[0]
        self.assertIn('href="/UserLogin.html"', guest_navigation)
        self.assertIn('href="/Register.html"', guest_navigation)
        self.assertNotIn('href="/Logout"', guest_navigation)

        login_page = self.client.get('/UserLogin.html')
        navigation = login_page.content.decode('utf-8').split('<main class="site-main">', 1)[0]
        self.assertNotIn('Panic / SOS', navigation)
        self.assertNotIn('href="/Logout"', navigation)

        for protected_url in ('/MyAccount', '/EmergencyContacts', '/Panic', '/SafetyTimer', '/Journeys'):
            response = self.client.get(protected_url)
            self.assertRedirects(response, '/UserLogin.html')

        login_again = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'demo-password',
        })
        self.assertRedirects(login_again, '/index.html')
        home = self.client.get('/index.html')
        navigation = home.content.decode('utf-8').split('<main class="site-main">', 1)[0]
        self.assertIn('Panic / SOS', navigation)
        self.assertIn('My Profile', navigation)
        self.assertIn('href="/MyAccount"', navigation)
        self.assertIn('href="/Logout"', navigation)

    def test_legacy_plaintext_password_can_log_in_and_is_upgraded(self):
        with get_register_db_connection() as connection:
            connection.execute(
                'INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)',
                (self.username, 'legacy-password', '1234567890', 'legacy@gmail.com', 'Legacy address'),
            )

        response = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'legacy-password',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/index.html')
        home_page = self.client.get(response.url)
        self.assertContains(home_page, 'Welcome back, <span>{}</span>'.format(self.username), html=True)
        self.assertEqual(self.client.session['username'], self.username)
        with get_register_db_connection() as connection:
            row = connection.execute(
                'SELECT password FROM register WHERE username = ?',
                (self.username,),
            ).fetchone()
        self.assertNotEqual(row['password'], 'legacy-password')
        self.assertTrue(check_password('legacy-password', row['password']))

    def test_legacy_plaintext_incorrect_password_is_not_upgraded(self):
        with get_register_db_connection() as connection:
            connection.execute(
                'INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)',
                (self.username, 'legacy-password', '1234567890', 'legacy@gmail.com', 'Legacy address'),
            )

        response = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'incorrect-password',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid username')
        with get_register_db_connection() as connection:
            row = connection.execute(
                'SELECT password FROM register WHERE username = ?',
                (self.username,),
            ).fetchone()
        self.assertEqual(row['password'], 'legacy-password')
