import json
import smtplib
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from .models import (
    EmergencyContact,
    Journey,
    JourneyActivity,
    SafetyCheckIn,
    SafetyCheckInAttempt,
    SafetyTimer,
    SOSEvent,
    SOSLiveLocationShare,
)
from .safety_checkins import (
    confirm_owner_checkin,
    process_safety_checkins,
    schedule_checkin,
    update_owner_location,
)


class SafetyCheckInTestCase(TestCase):
    def setUp(self):
        self.owner = 'checkin-owner'
        self.client = Client()
        session = self.client.session
        session['username'] = self.owner
        session['is_logged_in'] = True
        session.save()

    def make_contact(self, **overrides):
        values = {
            'user': self.owner,
            'name': 'Trusted contact',
            'phone_number': '+1 555 555 0123',
            'email': 'trusted@example.test',
            'is_active': True,
            'email_consent': True,
            'email_verified_at': timezone.now(),
        }
        values.update(overrides)
        return EmergencyContact.objects.create(**values)

    def make_timer(self, now=None, **overrides):
        now = now or timezone.now()
        values = {
            'user': self.owner,
            'started_at': now - timedelta(minutes=20),
            'expires_at': now + timedelta(hours=1),
            'duration': 60,
            'status': SafetyTimer.STATUS_ACTIVE,
        }
        values.update(overrides)
        return SafetyTimer.objects.create(**values)

    def make_journey(self, now=None, **overrides):
        now = now or timezone.now()
        values = {
            'user': self.owner,
            'origin_label': 'Origin',
            'origin_latitude': 1.0,
            'origin_longitude': 2.0,
            'destination_label': 'Destination',
            'destination_latitude': 3.0,
            'destination_longitude': 4.0,
            'distance_meters': 1000,
            'estimated_duration_seconds': 600,
            'planned_departure': now,
            'expected_arrival': now + timedelta(minutes=30),
            'started_at': now,
            'status': Journey.STATUS_ACTIVE,
        }
        values.update(overrides)
        return Journey.objects.create(**values)

    def make_unconfirmed_timer(self, now=None, **timer_overrides):
        now = now or timezone.now()
        timer = self.make_timer(
            now,
            expires_at=now - timedelta(minutes=20),
            **timer_overrides
        )
        return timer, schedule_checkin(
            timer,
            due_at=now - timedelta(minutes=20),
            now=now,
        )

    def test_starting_timer_persists_expected_checkin_and_approved_defaults(self):
        response = self.client.post('/SafetyTimerAction', {'duration': '30'})

        self.assertEqual(response.status_code, 302)
        timer = SafetyTimer.objects.get(user=self.owner, status=SafetyTimer.STATUS_ACTIVE)
        checkin = timer.safety_checkins.get()
        self.assertEqual(timer.checkin_interval_minutes, 15)
        self.assertEqual(timer.grace_period_minutes, 15)
        self.assertEqual(timer.reminder_retry_minutes, 5)
        self.assertEqual(timer.max_reminder_retries, 1)
        self.assertFalse(timer.auto_sos_on_unconfirmed)
        self.assertEqual(checkin.status, SafetyCheckIn.STATUS_SCHEDULED)
        self.assertEqual(checkin.scheduled_at, timer.started_at + timedelta(minutes=15))

    def test_checkin_reminders_repeat_once_then_transition_after_grace(self):
        now = timezone.now()
        timer = self.make_timer(now)
        checkin = schedule_checkin(timer, due_at=now - timedelta(minutes=1), now=now)

        first = process_safety_checkins(now=now, owner_kind='timer', dispatch_emails=False)
        process_safety_checkins(now=now + timedelta(minutes=5), owner_kind='timer', dispatch_emails=False)
        process_safety_checkins(now=now + timedelta(minutes=10), owner_kind='timer', dispatch_emails=False)

        checkin.refresh_from_db()
        self.assertEqual(first['due_checkins'], 1)
        self.assertEqual(checkin.status, SafetyCheckIn.STATUS_DUE)
        self.assertEqual(checkin.reminder_count, 2)
        self.assertEqual(
            SafetyCheckInAttempt.objects.filter(
                checkin=checkin,
                kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
            ).count(),
            2,
        )

        process_safety_checkins(now=now + timedelta(minutes=15), owner_kind='timer', dispatch_emails=False)
        checkin.refresh_from_db()
        timer.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckIn.STATUS_UNCONFIRMED)
        self.assertEqual(timer.status, SafetyTimer.STATUS_UNCONFIRMED)

    def test_in_app_reminder_is_claimed_once_and_presentation_is_recorded(self):
        now = timezone.now()
        timer = self.make_timer(now)
        checkin = schedule_checkin(timer, due_at=now - timedelta(minutes=1), now=now)
        process_safety_checkins(
            now=now,
            owner_kind='timer',
            user=self.owner,
            dispatch_emails=False,
        )
        attempt = SafetyCheckInAttempt.objects.get(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
        )

        first_response = self.client.get('/SafetyCheckIn/Reminders')
        session_client = Client()
        session = session_client.session
        session['username'] = self.owner
        session['is_logged_in'] = True
        session.save()
        overlapping_response = session_client.get('/SafetyCheckIn/Reminders')

        reminder = first_response.json()['reminders'][0]
        self.assertEqual(overlapping_response.json()['reminders'], [])
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SafetyCheckInAttempt.STATUS_SENDING)
        receipt = self.client.post(
            '/SafetyCheckIn/Reminders',
            data=json.dumps({
                'attempt_id': reminder['attempt_id'],
                'claim_token': reminder['claim_token'],
                'claim_generation': reminder['claim_generation'],
                'delivery_channel': SafetyCheckInAttempt.DELIVERY_CHANNEL_IN_APP,
            }),
            content_type='application/json',
        )

        self.assertEqual(receipt.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SafetyCheckInAttempt.STATUS_ACCEPTED)
        self.assertEqual(
            attempt.delivery_channel,
            SafetyCheckInAttempt.DELIVERY_CHANNEL_IN_APP,
        )
        self.assertEqual(self.client.get('/SafetyCheckIn/Reminders').json()['reminders'], [])

    def test_reminder_receipt_cannot_be_recorded_by_another_account(self):
        now = timezone.now()
        timer = self.make_timer(now)
        checkin = schedule_checkin(timer, due_at=now - timedelta(minutes=1), now=now)
        process_safety_checkins(
            now=now,
            owner_kind='timer',
            user=self.owner,
            dispatch_emails=False,
        )
        reminder = self.client.get('/SafetyCheckIn/Reminders').json()['reminders'][0]
        other_client = Client()
        session = other_client.session
        session['username'] = 'another-owner'
        session['is_logged_in'] = True
        session.save()

        response = other_client.post(
            '/SafetyCheckIn/Reminders',
            data=json.dumps({
                'attempt_id': reminder['attempt_id'],
                'claim_token': reminder['claim_token'],
                'claim_generation': reminder['claim_generation'],
                'delivery_channel': SafetyCheckInAttempt.DELIVERY_CHANNEL_IN_APP,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            SafetyCheckInAttempt.objects.get(checkin=checkin).status,
            SafetyCheckInAttempt.STATUS_SENDING,
        )

    def test_unacknowledged_reminder_claim_recovers_after_scheduler_restart(self):
        now = timezone.now()
        timer = self.make_timer(now)
        checkin = schedule_checkin(timer, due_at=now - timedelta(minutes=1), now=now)
        process_safety_checkins(
            now=now,
            owner_kind='timer',
            user=self.owner,
            dispatch_emails=False,
        )
        attempt = SafetyCheckInAttempt.objects.get(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
        )
        first_reminder = self.client.get('/SafetyCheckIn/Reminders').json()['reminders'][0]
        SafetyCheckInAttempt.objects.filter(pk=attempt.pk).update(
            lease_expires_at=now - timedelta(seconds=1),
        )
        process_safety_checkins(
            now=now + timedelta(minutes=3),
            owner_kind='timer',
            user=self.owner,
            dispatch_emails=False,
        )

        retry_reminder = self.client.get('/SafetyCheckIn/Reminders').json()['reminders'][0]
        attempt.refresh_from_db()
        self.assertEqual(retry_reminder['attempt_id'], first_reminder['attempt_id'])
        self.assertGreater(retry_reminder['claim_generation'], first_reminder['claim_generation'])
        self.assertEqual(attempt.status, SafetyCheckInAttempt.STATUS_SENDING)

    def test_timer_can_be_extended_during_grace_without_old_checkin_escalation(self):
        now = timezone.now()
        timer = self.make_timer(
            now,
            expires_at=now - timedelta(minutes=5),
            status=SafetyTimer.STATUS_EXPIRED,
            auto_sos_on_unconfirmed=True,
            auto_sos_delay_minutes=1,
        )
        old_checkin = schedule_checkin(
            timer,
            due_at=now - timedelta(minutes=5),
            now=now,
        )
        process_safety_checkins(
            now=now,
            owner_kind='timer',
            user=self.owner,
            dispatch_emails=False,
        )

        response = self.client.post('/SafetyTimerExtend', {'minutes': '15'})

        self.assertEqual(response.status_code, 302)
        old_checkin.refresh_from_db()
        timer.refresh_from_db()
        self.assertEqual(old_checkin.status, SafetyCheckIn.STATUS_CANCELLED)
        self.assertEqual(timer.status, SafetyTimer.STATUS_ACTIVE)
        self.assertEqual(timer.safety_checkins.filter(status=SafetyCheckIn.STATUS_SCHEDULED).count(), 1)
        process_safety_checkins(
            now=now + timedelta(minutes=10),
            owner_kind='timer',
            user=self.owner,
            dispatch_emails=False,
        )
        self.assertFalse(SOSEvent.objects.exists())

    def test_confirmation_wins_deadline_race_in_either_serialization_order(self):
        now = timezone.now()
        confirmed_first_timer = self.make_timer(now, user='confirmed-first')
        confirmed_first = schedule_checkin(
            confirmed_first_timer,
            due_at=now - timedelta(minutes=1),
            now=now,
        )
        self.assertIsNotNone(confirm_owner_checkin(confirmed_first_timer, now))
        process_safety_checkins(
            now=now + timedelta(minutes=30),
            owner_kind='timer',
            user='confirmed-first',
            dispatch_emails=False,
        )
        confirmed_first.refresh_from_db()
        self.assertEqual(confirmed_first.status, SafetyCheckIn.STATUS_CONFIRMED)
        self.assertEqual(confirmed_first.attempts.count(), 0)

        scheduler_first_timer = self.make_timer(now, user='scheduler-first')
        scheduler_first = schedule_checkin(
            scheduler_first_timer,
            due_at=now - timedelta(minutes=1),
            now=now,
        )
        process_safety_checkins(
            now=now,
            owner_kind='timer',
            user='scheduler-first',
            dispatch_emails=False,
        )
        self.assertIsNotNone(confirm_owner_checkin(scheduler_first_timer, now + timedelta(minutes=1)))
        process_safety_checkins(
            now=now + timedelta(minutes=30),
            owner_kind='timer',
            user='scheduler-first',
            dispatch_emails=False,
        )
        scheduler_first.refresh_from_db()
        self.assertEqual(scheduler_first.status, SafetyCheckIn.STATUS_CONFIRMED)

    def test_missed_checkin_notifies_only_verified_consented_contacts_after_grace(self):
        now = timezone.now()
        contact = self.make_contact()
        timer, checkin = self.make_unconfirmed_timer(
            now,
            guardian_enabled=True,
        )
        timer.guardian_contacts.add(contact)

        with patch(
            'SafetyApp.views.get_panic_email_config',
            return_value=('sender@example.test', 'test-password'),
        ), patch('SafetyApp.safety_checkins.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            result = process_safety_checkins(now=now, owner_kind='timer')

        attempt = SafetyCheckInAttempt.objects.get(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        )
        self.assertEqual(result['unconfirmed_checkins'], 1)
        self.assertEqual(attempt.status, SafetyCheckInAttempt.STATUS_ACCEPTED)
        smtp.assert_called_once()
        body = smtp.return_value.__enter__.return_value.send_message.call_args[0][0].get_content()
        self.assertIn('not proof of an emergency', body)
        self.assertIn('Location sharing was not enabled', body)
        self.assertFalse(SOSEvent.objects.exists())

    def test_contact_without_email_consent_or_verification_is_skipped(self):
        now = timezone.now()
        contact = self.make_contact(email_consent=False, email_verified_at=None)
        timer, checkin = self.make_unconfirmed_timer(now, guardian_enabled=True)
        timer.guardian_contacts.add(contact)

        with patch('SafetyApp.safety_checkins.smtplib.SMTP_SSL') as smtp:
            process_safety_checkins(now=now, owner_kind='timer')

        attempt = SafetyCheckInAttempt.objects.get(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        )
        self.assertEqual(attempt.status, SafetyCheckInAttempt.STATUS_SKIPPED)
        smtp.assert_not_called()

    def test_smtp_authentication_failure_is_recorded_without_blind_retry(self):
        now = timezone.now()
        contact = self.make_contact()
        timer, checkin = self.make_unconfirmed_timer(now, guardian_enabled=True)
        timer.guardian_contacts.add(contact)

        with patch(
            'SafetyApp.views.get_panic_email_config',
            return_value=('sender@example.test', 'test-password'),
        ), patch('SafetyApp.safety_checkins.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = __import__('smtplib').SMTPAuthenticationError(535, b'rejected')
            process_safety_checkins(now=now, owner_kind='timer')
            process_safety_checkins(now=now + timedelta(hours=1), owner_kind='timer')

        attempts = SafetyCheckInAttempt.objects.filter(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        )
        self.assertEqual(attempts.count(), 1)
        self.assertEqual(attempts.get().status, SafetyCheckInAttempt.STATUS_FAILED)
        smtp.assert_called_once()

    def test_smtp_timeout_after_dispatch_is_uncertain_and_never_resent(self):
        now = timezone.now()
        contact = self.make_contact()
        timer, checkin = self.make_unconfirmed_timer(now, guardian_enabled=True)
        timer.guardian_contacts.add(contact)

        with patch(
            'SafetyApp.views.get_panic_email_config',
            return_value=('sender@example.test', 'test-password'),
        ), patch('SafetyApp.safety_checkins.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.side_effect = TimeoutError()
            process_safety_checkins(now=now, owner_kind='timer')
            process_safety_checkins(now=now + timedelta(hours=1), owner_kind='timer')

        attempts = SafetyCheckInAttempt.objects.filter(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        )
        self.assertEqual(attempts.count(), 1)
        self.assertEqual(attempts.get().status, SafetyCheckInAttempt.STATUS_UNCERTAIN)
        smtp.assert_called_once()

    def test_definite_connection_failure_gets_one_bounded_retry(self):
        now = timezone.now()
        contact = self.make_contact()
        timer, checkin = self.make_unconfirmed_timer(now, guardian_enabled=True)
        timer.guardian_contacts.add(contact)

        with patch(
            'SafetyApp.views.get_panic_email_config',
            return_value=('sender@example.test', 'test-password'),
        ), patch('SafetyApp.safety_checkins.smtplib.SMTP_SSL') as smtp:
            smtp.side_effect = smtplib.SMTPConnectError(421, b'temporarily unavailable')
            process_safety_checkins(now=now, owner_kind='timer')
            process_safety_checkins(
                now=timezone.now() + timedelta(minutes=6),
                owner_kind='timer',
            )
            process_safety_checkins(
                now=timezone.now() + timedelta(minutes=12),
                owner_kind='timer',
            )

        attempts = SafetyCheckInAttempt.objects.filter(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        )
        self.assertEqual(attempts.count(), 2)
        self.assertEqual(
            set(attempts.values_list('status', flat=True)),
            {SafetyCheckInAttempt.STATUS_FAILED},
        )
        self.assertEqual(smtp.call_count, 2)

    def test_scheduler_recovery_backfills_missed_timer_schedule(self):
        now = timezone.now()
        timer = self.make_timer(
            now,
            started_at=now - timedelta(minutes=40),
            expires_at=now - timedelta(minutes=20),
        )

        result = process_safety_checkins(now=now, owner_kind='timer', dispatch_emails=False)

        timer.refresh_from_db()
        self.assertEqual(result['backfilled_checkins'], 1)
        self.assertEqual(timer.status, SafetyTimer.STATUS_UNCONFIRMED)
        self.assertEqual(timer.safety_checkins.count(), 1)

    def test_repeated_scheduler_runs_do_not_duplicate_reminder_attempts(self):
        now = timezone.now()
        timer = self.make_timer(now)
        checkin = schedule_checkin(timer, due_at=now - timedelta(minutes=1), now=now)

        process_safety_checkins(now=now, owner_kind='timer', dispatch_emails=False)
        process_safety_checkins(now=now, owner_kind='timer', dispatch_emails=False)

        self.assertEqual(
            SafetyCheckInAttempt.objects.filter(
                checkin=checkin,
                kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
            ).count(),
            1,
        )

    def test_checkin_response_wins_before_deadline_and_suppresses_later_escalation(self):
        now = timezone.now()
        journey = self.make_journey(now)
        checkin = schedule_checkin(journey, due_at=now - timedelta(minutes=1), now=now)
        process_safety_checkins(now=now, owner_kind='journey', dispatch_emails=False)

        response = self.client.post('/Journeys/{}/Safe'.format(journey.pk))

        checkin.refresh_from_db()
        journey.refresh_from_db()
        process_safety_checkins(now=now + timedelta(minutes=30), owner_kind='journey', dispatch_emails=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(checkin.status, SafetyCheckIn.STATUS_CONFIRMED)
        self.assertEqual(journey.status, Journey.STATUS_ACTIVE)
        self.assertEqual(journey.safety_checkins.count(), 2)
        self.assertFalse(SOSEvent.objects.exists())

    def test_consent_gated_location_update_records_accuracy_and_timestamp(self):
        now = timezone.now()
        journey = self.make_journey(now, share_location_with_contacts=True)
        accepted = update_owner_location(
            journey,
            {
                'location_status': 'AVAILABLE',
                'latitude': '17.5',
                'longitude': '78.4',
                'accuracy_meters': '18.5',
            },
            now=now,
        )
        journey.refresh_from_db()

        self.assertTrue(accepted)
        self.assertEqual(journey.location_status, 'AVAILABLE')
        self.assertEqual(journey.last_known_accuracy_meters, 18.5)
        self.assertEqual(journey.last_location_updated_at, now)
        self.assertEqual(journey.last_device_seen_at, now)

    def test_location_unavailable_is_distinct_from_missing_device_heartbeat(self):
        now = timezone.now()
        timer = self.make_timer(now, share_location_with_contacts=True)
        accepted = update_owner_location(
            timer,
            {'location_status': 'UNAVAILABLE'},
            now=now,
        )
        timer.refresh_from_db()

        self.assertTrue(accepted)
        self.assertEqual(timer.location_status, 'UNAVAILABLE')
        self.assertIsNone(timer.last_known_latitude)
        self.assertEqual(timer.last_device_seen_at, now)

    def test_unconfirmed_timer_can_be_cancelled_without_further_escalation(self):
        now = timezone.now()
        timer, checkin = self.make_unconfirmed_timer(now)

        response = self.client.post('/SafetyTimerCancel')
        self.assertEqual(response.status_code, 302)

        timer.refresh_from_db()
        checkin.refresh_from_db()
        self.assertEqual(timer.status, SafetyTimer.STATUS_CANCELLED)
        self.assertEqual(checkin.status, SafetyCheckIn.STATUS_CANCELLED)
        result = process_safety_checkins(
            now=now + timedelta(hours=1),
            owner_kind='timer',
            dispatch_emails=False,
        )
        self.assertEqual(result['unconfirmed_checkins'], 0)

    def test_automatic_sos_requires_opt_in_and_reuses_active_event(self):
        now = timezone.now()
        timer, checkin = self.make_unconfirmed_timer(
            now,
            guardian_enabled=False,
            auto_sos_on_unconfirmed=True,
            auto_sos_delay_minutes=15,
        )
        existing_event = SOSEvent.objects.create(
            user=self.owner,
            status=SOSEvent.STATUS_ACTIVE,
        )
        process_safety_checkins(now=now, owner_kind='timer', dispatch_emails=False)

        with patch('SafetyApp.sos_escalation.start_sos_escalation') as start_sos:
            process_safety_checkins(
                now=now + timedelta(minutes=16),
                owner_kind='timer',
                dispatch_emails=False,
            )

        checkin.refresh_from_db()
        self.assertEqual(SOSEvent.objects.filter(user=self.owner).count(), 1)
        self.assertEqual(checkin.auto_sos_event_id, existing_event.pk)
        start_sos.assert_called_once_with(existing_event)

    def test_opted_in_automatic_sos_creates_only_one_event(self):
        now = timezone.now()
        timer, checkin = self.make_unconfirmed_timer(
            now,
            guardian_enabled=False,
            auto_sos_on_unconfirmed=True,
            auto_sos_delay_minutes=15,
            share_location_with_contacts=True,
            last_known_latitude=12.34,
            last_known_longitude=56.78,
            last_location_updated_at=now,
        )
        process_safety_checkins(now=now, owner_kind='timer', dispatch_emails=False)

        with patch('SafetyApp.sos_escalation.start_sos_escalation') as start_sos:
            process_safety_checkins(
                now=now + timedelta(minutes=16),
                owner_kind='timer',
                dispatch_emails=False,
            )
            process_safety_checkins(
                now=now + timedelta(minutes=17),
                owner_kind='timer',
                dispatch_emails=False,
            )

        checkin.refresh_from_db()
        event = SOSEvent.objects.get(
            user=self.owner,
            status=SOSEvent.STATUS_ACTIVE,
        )
        self.assertEqual(SOSEvent.objects.filter(user=self.owner).count(), 1)
        self.assertIsNotNone(checkin.auto_sos_event_id)
        self.assertEqual(checkin.auto_sos_event_id, event.pk)
        self.assertEqual(event.latitude, 12.34)
        self.assertEqual(event.longitude, 56.78)
        self.assertEqual(SOSLiveLocationShare.objects.filter(sos_event=event).count(), 1)
        self.assertGreaterEqual(start_sos.call_count, 1)

    def test_automatic_sos_stays_disabled_by_default(self):
        now = timezone.now()
        self.make_unconfirmed_timer(now, auto_sos_on_unconfirmed=False)

        with patch('SafetyApp.sos_escalation.start_sos_escalation') as start_sos:
            process_safety_checkins(
                now=now + timedelta(hours=1),
                owner_kind='timer',
                dispatch_emails=False,
            )

        self.assertFalse(SOSEvent.objects.exists())
        start_sos.assert_not_called()

    def test_unified_scheduler_processes_journey_and_timer_once(self):
        now = timezone.now()
        timer = self.make_timer(now)
        timer_checkin = schedule_checkin(
            timer,
            due_at=now - timedelta(minutes=1),
            now=now,
        )
        journey = self.make_journey(now)
        journey_checkin = schedule_checkin(
            journey,
            due_at=now - timedelta(minutes=1),
            now=now,
        )
        output = StringIO()

        call_command('process_safety_work', stdout=output)
        call_command('process_safety_work', stdout=output)

        self.assertEqual(
            SafetyCheckInAttempt.objects.filter(
                kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
                checkin__in=(timer_checkin, journey_checkin),
            ).count(),
            2,
        )
        self.assertIn('SOS escalation events processed', output.getvalue())

    def test_location_endpoint_requires_consent_and_valid_coordinates(self):
        journey = self.make_journey(share_location_with_contacts=False)
        payload = {
            'owner_type': 'journey',
            'owner_id': journey.pk,
            'location_status': 'AVAILABLE',
            'latitude': 999,
            'longitude': 1,
            'accuracy_meters': 5,
        }

        response = self.client.post(
            '/SafetyCheckIn/Location',
            data=json.dumps(payload),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        journey.refresh_from_db()
        self.assertIsNone(journey.last_known_latitude)

    def test_guardian_email_includes_opted_in_location_timestamp_and_accuracy(self):
        now = timezone.now()
        contact = self.make_contact()
        timer, checkin = self.make_unconfirmed_timer(
            now,
            guardian_enabled=True,
            share_location_with_contacts=True,
        )
        timer.guardian_contacts.add(contact)
        self.assertTrue(update_owner_location(
            timer,
            {
                'location_status': 'AVAILABLE',
                'latitude': '17.5031',
                'longitude': '78.4272',
                'accuracy_meters': '12.5',
            },
            now=now - timedelta(minutes=1),
        ))
        timer.refresh_from_db()

        with patch(
            'SafetyApp.views.get_panic_email_config',
            return_value=('sender@example.test', 'test-password'),
        ), patch('SafetyApp.safety_checkins.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            process_safety_checkins(now=now, owner_kind='timer')

        message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        body = message.get_content()
        self.assertIn('17.503100, 78.427200', body)
        self.assertIn('12 m accuracy', body)
        self.assertIn('https://www.google.com/maps?q=17.503100,78.427200', body)
        self.assertIn('seconds old', body)
