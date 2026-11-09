import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.utils import timezone

from .models import (
    EmergencyContact,
    SOSContactAcknowledgement,
    SOSEscalationState,
    SOSEvent,
    SOSNotificationAttempt,
)
from .sos_acknowledgement import (
    VOICE_CALLBACK_MAX_AGE,
    VoiceCallbackValidator,
    generate_web_acknowledgement_token,
    record_voice_acknowledgement,
    record_web_acknowledgement,
)


class SyntheticVoiceCallbackValidator(VoiceCallbackValidator):
    def __init__(self, claims):
        self.claims = claims

    def validate(self, callback_payload, callback_signature):
        if callback_signature != 'synthetic-signature':
            raise ValueError('Invalid synthetic signature.')
        return self.claims


def _ensure_sos_workflow_test_schema():
    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        contact_table = EmergencyContact._meta.db_table
        if contact_table in existing_tables:
            with connection.cursor() as cursor:
                cursor.execute('SELECT * FROM {}'.format(connection.ops.quote_name(contact_table)))
                existing_columns = {column[0] for column in cursor.description}
            for field_name in (
                'email_consent',
                'sms_consent',
                'voice_consent',
                'email_verified_at',
                'phone_verified_at',
                'consent_updated_at',
            ):
                field = EmergencyContact._meta.get_field(field_name)
                if field.column not in existing_columns:
                    schema_editor.add_field(EmergencyContact, field)

        for model in (
            SOSNotificationAttempt,
            SOSContactAcknowledgement,
            SOSEscalationState,
        ):
            if model._meta.db_table not in existing_tables:
                schema_editor.create_model(model)
                existing_tables.add(model._meta.db_table)


