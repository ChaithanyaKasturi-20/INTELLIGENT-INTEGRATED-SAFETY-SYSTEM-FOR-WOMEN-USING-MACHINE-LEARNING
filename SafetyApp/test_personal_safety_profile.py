import uuid
from datetime import date

from django.test import Client, TestCase

from .models import EmergencyContact, PersonalSafetyProfile
from .test_utils import isolate_register_test_database
from .views import ensure_register_table, get_register_db_connection


class PersonalSafetyProfileTestCase(TestCase):
    def setUp(self):
        isolate_register_test_database(self)

        self.username = 'profile-' + uuid.uuid4().hex
        ensure_register_table()
        with get_register_db_connection() as connection:
            connection.execute(
                'INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)',
                (self.username, 'not-used', '1234567890', 'profile@example.org', 'Synthetic address'),
            )
            connection.commit()

        self.client = Client()
        session = self.client.session
        session['username'] = self.username
        session['is_logged_in'] = True
        session.save()

    def tearDown(self):
        EmergencyContact.objects.filter(user=self.username).delete()
        PersonalSafetyProfile.objects.filter(username=self.username).delete()

    def test_profile_requires_authentication(self):
        response = Client().get('/MyAccount')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/UserLogin.html')

    def test_profile_creation_edit_and_age_calculation(self):
        response = self.client.post('/MyAccount', {
            'full_name': 'Synthetic Safety User',
            'date_of_birth': '2000-01-02',
            'gender': 'female',
            'email': 'profile@example.org',
            'mobile_number': '1234567890',
            'address': 'Synthetic address',
            'emergency_instructions': 'Call the primary contact first.',
        })
        self.assertEqual(response.status_code, 302)

        profile = PersonalSafetyProfile.objects.get(username=self.username)
        self.assertEqual(profile.full_name, 'Synthetic Safety User')
        self.assertEqual(profile.date_of_birth, date(2000, 1, 2))
        self.assertEqual(profile.age, profile.age)
        self.assertFalse(profile.medical_consent)

    def test_optional_medical_fields_require_consent_and_persist(self):
        response = self.client.post('/MyAccount', {
            'full_name': '',
            'date_of_birth': '',
            'gender': '',
            'email': 'profile@example.org',
            'mobile_number': '1234567890',
            'address': '',
            'medical_consent': 'on',
            'blood_group': 'O+',
            'allergies': 'Synthetic allergy',
            'medications': '',
            'medical_notes': 'Synthetic note',
        })
        self.assertEqual(response.status_code, 302)

        profile = PersonalSafetyProfile.objects.get(username=self.username)
        self.assertTrue(profile.medical_consent)
        self.assertEqual(profile.blood_group, 'O+')
        self.assertEqual(profile.allergies, 'Synthetic allergy')

    def test_medical_fields_are_cleared_when_consent_is_removed(self):
        profile = PersonalSafetyProfile.objects.create(
            username=self.username,
            medical_consent=True,
            blood_group='AB+',
            allergies='Keep until consent removed',
        )
        self.client.post('/MyAccount', {
            'full_name': '',
            'date_of_birth': '',
            'gender': '',
            'email': 'profile@example.org',
            'mobile_number': '1234567890',
            'address': '',
            'blood_group': 'AB+',
            'allergies': 'Keep until consent removed',
        })
        profile.refresh_from_db()
        self.assertFalse(profile.medical_consent)
        self.assertEqual(profile.blood_group, '')
        self.assertEqual(profile.allergies, '')

    def test_future_date_of_birth_is_rejected(self):
        response = self.client.post('/MyAccount', {
            'full_name': 'Synthetic Safety User',
            'date_of_birth': '2999-01-01',
            'email': 'profile@example.org',
            'mobile_number': '1234567890',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Date of birth must be in the past.')
        self.assertFalse(PersonalSafetyProfile.objects.filter(username=self.username, full_name='Synthetic Safety User').exists())

    def test_user_can_only_manage_owned_contacts_with_alternative_phone(self):
        owned = EmergencyContact.objects.create(user=self.username, name='Owned', phone_number='1234567890')
        other = EmergencyContact.objects.create(user='other-user', name='Private Other', phone_number='9876543210')

        response = self.client.post('/EmergencyContactEdit/{}'.format(owned.pk), {
            'name': 'Owned Updated',
            'phone_number': '1234567890',
            'alternative_phone_number': '1111111111',
            'relationship': 'Sibling',
            'is_primary': 'on',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        owned.refresh_from_db()
        self.assertEqual(owned.alternative_phone_number, '1111111111')

        response = self.client.post('/EmergencyContactDelete/{}'.format(other.pk), {})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EmergencyContact.objects.filter(pk=other.pk).exists())
        self.assertNotContains(self.client.get('/EmergencyContacts'), 'Private Other')
