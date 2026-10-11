import uuid

from django.test import Client, TestCase

from .models import EmergencyContact
from .views import ensure_register_table, get_register_db_connection


class RegistrationFlowTestCase(TestCase):
    def setUp(self):
        from . import views

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

    def test_newly_registered_credentials_can_log_in(self):
        self.client.post('/RegisterAction', self.registration_data())

        response = self.client.post('/UserLoginAction', {
            't1': self.username,
            't2': 'demo-password',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Welcome ' + self.username)
        self.assertEqual(self.client.session['username'], self.username)