class SOSAcknowledgementTestCase(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super(SOSAcknowledgementTestCase, cls).setUpClass()
        _ensure_sos_workflow_test_schema()

    def setUp(self):
        self.username = 'acknowledgement-user'
        self.event = SOSEvent.objects.create(
            user=self.username,
            status=SOSEvent.STATUS_ACTIVE,
        )
        self.contact = EmergencyContact.objects.create(
            user=self.username,
            name='Trusted Contact',
            phone_number='+1 202 555 0133',
            email='trusted@example.org',
            is_active=True,
        )
        self.state = SOSEscalationState.objects.create(
            sos_event=self.event,
            current_contact=self.contact,
            current_contact_order=1,
            contact_sequence=json.dumps([self.contact.pk]),
            acknowledgement_deadline=timezone.now() + timedelta(minutes=1),
            next_action_at=timezone.now() + timedelta(minutes=1),
        )
        self.voice_attempt = SOSNotificationAttempt.objects.create(
            sos_event=self.event,
            emergency_contact=self.contact,
            channel=SOSNotificationAttempt.CHANNEL_VOICE,
            attempt_number=1,
            provider_reference='synthetic-provider-call-123',
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
        )

    def voice_callback_claims(self, **overrides):
        claims = {
            'authenticated': True,
            'timestamp': timezone.now(),
            'provider_call_reference': self.voice_attempt.provider_reference,
            'event_id': self.event.pk,
            'contact_id': self.contact.pk,
            'dtmf_input': '1',
        }
        claims.update(overrides)
        return claims

    def voice_callback(self, dtmf_input='1', claims=None, signature='synthetic-signature'):
        if claims is None:
            claims = self.voice_callback_claims(dtmf_input=dtmf_input)
        return record_voice_acknowledgement(
            {'synthetic': 'provider callback'},
            signature,
            SyntheticVoiceCallbackValidator(claims),
        )

    def test_valid_voice_ack_requires_provider_validation_and_dtmf_one(self):
        acknowledgement = self.voice_callback()
        self.assertEqual(
            acknowledgement.method,
            SOSContactAcknowledgement.METHOD_VOICE_DTMF,
        )
        self.voice_attempt.refresh_from_db()
        self.assertEqual(
            self.voice_attempt.status,
            SOSNotificationAttempt.STATUS_ACKNOWLEDGED,
        )

    def test_invalid_or_unvalidated_voice_callback_is_rejected(self):
        with self.assertRaises(ValueError):
            record_voice_acknowledgement({}, 'unverified', None)
        with self.assertRaises(ValueError):
            record_voice_acknowledgement(
                {},
                'bad-signature',
                SyntheticVoiceCallbackValidator(self.voice_callback_claims()),
            )
        with self.assertRaises(ValueError):
            self.voice_callback(claims=self.voice_callback_claims(authenticated=False))
        with self.assertRaises(ValueError):
            self.voice_callback(dtmf_input='2')

    def test_stale_voice_callback_is_rejected(self):
        with self.assertRaises(ValueError):
            self.voice_callback(claims=self.voice_callback_claims(
                timestamp=timezone.now() - VOICE_CALLBACK_MAX_AGE - timedelta(seconds=1),
            ))

    def test_mismatched_voice_provider_call_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            self.voice_callback(claims=self.voice_callback_claims(
                provider_call_reference='different-provider-call',
            ))

    def test_voice_callback_rejects_references_for_another_contact_or_event(self):
        other_event = SOSEvent.objects.create(
            user='other-voice-owner',
            status=SOSEvent.STATUS_ACTIVE,
        )
        other_contact = EmergencyContact.objects.create(
            user='other-voice-owner',
            name='Other Voice Contact',
            phone_number='+1 202 555 0137',
            is_active=True,
        )
        SOSEscalationState.objects.create(
            sos_event=other_event,
            current_contact=other_contact,
            current_contact_order=1,
            contact_sequence=json.dumps([other_contact.pk]),
        )
        with self.assertRaises(ValueError):
            self.voice_callback(claims=self.voice_callback_claims(
                event_id=other_event.pk,
                contact_id=other_contact.pk,
            ))

    def test_web_token_is_cryptographically_random_and_stored_only_as_hash(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        stored = SOSContactAcknowledgement.objects.get(
            sos_event=self.event,
            emergency_contact=self.contact,
        )
        self.assertGreaterEqual(len(token), 40)
        self.assertNotEqual(stored.token_hash, token)
        self.assertEqual(len(stored.token_hash), 64)

    def test_valid_web_token_acknowledges_and_is_single_use(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        acknowledgement = record_web_acknowledgement(token)

        self.assertEqual(acknowledgement.method, SOSContactAcknowledgement.METHOD_WEB_LINK)
        self.assertIsNotNone(acknowledgement.acknowledged_at)
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, SOSEscalationState.STATUS_ACKNOWLEDGED)
        with self.assertRaises(ValueError):
            record_web_acknowledgement(token)

    def test_web_acknowledgement_endpoint_accepts_bearer_token_only_in_post_body(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            '/SOS/Acknowledge',
            data=json.dumps({'token': token}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'acknowledged'})
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, SOSEscalationState.STATUS_ACKNOWLEDGED)

        replay = client.post(
            '/SOS/Acknowledge',
            data=json.dumps({'token': token}),
            content_type='application/json',
        )
        self.assertEqual(replay.status_code, 400)
        self.assertNotIn(token, replay.content.decode('utf-8'))

    def test_web_acknowledgement_endpoint_rejects_get_query_tokens_and_invalid_requests(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        client = Client(enforce_csrf_checks=True)

        get_response = client.get('/SOS/Acknowledge?token={}'.format(token))
        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(
            client.post('/SOS/Acknowledge', {'token': token}).status_code,
            415,
        )
        invalid_response = client.post(
            '/SOS/Acknowledge',
            data=json.dumps({'token': 'invalid-token'}),
            content_type='application/json',
        )
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIsNone(
            SOSContactAcknowledgement.objects.get(
                sos_event=self.event,
                emergency_contact=self.contact,
            ).acknowledged_at
        )

    def test_expired_web_token_is_rejected(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        acknowledgement = SOSContactAcknowledgement.objects.get(
            sos_event=self.event,
            emergency_contact=self.contact,
        )
        acknowledgement.token_expires_at = timezone.now() - timedelta(seconds=1)
        acknowledgement.save(update_fields=('token_expires_at',))

        with self.assertRaises(ValueError):
            record_web_acknowledgement(token)

    def test_mismatched_event_contact_token_binding_is_rejected(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        other_event = SOSEvent.objects.create(
            user='other-ack-user',
            status=SOSEvent.STATUS_ACTIVE,
        )
        other_contact = EmergencyContact.objects.create(
            user='other-ack-user',
            name='Other Contact',
            phone_number='+1 202 555 0134',
            email='other@example.org',
        )
        SOSEscalationState.objects.create(
            sos_event=other_event,
            current_contact=other_contact,
            current_contact_order=1,
            contact_sequence=json.dumps([other_contact.pk]),
        )
        token_record = SOSContactAcknowledgement.objects.get(
            sos_event=self.event,
            emergency_contact=self.contact,
        )
        token_record.emergency_contact = other_contact
        token_record.save(update_fields=('emergency_contact',))

        with self.assertRaises(ValueError):
            record_web_acknowledgement(token)

    def test_other_owner_and_contact_outside_snapshot_are_rejected(self):
        other_contact = EmergencyContact.objects.create(
            user='different-owner',
            name='Different Owner',
            phone_number='+1 202 555 0135',
            email='different@example.org',
        )
        with self.assertRaises(ValueError):
            record_voice_acknowledgement(
                {},
                'synthetic-signature',
                SyntheticVoiceCallbackValidator(self.voice_callback_claims(
                    contact_id=other_contact.pk,
                )),
            )

        outside_contact = EmergencyContact.objects.create(
            user=self.username,
            name='Not Snapshotted',
            phone_number='+1 202 555 0136',
            email='outside@example.org',
        )
        with self.assertRaises(ValueError):
            record_voice_acknowledgement(
                {},
                'synthetic-signature',
                SyntheticVoiceCallbackValidator(self.voice_callback_claims(
                    contact_id=outside_contact.pk,
                )),
            )

    def test_cancelled_event_cannot_be_acknowledged(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        self.event.mark_cancelled()
        with self.assertRaises(ValueError):
            record_web_acknowledgement(token)
        with self.assertRaises(ValueError):
            self.voice_callback()

    def test_replayed_voice_callback_is_rejected(self):
        first = self.voice_callback()
        with self.assertRaises(ValueError):
            self.voice_callback()
        self.voice_attempt.refresh_from_db()
        self.assertEqual(
            self.voice_attempt.status,
            SOSNotificationAttempt.STATUS_ACKNOWLEDGED,
        )
        self.assertEqual(
            SOSContactAcknowledgement.objects.filter(
                sos_event=self.event,
                emergency_contact=self.contact,
            ).count(),
            1,
        )

    def test_concurrent_replayed_voice_callbacks_record_only_one_acknowledgement(self):
        barrier = Barrier(2)

        def acknowledge():
            close_old_connections()
            barrier.wait(timeout=5)
            try:
                result = self.voice_callback()
                return ('acknowledged', result.pk)
            except ValueError:
                return ('rejected', None)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: acknowledge(), range(2)))

        self.assertEqual(
            sorted(result[0] for result in results),
            ['acknowledged', 'rejected'],
        )
        self.assertEqual(
            SOSContactAcknowledgement.objects.filter(
                sos_event=self.event,
                emergency_contact=self.contact,
            ).count(),
            1,
        )

    def test_acknowledgement_cancels_queued_attempts_and_invalidates_tokens(self):
        token = generate_web_acknowledgement_token(self.event, self.contact)
        queued = SOSNotificationAttempt.objects.create(
            sos_event=self.event,
            emergency_contact=self.contact,
            channel=SOSNotificationAttempt.CHANNEL_SMS,
            attempt_number=1,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )

        acknowledgement = record_web_acknowledgement(token)
        queued.refresh_from_db()
        acknowledgement.refresh_from_db()

        self.assertEqual(queued.status, SOSNotificationAttempt.STATUS_CANCELLED)
        self.assertEqual(acknowledgement.used_at, acknowledgement.acknowledged_at)
        self.assertLessEqual(acknowledgement.token_expires_at, timezone.now())
