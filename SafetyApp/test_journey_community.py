import io
import os
import uuid
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .journey_notifications import process_overdue_journeys
from .models import (
    CommunityReport,
    CommunityReportFlag,
    EmergencyContact,
    Journey,
    JourneyActivity,
    JourneyCheckInAlert,
    JourneyCheckInDelivery,
    SafetyCheckIn,
    SafetyCheckInAttempt,
    SOSEvent,
)
from .views import ensure_register_table
from .test_utils import isolate_register_test_database


class JourneyFeatureTestCase(TestCase):
    def setUp(self):
        isolate_register_test_database(self)
        ensure_register_table()
        self.client = Client()
        self.owner = 'journey-owner'
        session = self.client.session
        session['username'] = self.owner
        session['email'] = 'owner@example.test'
        session['is_logged_in'] = True
        session.save()
        self.contact = EmergencyContact.objects.create(
            user=self.owner,
            name='Trusted Contact',
            phone_number='+1 555 555 0100',
            email='trusted@example.test',
            is_active=True,
            email_consent=True,
            email_verified_at=timezone.now(),
        )

    def valid_plan(self, **overrides):
        departure = timezone.now() + timedelta(minutes=5)
        arrival = departure + timedelta(minutes=35)
        data = {
            'origin_label': 'Starting place',
            'destination_label': 'Destination place',
            'origin_latitude': '40.7128',
            'origin_longitude': '-74.0060',
            'destination_latitude': '40.7306',
            'destination_longitude': '-73.9352',
            'distance_meters': '8000',
            'duration_seconds': '1800',
            'planned_departure': departure.strftime('%Y-%m-%dT%H:%M'),
            'expected_arrival': arrival.strftime('%Y-%m-%dT%H:%M'),
            'timezone_offset_minutes': '0',
            'guardian_enabled': 'true',
            'guardian_contact_ids': [str(self.contact.pk)],
        }
        data.update(overrides)
        return data

    def test_user_can_save_plan_with_only_their_active_email_contacts(self):
        response = self.client.post('/Journeys/Create', self.valid_plan())
        self.assertEqual(response.status_code, 201)
        journey = Journey.objects.get(pk=response.json()['id'])
        self.assertEqual(journey.user, self.owner)
        self.assertEqual(list(journey.guardian_contacts.all()), [self.contact])
        self.assertEqual(journey.activities.get().action, JourneyActivity.ACTION_PLANNED)

    def test_unauthenticated_users_cannot_create_or_view_owned_journeys(self):
        anonymous = Client()
        response = anonymous.post('/Journeys/Create', self.valid_plan())
        self.assertEqual(response.status_code, 401)
        response = anonymous.get('/Journeys')
        self.assertEqual(response.status_code, 302)

    def test_user_cannot_select_another_users_contact(self):
        other_contact = EmergencyContact.objects.create(
            user='other-user',
            name='Other contact',
            phone_number='+1 555 555 0101',
            email='other@example.test',
        )
        response = self.client.post(
            '/Journeys/Create',
            self.valid_plan(guardian_contact_ids=[str(other_contact.pk)]),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Journey.objects.exists())

    def test_journey_actions_are_scoped_to_the_signed_in_session_owner(self):
        journey = Journey.objects.create(
            user='different-user',
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=timezone.now(),
            expected_arrival=timezone.now() + timedelta(minutes=20),
        )
        response = self.client.post('/Journeys/{}/Start'.format(journey.pk))
        self.assertEqual(response.status_code, 404)
        journey.refresh_from_db()
        self.assertEqual(journey.status, Journey.STATUS_PLANNED)

    def test_start_end_and_activity_are_persisted(self):
        journey = Journey.objects.create(
            user=self.owner,
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=timezone.now(),
            expected_arrival=timezone.now() + timedelta(minutes=20),
        )
        started = self.client.post('/Journeys/{}/Start'.format(journey.pk))
        self.assertEqual(started.status_code, 200)
        journey.refresh_from_db()
        self.assertEqual(journey.status, Journey.STATUS_ACTIVE)
        ended = self.client.post('/Journeys/{}/End'.format(journey.pk))
        self.assertEqual(ended.status_code, 302)
        journey.refresh_from_db()
        self.assertEqual(journey.status, Journey.STATUS_COMPLETED)
        self.assertEqual(journey.activities.count(), 2)

    def test_safe_checkin_and_deadline_extension_do_not_activate_sos(self):
        journey = Journey.objects.create(
            user=self.owner,
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=timezone.now(),
            expected_arrival=timezone.now() - timedelta(minutes=1),
            status=Journey.STATUS_OVERDUE,
        )
        response = self.client.post('/Journeys/{}/Safe'.format(journey.pk))
        self.assertEqual(response.status_code, 302)
        journey.refresh_from_db()
        self.assertEqual(journey.status, Journey.STATUS_ACTIVE)
        self.assertFalse(SOSEvent.objects.exists())
        response = self.client.post('/Journeys/{}/Extend'.format(journey.pk), {'minutes': '30'})
        self.assertEqual(response.status_code, 302)
        journey.refresh_from_db()
        self.assertGreater(journey.expected_arrival, timezone.now() + timedelta(minutes=29))
        self.assertFalse(SOSEvent.objects.exists())

    def test_journey_confirmation_does_not_resolve_sos_linked_to_another_journey(self):
        now = timezone.now()
        journey = Journey.objects.create(
            user=self.owner,
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=now,
            expected_arrival=now + timedelta(minutes=30),
            status=Journey.STATUS_UNCONFIRMED,
        )
        other_journey = Journey.objects.create(
            user=self.owner,
            origin_label='Other origin',
            origin_latitude=3,
            origin_longitude=3,
            destination_label='Other destination',
            destination_latitude=4,
            destination_longitude=4,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=now,
            expected_arrival=now + timedelta(minutes=30),
            status=Journey.STATUS_UNCONFIRMED,
        )
        event = SOSEvent.objects.create(
            user=self.owner,
            status=SOSEvent.STATUS_ACTIVE,
        )
        SafetyCheckIn.objects.create(
            journey=journey,
            sequence=1,
            scheduled_at=now - timedelta(minutes=15),
            grace_deadline=now - timedelta(minutes=1),
            status=SafetyCheckIn.STATUS_UNCONFIRMED,
        )
        SafetyCheckIn.objects.create(
            journey=other_journey,
            sequence=1,
            scheduled_at=now - timedelta(minutes=15),
            grace_deadline=now - timedelta(minutes=1),
            status=SafetyCheckIn.STATUS_UNCONFIRMED,
            auto_sos_event=event,
        )

        response = self.client.post('/Journeys/{}/Safe'.format(journey.pk))

        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, SOSEvent.STATUS_ACTIVE)

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': 'sender@example.test', 'PANIC_EMAIL_PASS': 'secret'}, clear=False)
    @patch('smtplib.SMTP_SSL')
    def test_overdue_scheduler_notifies_once_without_creating_sos(self, mock_smtp):
        mock_smtp.return_value.__enter__.return_value.send_message.return_value = {}
        journey = Journey.objects.create(
            user=self.owner,
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=timezone.now() - timedelta(hours=1),
            expected_arrival=timezone.now() - timedelta(minutes=5),
            status=Journey.STATUS_ACTIVE,
            guardian_enabled=True,
            grace_period_minutes=5,
        )
        journey.guardian_contacts.add(self.contact)

        first = process_overdue_journeys()
        second = process_overdue_journeys()

        journey.refresh_from_db()
        self.assertEqual(journey.status, Journey.STATUS_UNCONFIRMED)
        self.assertEqual(first['overdue_journeys'], 1)
        self.assertEqual(first['unconfirmed_journeys'], 1)
        self.assertEqual(first['sent_notifications'], 1)
        self.assertEqual(second['overdue_journeys'], 0)
        self.assertEqual(mock_smtp.call_count, 1)
        self.assertEqual(SafetyCheckIn.objects.count(), 1)
        self.assertEqual(
            SafetyCheckInAttempt.objects.get(kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL).status,
            SafetyCheckInAttempt.STATUS_ACCEPTED,
        )
        self.assertFalse(SOSEvent.objects.exists())

    @patch.dict(os.environ, {'PANIC_EMAIL_USER': '', 'PANIC_EMAIL_PASS': ''}, clear=False)
    def test_unconfigured_smtp_is_audited_and_not_retried(self):
        journey = Journey.objects.create(
            user=self.owner,
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=timezone.now() - timedelta(hours=1),
            expected_arrival=timezone.now() - timedelta(minutes=5),
            status=Journey.STATUS_ACTIVE,
            guardian_enabled=True,
            grace_period_minutes=5,
        )
        journey.guardian_contacts.add(self.contact)

        first_result = process_overdue_journeys()
        second_result = process_overdue_journeys()

        self.assertEqual(first_result['failed_notifications'], 1)
        self.assertEqual(second_result['failed_notifications'], 0)
        self.assertEqual(first_result['unconfirmed_journeys'], 1)
        self.assertEqual(
            SafetyCheckInAttempt.objects.get(kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL).status,
            SafetyCheckInAttempt.STATUS_FAILED,
        )

    def test_scheduler_does_not_process_journeys_without_guardian_opt_in(self):
        Journey.objects.create(
            user=self.owner,
            origin_label='Origin',
            origin_latitude=1,
            origin_longitude=1,
            destination_label='Destination',
            destination_latitude=2,
            destination_longitude=2,
            distance_meters=1000,
            estimated_duration_seconds=600,
            planned_departure=timezone.now() - timedelta(hours=1),
            expected_arrival=timezone.now() - timedelta(minutes=5),
            status=Journey.STATUS_ACTIVE,
            guardian_enabled=False,
        )
        result = process_overdue_journeys()
        self.assertEqual(result['overdue_journeys'], 1)
        self.assertEqual(result['unconfirmed_journeys'], 1)
        self.assertFalse(JourneyCheckInAlert.objects.exists())
        self.assertFalse(SafetyCheckInAttempt.objects.filter(
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        ).exists())


class CommunityReportTestCase(TestCase):
    def setUp(self):
        isolate_register_test_database(self)
        ensure_register_table()
        self.client = Client()
        session = self.client.session
        session['username'] = 'reporter'
        session['is_logged_in'] = True
        session.save()

    def test_shared_navigation_exposes_feature_links_and_authenticated_account_menu(self):
        response = self.client.get('/CommunityReports')

        self.assertEqual(response.status_code, 200)
        navigation = response.content.decode().split('<main class="site-main">', 1)[0]
        self.assertIn('<header id="header" class="site-header">', navigation)
        self.assertIn('<summary>More Features</summary>', navigation)
        self.assertIn('>reporter</summary>', navigation)
        for label, path in (
            ('Community Reports', '/CommunityReports'),
            ('Safety Timer', '/SafetyTimer'),
            ('My Journeys', '/Journeys'),
            ('Train Neural Network', '/TrainML'),
            ('Emergency Contacts', '/EmergencyContacts'),
            ('Logout', '/Logout'),
        ):
            self.assertIn('href="{}">{}</a>'.format(path, label), navigation)
        for path in ('/Route', '/CrimePredict', '/Panic', '/Heatmap'):
            self.assertIn('href="{}"'.format(path), navigation)
        self.assertNotIn('href="/Dashboard"', navigation)
        self.assertNotIn('href="/UserLogin.html"', navigation)
        self.assertNotIn('href="/Register.html"', navigation)

    def valid_report(self, **overrides):
        data = {
            'submission_key': str(uuid.uuid4()),
            'category': CommunityReport.CATEGORY_LIGHTING,
            'description': 'A user-reported concern near a public location.',
            'incident_at': (timezone.now() - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            'timezone_offset_minutes': '0',
            'approximate_latitude': '40.712812',
            'approximate_longitude': '-74.006045',
        }
        data.update(overrides)
        return data

    def test_submitted_report_is_private_until_staff_approval(self):
        response = self.client.post('/CommunityReports/Submit', self.valid_report())
        self.assertEqual(response.status_code, 302)
        report = CommunityReport.objects.get()
        self.assertEqual(report.status, CommunityReport.STATUS_SUBMITTED)
        self.assertAlmostEqual(report.approximate_latitude, 40.71)
        self.assertAlmostEqual(report.approximate_longitude, -74.01)
        other_client = Client()
        self.assertNotContains(other_client.get('/CommunityReports'), report.description)
        map_data = self.client.get('/CommunityReports/MapData').json()
        self.assertEqual(map_data['reports'], [])

    def test_approved_report_is_visible_without_reporter_identity(self):
        report = CommunityReport.objects.create(
            reporter='private-identity',
            category=CommunityReport.CATEGORY_HARASSMENT,
            description='Approved public summary.',
            approximate_latitude=10.123,
            approximate_longitude=20.456,
            incident_at=timezone.now() - timedelta(days=1),
            status=CommunityReport.STATUS_APPROVED,
        )
        response = self.client.get('/CommunityReports')
        self.assertContains(response, report.description)
        self.assertContains(response, 'Approximate area: 10.12, 20.46')
        self.assertContains(response, 'Submitted:')
        self.assertNotContains(response, report.reporter)
        payload = self.client.get('/CommunityReports/MapData').json()
        self.assertEqual(payload['reports'][0]['id'], report.pk)
        self.assertNotIn('reporter', payload['reports'][0])
        self.assertIn('submitted_at', payload['reports'][0])

    def test_submitter_can_see_private_submission_status_only_on_their_account(self):
        report = CommunityReport.objects.create(
            reporter='reporter',
            category=CommunityReport.CATEGORY_OTHER,
            description='Private pending report details.',
            approximate_latitude=1,
            approximate_longitude=1,
            incident_at=timezone.now() - timedelta(hours=1),
            status=CommunityReport.STATUS_UNDER_REVIEW,
        )

        response = self.client.get('/CommunityReports')
        self.assertContains(response, report.description)
        self.assertContains(response, 'Under review')

        other_client = Client()
        other_session = other_client.session
        other_session['username'] = 'another-reporter'
        other_session['is_logged_in'] = True
        other_session.save()
        self.assertNotContains(other_client.get('/CommunityReports'), report.description)

    def test_community_feed_filters_and_paginates_real_approved_reports(self):
        incident_at = timezone.make_aware(datetime(2020, 1, 15, 12, 0))
        for index in range(11):
            CommunityReport.objects.create(
                reporter='reporter',
                category=CommunityReport.CATEGORY_HARASSMENT,
                description='Approved report {}'.format(index),
                approximate_latitude=1,
                approximate_longitude=1,
                incident_at=incident_at + timedelta(minutes=index),
                status=CommunityReport.STATUS_APPROVED,
            )
        CommunityReport.objects.create(
            reporter='reporter',
            category=CommunityReport.CATEGORY_OTHER,
            description='Different category',
            approximate_latitude=1,
            approximate_longitude=1,
            incident_at=incident_at,
            status=CommunityReport.STATUS_APPROVED,
        )

        response = self.client.get('/CommunityReports', {
            'category': CommunityReport.CATEGORY_HARASSMENT,
            'date_from': '2020-01-15',
            'date_to': '2020-01-15',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['reports_page'].paginator.count, 11)
        self.assertEqual(len(response.context['reports']), 10)
        self.assertContains(response, 'Page 1 of 2')
        self.assertContains(response, 'Next')
        map_reports = self.client.get('/CommunityReports/MapData', {
            'category': CommunityReport.CATEGORY_HARASSMENT,
            'date_from': '2020-01-15',
            'date_to': '2020-01-15',
        }).json()['reports']
        self.assertEqual(len(map_reports), 11)

        second_page = self.client.get('/CommunityReports', {
            'category': CommunityReport.CATEGORY_HARASSMENT,
            'date_from': '2020-01-15',
            'date_to': '2020-01-15',
            'page': '2',
        })
        self.assertEqual(len(second_page.context['reports']), 1)
        self.assertContains(second_page, 'Page 2 of 2')

    def test_invalid_community_feed_filters_do_not_show_unfiltered_map_data(self):
        report = CommunityReport.objects.create(
            reporter='reporter',
            category=CommunityReport.CATEGORY_OTHER,
            description='Approved report.',
            approximate_latitude=1,
            approximate_longitude=1,
            incident_at=timezone.now() - timedelta(hours=1),
            status=CommunityReport.STATUS_APPROVED,
        )

        response = self.client.get('/CommunityReports', {'category': 'invalid'})
        self.assertEqual(response.context['reports_page'].paginator.count, 0)
        self.assertContains(response, 'Choose a valid report category.')
        map_response = self.client.get('/CommunityReports/MapData', {'category': 'invalid'})
        self.assertEqual(map_response.status_code, 400)
        self.assertEqual(map_response.json()['reports'], [])
        self.assertEqual(CommunityReport.objects.filter(pk=report.pk).count(), 1)

    def test_repeated_submission_key_creates_only_one_report(self):
        form_response = self.client.get('/CommunityReports')
        submission_key = form_response.context['submission_key']
        payload = self.valid_report(submission_key=str(submission_key))

        first_response = self.client.post('/CommunityReports/Submit', payload)
        repeated_response = self.client.post('/CommunityReports/Submit', payload, follow=True)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(CommunityReport.objects.count(), 1)
        self.assertContains(repeated_response, 'This report was already received')

    def test_database_failure_does_not_confirm_or_create_a_report(self):
        payload = self.valid_report()
        with patch.object(CommunityReport, 'save', side_effect=DatabaseError('synthetic database failure')):
            response = self.client.post('/CommunityReports/Submit', payload, follow=True)

        self.assertContains(response, 'We could not save your report.')
        self.assertFalse(CommunityReport.objects.exists())

    def test_report_submission_requires_a_valid_session(self):
        response = Client().post('/CommunityReports/Submit', self.valid_report(submission_key='invalid'))

        self.assertRedirects(response, '/UserLogin.html')
        self.assertFalse(CommunityReport.objects.exists())

    def test_report_coordinates_and_incident_time_are_validated(self):
        response = self.client.post('/CommunityReports/Submit', self.valid_report(approximate_latitude='100'))
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            '/CommunityReports/Submit',
            self.valid_report(incident_at=(timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommunityReport.objects.exists())

    def test_evidence_must_be_a_real_allowed_image_and_under_limit(self):
        fake_image = SimpleUploadedFile('evidence.jpg', b'not an image', content_type='image/jpeg')
        invalid_payload = self.valid_report()
        invalid_payload['evidence'] = fake_image
        response = self.client.post('/CommunityReports/Submit', invalid_payload)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommunityReport.objects.exists())

        oversized = SimpleUploadedFile('large.png', b'x' * (5 * 1024 * 1024 + 1), content_type='image/png')
        oversized_payload = self.valid_report()
        oversized_payload['evidence'] = oversized
        response = self.client.post('/CommunityReports/Submit', oversized_payload)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommunityReport.objects.exists())

    def test_valid_evidence_is_private_and_downloadable_only_by_staff(self):
        from PIL import Image

        image_data = io.BytesIO()
        Image.new('RGB', (1, 1), color='red').save(image_data, format='PNG')
        payload = self.valid_report()
        payload['evidence'] = SimpleUploadedFile('evidence.png', image_data.getvalue(), content_type='image/png')

        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = self.client.post('/CommunityReports/Submit', payload)
                self.assertEqual(response.status_code, 302)
                report = CommunityReport.objects.get()
                self.assertTrue(report.evidence.name.startswith('community-reports/'))
                self.assertEqual(self.client.get('/CommunityReports/MapData').json()['reports'], [])
                self.assertNotContains(self.client.get('/CommunityReports'), report.evidence.name)

                evidence_url = '/admin/SafetyApp/communityreport/{}/evidence/'.format(report.pk)
                self.assertEqual(self.client.get(evidence_url).status_code, 302)
                moderator = get_user_model().objects.create_superuser(
                    username='evidence-moderator',
                    email='moderator@example.test',
                    password='temporary-test-password',
                )
                staff_client = Client()
                staff_client.force_login(moderator)
                download = staff_client.get(evidence_url)
                self.assertEqual(download.status_code, 200)
                self.assertIn('private, no-store', download['Cache-Control'])
                self.assertEqual(download['X-Content-Type-Options'], 'nosniff')
                downloaded_image = b''.join(download.streaming_content)
                download.close()
                self.assertEqual(downloaded_image, image_data.getvalue())

    def test_only_approved_reports_can_be_flagged_and_duplicate_flags_are_prevented(self):
        report = CommunityReport.objects.create(
            reporter='someone-else',
            category=CommunityReport.CATEGORY_OTHER,
            description='Public summary.',
            approximate_latitude=1,
            approximate_longitude=1,
            incident_at=timezone.now() - timedelta(hours=1),
            status=CommunityReport.STATUS_APPROVED,
        )
        response = self.client.post(
            '/CommunityReports/{}/Flag'.format(report.pk),
            {'reason': 'Inaccurate information'},
        )
        self.assertEqual(response.status_code, 302)
        self.client.post(
            '/CommunityReports/{}/Flag'.format(report.pk),
            {'reason': 'Inaccurate information'},
        )
        self.assertEqual(CommunityReportFlag.objects.filter(report=report, reporter='reporter').count(), 1)

        pending = CommunityReport.objects.create(
            reporter='another-user',
            category=CommunityReport.CATEGORY_OTHER,
            description='Pending report.',
            approximate_latitude=1,
            approximate_longitude=1,
            incident_at=timezone.now() - timedelta(hours=1),
        )
        response = self.client.post(
            '/CommunityReports/{}/Flag'.format(pending.pk),
            {'reason': 'Other concern'},
        )
        self.assertEqual(response.status_code, 404)

    def test_report_owner_is_taken_from_session_not_form_input(self):
        response = self.client.post(
            '/CommunityReports/Submit',
            self.valid_report(reporter='attacker-controlled-name'),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommunityReport.objects.get().reporter, 'reporter')

    def test_staff_moderation_action_records_approver(self):
        report = CommunityReport.objects.create(
            reporter='reporter',
            category=CommunityReport.CATEGORY_OTHER,
            description='Awaiting moderation.',
            approximate_latitude=1,
            approximate_longitude=1,
            incident_at=timezone.now() - timedelta(hours=1),
        )
        admin_user = get_user_model().objects.create_superuser(
            username='moderator',
            email='moderator@example.test',
            password='temporary-test-password',
        )
        admin_client = Client()
        admin_client.force_login(admin_user)
        response = admin_client.post('/admin/SafetyApp/communityreport/', {
            'action': 'approve_reports',
            'index': '0',
            '_selected_action': [str(report.pk)],
        })
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.status, CommunityReport.STATUS_APPROVED)
        self.assertEqual(report.moderated_by, 'moderator')
        self.assertIsNotNone(report.moderated_at)
