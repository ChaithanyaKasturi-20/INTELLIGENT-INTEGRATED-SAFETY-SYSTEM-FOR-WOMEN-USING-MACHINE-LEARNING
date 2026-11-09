import base64
import io
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

from django.test import SimpleTestCase

from .models import EmergencyContact, SOSEvent, SOSNotificationAttempt
from .sos_notifications import (
    MockEmailProvider,
    MockSMSProvider,
    MockVoiceProvider,
    ExotelSMSProvider,
    ExotelVoiceProvider,
    _is_valid_email,
    _next_attempt_number,
    _record_result,
    dispatch_contact_notifications,
    dispatch_notification_attempt,
    get_eligible_contacts,
    get_default_provider,
    is_valid_phone_number,
    recover_interrupted_attempts,
)
from .sos_notifications import ProviderResult


@patch.dict(
    os.environ,
    {
        'SOS_EXOTEL_SMS_ENABLED': '',
        'SOS_MOCK_SMS_ENABLED': '',
        'SOS_EXOTEL_VOICE_ENABLED': '',
        'SOS_MOCK_VOICE_ENABLED': '',
    },
    clear=False,
)
class SOSNotificationValidationTests(SimpleTestCase):
    class _HTTPResponse(io.BytesIO):
        def getcode(self):
            return 200

    def test_phone_validation_rejects_malformed_and_placeholder_numbers(self):
        invalid_numbers = (
            '',
            'not a phone',
            '0000000000',
            '1234567890',
            '1111111111',
        )
        for number in invalid_numbers:
            with self.subTest(number=number):
                self.assertFalse(is_valid_phone_number(number))

    def test_phone_validation_accepts_valid_number(self):
        self.assertTrue(is_valid_phone_number('+91 (98765) 43210'))

    def test_email_validation_rejects_placeholders_and_invalid_values(self):
        self.assertFalse(_is_valid_email('contact@example.com'))
        self.assertFalse(_is_valid_email('not-an-email'))

    def test_email_validation_accepts_valid_address(self):
        self.assertTrue(_is_valid_email('guardian@example.org'))

    def test_email_contact_requires_active_user_consent_and_verification(self):
        contact = SimpleNamespace(
            email='guardian@example.org',
            email_consent=True,
            email_verified_at=object(),
        )
        queryset = Mock()
        queryset.order_by.return_value = [contact]
        event = SimpleNamespace(user='owner')

        with patch.object(
            EmergencyContact.objects,
            'filter',
            return_value=queryset,
        ) as contact_filter:
            eligible = get_eligible_contacts(
                event,
                SOSNotificationAttempt.CHANNEL_EMAIL,
            )

        self.assertEqual(eligible, [contact])
        contact_filter.assert_called_once_with(user='owner', is_active=True)

    def test_primary_contact_is_first_with_deterministic_tie_order(self):
        primary_later_pk = SimpleNamespace(
            pk=4,
            is_primary=True,
            email='primary@example.org',
            email_consent=True,
            email_verified_at=object(),
        )
        primary_earlier_pk = SimpleNamespace(
            pk=2,
            is_primary=True,
            email='primary2@example.org',
            email_consent=True,
            email_verified_at=object(),
        )
        secondary = SimpleNamespace(
            pk=1,
            is_primary=False,
            email='secondary@example.org',
            email_consent=True,
            email_verified_at=object(),
        )

        class OrderedContacts(list):
            def order_by(self, *fields):
                self.asserted_fields = fields
                return sorted(self, key=lambda contact: (not contact.is_primary, contact.pk))

        queryset = OrderedContacts((secondary, primary_later_pk, primary_earlier_pk))
        event = SimpleNamespace(user='owner')

        with patch.object(EmergencyContact.objects, 'filter', return_value=queryset):
            eligible = get_eligible_contacts(event, SOSNotificationAttempt.CHANNEL_EMAIL)

        self.assertEqual(eligible, [primary_earlier_pk, primary_later_pk, secondary])
        self.assertEqual(queryset.asserted_fields, ('-is_primary', 'pk'))

    def test_phone_contact_requires_consent_verification_and_valid_number(self):
        contact = SimpleNamespace(
            phone_number='+91 9876543210',
            phone_verified_at=object(),
            sms_consent=True,
            voice_consent=False,
        )
        queryset = Mock()
        queryset.order_by.return_value = [contact]
        event = SimpleNamespace(user='owner')

        with patch.object(
            EmergencyContact.objects,
            'filter',
            return_value=queryset,
        ):
            sms_eligible = get_eligible_contacts(
                event,
                SOSNotificationAttempt.CHANNEL_SMS,
            )
            voice_eligible = get_eligible_contacts(
                event,
                SOSNotificationAttempt.CHANNEL_VOICE,
            )

        self.assertEqual(sms_eligible, [contact])
        self.assertEqual(voice_eligible, [])

    def test_default_providers_are_mock_only_and_report_acceptance(self):
        attempt = Mock(idempotency_key='attempt-key')
        event = Mock()
        contact = Mock()
        provider_types = (
            (MockEmailProvider, SOSNotificationAttempt.CHANNEL_EMAIL, 'mock-email-'),
            (MockSMSProvider, SOSNotificationAttempt.CHANNEL_SMS, 'mock-sms-'),
            (MockVoiceProvider, SOSNotificationAttempt.CHANNEL_VOICE, 'mock-voice-'),
        )

        for provider_type, channel, reference_prefix in provider_types:
            with self.subTest(channel=channel):
                result = provider_type().send(attempt, event, contact)
                self.assertEqual(result.status, SOSNotificationAttempt.STATUS_ACCEPTED)
                self.assertTrue(result.provider_reference.startswith(reference_prefix))

    def test_exotel_sms_adapter_uses_documented_request_and_only_reports_acceptance(self):
        provider = ExotelSMSProvider(
            api_key='synthetic-key',
            api_token='synthetic-token',
            account_sid='synthetic-account',
            region='in',
            sender_id='TESTSAFE',
            message_body='An emergency contact needs you.',
            dlt_entity_id='synthetic-entity',
            dlt_template_id='synthetic-template',
        )
        attempt = SimpleNamespace(idempotency_key=uuid.uuid4())
        event = SimpleNamespace(user='synthetic-user')
        contact = SimpleNamespace(phone_number='+1 (202) 555-0100')
        response_body = json.dumps({
            'SMSMessage': {
                'Sid': 'synthetic-sms-reference',
                'Status': 'queued',
            },
        }).encode('utf-8')
        response = self._HTTPResponse(response_body)

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            return_value=response,
        ) as urlopen:
            result = provider.send(attempt, event, contact)

        request = urlopen.call_args[0][0]
        self.assertEqual(
            request.full_url,
            'https://api.in.exotel.com/v1/Accounts/synthetic-account/Sms/send',
        )
        self.assertEqual(
            request.get_header('Authorization'),
            'Basic {}'.format(base64.b64encode(
                b'synthetic-key:synthetic-token'
            ).decode('ascii')),
        )
        self.assertEqual(urlopen.call_args[1]['timeout'], 10)
        parameters = urllib.parse.parse_qs(request.data.decode('ascii'))
        self.assertEqual(parameters['From'], ['TESTSAFE'])
        self.assertEqual(parameters['To'], ['+12025550100'])
        self.assertEqual(parameters['Body'], ['An emergency contact needs you.'])
        self.assertEqual(parameters['DltEntityId'], ['synthetic-entity'])
        self.assertEqual(parameters['DltTemplateId'], ['synthetic-template'])
        self.assertEqual(parameters['CustomField'], [str(attempt.idempotency_key)])
        self.assertEqual(result.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertEqual(result.provider_reference, 'synthetic-sms-reference')

    def test_exotel_sms_adapter_marks_rejections_and_ambiguous_results_distinctly(self):
        provider = ExotelSMSProvider(
            api_key='synthetic-key',
            api_token='synthetic-token',
            account_sid='synthetic-account',
            region='sg',
            sender_id='TESTSAFE',
            message_body='An emergency contact needs you.',
        )
        attempt = SimpleNamespace(idempotency_key=uuid.uuid4())
        event = SimpleNamespace(user='synthetic-user')
        contact = SimpleNamespace(phone_number='+1 202 555 0100')

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            side_effect=urllib.error.HTTPError(
                'https://api.exotel.com/request',
                400,
                'Bad request',
                {},
                io.BytesIO(b''),
            ),
        ):
            rejected = provider.send(attempt, event, contact)
        self.assertEqual(rejected.status, SOSNotificationAttempt.STATUS_FAILED)

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            side_effect=urllib.error.HTTPError(
                'https://api.exotel.com/request',
                503,
                'Service unavailable',
                {},
                io.BytesIO(b''),
            ),
        ):
            unavailable = provider.send(attempt, event, contact)
        self.assertEqual(unavailable.status, SOSNotificationAttempt.STATUS_UNCERTAIN)

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            side_effect=TimeoutError('synthetic timeout'),
        ):
            timed_out = provider.send(attempt, event, contact)
        self.assertEqual(timed_out.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(provider.reconcile(attempt).outcome, 'UNKNOWN')

    def test_exotel_sms_requires_explicit_opt_in_and_runtime_configuration(self):
        with patch.dict(os.environ, {'SOS_EXOTEL_SMS_ENABLED': ''}, clear=False):
            self.assertIsInstance(
                get_default_provider(SOSNotificationAttempt.CHANNEL_SMS),
                MockSMSProvider,
            )

        with patch.dict(os.environ, {'SOS_EXOTEL_SMS_ENABLED': 'true'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'SOS_EXOTEL_API_KEY'):
                get_default_provider(SOSNotificationAttempt.CHANNEL_SMS)

        runtime_config = {
            'SOS_EXOTEL_SMS_ENABLED': 'true',
            'SOS_EXOTEL_API_KEY': 'synthetic-key',
            'SOS_EXOTEL_API_TOKEN': 'synthetic-token',
            'SOS_EXOTEL_ACCOUNT_SID': 'synthetic-account',
            'SOS_EXOTEL_REGION': 'IN',
            'SOS_EXOTEL_SMS_SENDER_ID': 'TESTSAFE',
            'SOS_EXOTEL_SMS_BODY': 'An emergency contact needs you.',
            'SOS_EXOTEL_DLT_ENTITY_ID': 'synthetic-entity',
        }
        with patch.dict(os.environ, runtime_config, clear=True):
            provider = get_default_provider(SOSNotificationAttempt.CHANNEL_SMS)
        self.assertIsInstance(provider, ExotelSMSProvider)
        self.assertEqual(provider.region, 'in')

    def test_exotel_sms_adapter_rejects_non_e164_numbers_without_a_request(self):
        provider = ExotelSMSProvider(
            api_key='synthetic-key',
            api_token='synthetic-token',
            account_sid='synthetic-account',
            region='sg',
            sender_id='TESTSAFE',
            message_body='An emergency contact needs you.',
        )
        with patch('SafetyApp.sos_notifications.urllib.request.urlopen') as urlopen:
            result = provider.send(
                SimpleNamespace(idempotency_key=uuid.uuid4()),
                SimpleNamespace(user='synthetic-user'),
                SimpleNamespace(phone_number='2025550100'),
            )

        self.assertEqual(result.status, SOSNotificationAttempt.STATUS_FAILED)
        urlopen.assert_not_called()

    def test_exotel_voice_adapter_uses_documented_flow_request_and_only_reports_acceptance(self):
        provider = ExotelVoiceProvider(
            api_key='synthetic-key',
            api_token='synthetic-token',
            account_sid='synthetic-account',
            region='in',
            caller_id='0101234567',
            app_id='synthetic-flow',
        )
        attempt = SimpleNamespace(idempotency_key=uuid.uuid4())
        event = SimpleNamespace(user='synthetic-user')
        contact = SimpleNamespace(phone_number='+91 (98765) 43210')
        response_body = json.dumps({
            'Call': {
                'Sid': 'synthetic-call-reference',
                'Status': 'in-progress',
            },
        }).encode('utf-8')
        response = self._HTTPResponse(response_body)

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            return_value=response,
        ) as urlopen:
            result = provider.send(attempt, event, contact)

        request = urlopen.call_args[0][0]
        self.assertEqual(
            request.full_url,
            'https://api.in.exotel.com/v1/Accounts/synthetic-account/Calls/connect',
        )
        self.assertEqual(urlopen.call_args[1]['timeout'], 10)
        parameters = urllib.parse.parse_qs(request.data.decode('ascii'))
        self.assertEqual(parameters['From'], ['+919876543210'])
        self.assertEqual(parameters['CallerId'], ['0101234567'])
        self.assertEqual(
            parameters['Url'],
            ['http://my.exotel.com/synthetic-account/exoml/start_voice/synthetic-flow'],
        )
        self.assertEqual(parameters['CallType'], ['trans'])
        self.assertEqual(parameters['CustomField'], [str(attempt.idempotency_key)])
        self.assertEqual(result.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertEqual(result.provider_reference, 'synthetic-call-reference')
        self.assertEqual(provider.reconcile(attempt).outcome, 'UNKNOWN')

    def test_exotel_voice_adapter_handles_rejections_timeouts_and_malformed_responses(self):
        provider = ExotelVoiceProvider(
            api_key='synthetic-key',
            api_token='synthetic-token',
            account_sid='synthetic-account',
            region='sg',
            caller_id='0101234567',
            app_id='synthetic-flow',
        )
        attempt = SimpleNamespace(idempotency_key=uuid.uuid4())
        event = SimpleNamespace(user='synthetic-user')
        contact = SimpleNamespace(phone_number='+1 202 555 0100')

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            side_effect=urllib.error.HTTPError(
                'https://api.exotel.com/request',
                401,
                'Unauthorized',
                {},
                io.BytesIO(b''),
            ),
        ):
            rejected = provider.send(attempt, event, contact)
        self.assertEqual(rejected.status, SOSNotificationAttempt.STATUS_FAILED)
        self.assertEqual(rejected.error_code, 'EXOTEL_HTTP_401')

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            side_effect=TimeoutError('synthetic timeout'),
        ):
            timed_out = provider.send(attempt, event, contact)
        self.assertEqual(timed_out.status, SOSNotificationAttempt.STATUS_UNCERTAIN)

        for body in (b'not-json', b'{}', json.dumps({
            'Call': {'Sid': 'synthetic-call-reference', 'Status': 'unknown'},
        }).encode('utf-8')):
            with self.subTest(body=body), patch(
                'SafetyApp.sos_notifications.urllib.request.urlopen',
                return_value=self._HTTPResponse(body),
            ):
                malformed = provider.send(attempt, event, contact)
            self.assertEqual(
                malformed.status,
                SOSNotificationAttempt.STATUS_UNCERTAIN,
            )

        with patch(
            'SafetyApp.sos_notifications.urllib.request.urlopen',
            return_value=self._HTTPResponse(json.dumps({
                'Call': {'Sid': 'synthetic-call-reference', 'Status': 'busy'},
            }).encode('utf-8')),
        ):
            not_connected = provider.send(attempt, event, contact)
        self.assertEqual(
            not_connected.status,
            SOSNotificationAttempt.STATUS_FAILED,
        )

    def test_exotel_voice_adapter_rejects_invalid_recipient_without_a_request(self):
        provider = ExotelVoiceProvider(
            api_key='synthetic-key',
            api_token='synthetic-token',
            account_sid='synthetic-account',
            region='sg',
            caller_id='0101234567',
            app_id='synthetic-flow',
        )
        with patch('SafetyApp.sos_notifications.urllib.request.urlopen') as urlopen:
            result = provider.send(
                SimpleNamespace(idempotency_key=uuid.uuid4()),
                SimpleNamespace(user='synthetic-user'),
                SimpleNamespace(phone_number='2025550100'),
            )

        self.assertEqual(result.status, SOSNotificationAttempt.STATUS_FAILED)
        self.assertEqual(result.error_code, 'INVALID_E164_RECIPIENT')
        urlopen.assert_not_called()

    def test_provider_flags_are_independent_fail_closed_and_default_to_mocks(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(
                get_default_provider(SOSNotificationAttempt.CHANNEL_SMS),
                MockSMSProvider,
            )
            self.assertIsInstance(
                get_default_provider(SOSNotificationAttempt.CHANNEL_VOICE),
                MockVoiceProvider,
            )

        with patch.dict(os.environ, {
            'SOS_MOCK_SMS_ENABLED': 'false',
            'SOS_EXOTEL_SMS_ENABLED': 'false',
        }, clear=True):
            disabled = get_default_provider(SOSNotificationAttempt.CHANNEL_SMS)
        self.assertEqual(disabled.channel, SOSNotificationAttempt.CHANNEL_SMS)
        self.assertEqual(
            disabled.send(None, None, None).error_code,
            'PROVIDER_DISABLED',
        )

        with patch.dict(os.environ, {
            'SOS_MOCK_VOICE_ENABLED': 'true',
            'SOS_EXOTEL_VOICE_ENABLED': 'true',
        }, clear=True):
            with self.assertRaisesRegex(ValueError, 'Only one voice'):
                get_default_provider(SOSNotificationAttempt.CHANNEL_VOICE)

        voice_config = {
            'SOS_MOCK_VOICE_ENABLED': 'false',
            'SOS_EXOTEL_VOICE_ENABLED': 'true',
            'SOS_EXOTEL_API_KEY': 'synthetic-key',
            'SOS_EXOTEL_API_TOKEN': 'synthetic-token',
            'SOS_EXOTEL_ACCOUNT_SID': 'synthetic-account',
            'SOS_EXOTEL_REGION': 'IN',
            'SOS_EXOTEL_VOICE_CALLER_ID': '0101234567',
            'SOS_EXOTEL_VOICE_APP_ID': 'synthetic-flow',
        }
        with patch.dict(os.environ, voice_config, clear=True):
            provider = get_default_provider(SOSNotificationAttempt.CHANNEL_VOICE)
        self.assertIsInstance(provider, ExotelVoiceProvider)
        self.assertEqual(provider.region, 'in')

    def test_dispatch_does_not_send_when_another_worker_owns_the_attempt(self):
        event = SimpleNamespace(pk=17, user='owner')
        contact = SimpleNamespace(pk=23)
        attempt = SimpleNamespace(
            pk=31,
            status=SOSNotificationAttempt.STATUS_QUEUED,
        )
        provider = Mock()
        provider.channel = SOSNotificationAttempt.CHANNEL_VOICE

        with patch(
            'SafetyApp.sos_notifications._validate_dispatch_target',
        ), patch(
            'SafetyApp.sos_notifications.get_eligible_contacts',
            return_value=[contact],
        ), patch(
            'SafetyApp.sos_notifications._claim_attempt',
            return_value=None,
        ) as claim_attempt, patch(
            'SafetyApp.sos_notifications.SOSNotificationAttempt.objects.get',
            return_value=attempt,
        ) as get_attempt:
            result = dispatch_notification_attempt(
                event,
                contact,
                SOSNotificationAttempt.CHANNEL_VOICE,
                provider=provider,
                attempt=attempt,
            )

        self.assertIs(result, attempt)
        claim_attempt.assert_called_once_with(attempt.pk)
        get_attempt.assert_called_once_with(pk=attempt.pk)
        provider.send.assert_not_called()

    def test_provider_outcomes_are_recorded_without_acknowledgement(self):
        for status in (
            SOSNotificationAttempt.STATUS_ACCEPTED,
            SOSNotificationAttempt.STATUS_DELIVERED,
            SOSNotificationAttempt.STATUS_FAILED,
            SOSNotificationAttempt.STATUS_UNCERTAIN,
        ):
            with self.subTest(status=status):
                claim_token = uuid.uuid4()
                attempt = SOSNotificationAttempt(
                    pk=12,
                    status=SOSNotificationAttempt.STATUS_SENDING,
                    claim_token=claim_token,
                    claim_generation=3,
                )
                attempt._state.adding = False
                attempt._state.db = 'default'
                queryset = Mock()
                queryset.update.return_value = 1
                manager = Mock()
                manager.filter.return_value = queryset

                with patch.object(
                    SOSNotificationAttempt.objects,
                    'using',
                    return_value=manager,
                ):
                    recorded = _record_result(
                        attempt,
                        ProviderResult(status=status),
                    )

                self.assertTrue(recorded)
                self.assertEqual(attempt.status, status)
                self.assertIsNotNone(attempt.completed_at)
                manager.filter.assert_called_once_with(
                    pk=attempt.pk,
                    status=SOSNotificationAttempt.STATUS_SENDING,
                    claim_token=claim_token,
                    claim_generation=3,
                    lease_expires_at__gt=attempt.completed_at,
                )
                queryset.update.assert_called_once()

    def test_non_model_result_attempt_is_rejected_without_saving(self):
        attempt = Mock(status=SOSNotificationAttempt.STATUS_SENDING)

        with self.assertRaises(TypeError):
            _record_result(
                attempt,
                ProviderResult(status=SOSNotificationAttempt.STATUS_DELIVERED),
            )

        attempt.save.assert_not_called()
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_SENDING)

    def test_unpersisted_model_result_attempt_is_rejected_without_update(self):
        attempt = SOSNotificationAttempt(
            pk=12,
            status=SOSNotificationAttempt.STATUS_SENDING,
            claim_token=uuid.uuid4(),
            claim_generation=3,
        )

        with patch.object(SOSNotificationAttempt.objects, 'using') as using:
            recorded = _record_result(
                attempt,
                ProviderResult(status=SOSNotificationAttempt.STATUS_DELIVERED),
            )

        self.assertFalse(recorded)
        self.assertEqual(attempt.status, SOSNotificationAttempt.STATUS_SENDING)
        using.assert_not_called()

    def test_uncertain_provider_exception_does_not_create_retry(self):
        attempt = Mock(
            pk=12,
            status=SOSNotificationAttempt.STATUS_SENDING,
            idempotency_key='attempt-key',
        )
        provider = Mock()
        provider.channel = SOSNotificationAttempt.CHANNEL_EMAIL
        provider.send.side_effect = TimeoutError('synthetic timeout')

        with patch(
            'SafetyApp.sos_notifications._validate_dispatch_target',
        ), patch(
            'SafetyApp.sos_notifications.get_eligible_contacts',
            return_value=[Mock()],
        ) as eligible_contacts, patch(
            'SafetyApp.sos_notifications._get_or_create_attempt',
            return_value=attempt,
        ) as get_or_create_attempt, patch(
            'SafetyApp.sos_notifications._claim_attempt',
            return_value=attempt,
        ), patch(
            'SafetyApp.sos_notifications._record_result',
        ) as record_result:
            from .sos_notifications import dispatch_notification_attempt
            contact = eligible_contacts.return_value[0]
            result = dispatch_notification_attempt(
                Mock(),
                contact,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                provider=provider,
            )

        self.assertIs(result, attempt)
        provider.send.assert_called_once()
        recorded = record_result.call_args[0][1]
        self.assertEqual(recorded.status, SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertEqual(recorded.error_code, 'TimeoutError')
        get_or_create_attempt.assert_called_once()

    def test_returned_provider_outcome_is_recorded_after_cancellation_race(self):
        event = SimpleNamespace(pk=17, user='owner')
        contact = SimpleNamespace(pk=23)
        attempt = SimpleNamespace(
            pk=12,
            status=SOSNotificationAttempt.STATUS_QUEUED,
            claim_token=uuid.uuid4(),
            claim_generation=2,
            provider_reference='',
            error_code='',
            error_message='',
            save=Mock(),
        )
        provider = Mock()
        provider.channel = SOSNotificationAttempt.CHANNEL_EMAIL
        provider.send.side_effect = lambda *_args: (
            setattr(event, 'status', SOSEvent.STATUS_CANCELLED)
            or ProviderResult(
                status=SOSNotificationAttempt.STATUS_ACCEPTED,
                provider_reference='synthetic-provider-reference',
            )
        )

        def record_claimed_result(claimed_attempt, provider_result, **kwargs):
            claimed_attempt.status = provider_result.status
            claimed_attempt.provider_reference = provider_result.provider_reference
            return True

        with patch(
            'SafetyApp.sos_notifications._validate_dispatch_target',
        ), patch(
            'SafetyApp.sos_notifications.get_eligible_contacts',
            return_value=[contact],
        ), patch(
            'SafetyApp.sos_notifications._get_or_create_attempt',
            return_value=attempt,
        ), patch(
            'SafetyApp.sos_notifications._claim_attempt',
            return_value=attempt,
        ), patch(
            'SafetyApp.sos_notifications.renew_attempt_lease',
            return_value=True,
        ), patch(
            'SafetyApp.sos_notifications._record_result',
            side_effect=record_claimed_result,
        ) as record_result:
            result = dispatch_notification_attempt(
                event,
                contact,
                SOSNotificationAttempt.CHANNEL_EMAIL,
                provider=provider,
                dispatch_guard=lambda *_args: True,
            )

        self.assertEqual(event.status, SOSEvent.STATUS_CANCELLED)
        self.assertEqual(result.status, SOSNotificationAttempt.STATUS_ACCEPTED)
        self.assertEqual(result.provider_reference, 'synthetic-provider-reference')
        record_result.assert_called_once()
        self.assertEqual(
            record_result.call_args.kwargs['claim_token'],
            attempt.claim_token,
        )
        self.assertEqual(
            record_result.call_args.kwargs['claim_generation'],
            attempt.claim_generation,
        )

    def test_dispatch_guard_stops_later_channels_after_cancellation(self):
        event = SimpleNamespace(pk=17, user='owner')
        contact = SimpleNamespace(pk=23)
        attempts = {
            SOSNotificationAttempt.CHANNEL_EMAIL: SimpleNamespace(
                pk=1,
                status=SOSNotificationAttempt.STATUS_QUEUED,
                claim_token=None,
                claim_generation=0,
                lease_expires_at=None,
                provider_reference='',
                error_code='',
                error_message='',
                save=Mock(),
            ),
            SOSNotificationAttempt.CHANNEL_VOICE: SimpleNamespace(
                pk=2,
                status=SOSNotificationAttempt.STATUS_QUEUED,
                provider_reference='',
                error_code='',
                error_message='',
                save=Mock(),
            ),
        }
        claimed_attempts = {
            channel: SimpleNamespace(
                pk=attempt.pk,
                status=SOSNotificationAttempt.STATUS_SENDING,
                claim_token=uuid.uuid4(),
                claim_generation=1,
                lease_expires_at=None,
                provider_reference='',
                error_code='',
                error_message='',
                save=Mock(),
            )
            for channel, attempt in attempts.items()
        }
        email_provider = Mock()
        email_provider.channel = SOSNotificationAttempt.CHANNEL_EMAIL
        email_provider.send.return_value = ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
        )
        voice_provider = Mock()
        voice_provider.channel = SOSNotificationAttempt.CHANNEL_VOICE
        voice_provider.send.return_value = ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
        )
        guard = Mock(side_effect=(True, False))
        recorded_claims = []

        def record_claimed_result(
            attempt,
            result,
            claim_token,
            claim_generation,
        ):
            if (
                attempt.claim_token != claim_token
                or attempt.claim_generation != claim_generation
                or attempt.status != SOSNotificationAttempt.STATUS_SENDING
            ):
                return False
            recorded_claims.append((claim_token, claim_generation))
            attempt.status = result.status
            attempt.provider_reference = result.provider_reference
            return True

        def cancel_claimed_attempt(attempt):
            attempt.status = SOSNotificationAttempt.STATUS_CANCELLED
            for prepared_attempt in attempts.values():
                if prepared_attempt.pk == attempt.pk:
                    prepared_attempt.status = SOSNotificationAttempt.STATUS_CANCELLED

        with patch(
            'SafetyApp.sos_notifications._validate_dispatch_target',
        ), patch(
            'SafetyApp.sos_notifications.get_eligible_contacts',
            return_value=[contact],
        ), patch(
            'SafetyApp.sos_notifications._get_or_create_attempt',
            side_effect=[
                attempts[SOSNotificationAttempt.CHANNEL_EMAIL],
                attempts[SOSNotificationAttempt.CHANNEL_VOICE],
            ],
        ), patch(
            'SafetyApp.sos_notifications._claim_attempt',
            side_effect=lambda attempt_id: next(
                claimed
                for claimed in claimed_attempts.values()
                if claimed.pk == attempt_id
            ),
        ), patch(
            'SafetyApp.sos_notifications._record_result',
            side_effect=record_claimed_result,
        ), patch(
            'SafetyApp.sos_notifications._cancel_claimed_attempt',
            side_effect=cancel_claimed_attempt,
        ), patch(
            'SafetyApp.sos_notifications.SOSNotificationAttempt.objects.get',
            side_effect=lambda pk: next(
                attempt for attempt in attempts.values() if attempt.pk == pk
            ),
        ):
            outcomes = dispatch_contact_notifications(
                event,
                contact,
                channels=(
                    SOSNotificationAttempt.CHANNEL_EMAIL,
                    SOSNotificationAttempt.CHANNEL_VOICE,
                ),
                providers={
                    SOSNotificationAttempt.CHANNEL_EMAIL: email_provider,
                    SOSNotificationAttempt.CHANNEL_VOICE: voice_provider,
                },
                dispatch_guard=guard,
            )

        email_provider.send.assert_called_once()
        voice_provider.send.assert_not_called()
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(
            recorded_claims,
            [(
                claimed_attempts[SOSNotificationAttempt.CHANNEL_EMAIL].claim_token,
                claimed_attempts[SOSNotificationAttempt.CHANNEL_EMAIL].claim_generation,
            )],
        )
        self.assertEqual(
            claimed_attempts[SOSNotificationAttempt.CHANNEL_EMAIL].status,
            SOSNotificationAttempt.STATUS_ACCEPTED,
        )
        self.assertEqual(
            attempts[SOSNotificationAttempt.CHANNEL_VOICE].status,
            SOSNotificationAttempt.STATUS_CANCELLED,
        )
        self.assertEqual(len(outcomes), 2)

    def test_cancelled_and_resolved_events_are_rejected(self):
        event = SimpleNamespace(pk=17, user='owner')
        contact = SimpleNamespace(pk=23)
        provider = Mock()
        provider.channel = SOSNotificationAttempt.CHANNEL_EMAIL

        for status in (SOSEvent.STATUS_CANCELLED, SOSEvent.STATUS_RESOLVED):
            with self.subTest(status=status):
                queryset = Mock()
                queryset.values_list.return_value.first.return_value = None
                with patch.object(SOSEvent.objects, 'filter', return_value=queryset):
                    with self.assertRaises(ValueError):
                        dispatch_notification_attempt(
                            event,
                            contact,
                            SOSNotificationAttempt.CHANNEL_EMAIL,
                            provider=provider,
                        )
                provider.send.assert_not_called()

    def test_contact_ownership_and_active_status_are_validated(self):
        event = SimpleNamespace(pk=17, user='different-in-memory-owner')
        contact = SimpleNamespace(pk=23)
        event_queryset = Mock()
        event_queryset.values_list.return_value.first.return_value = 'owner'
        contact_queryset = Mock()
        contact_queryset.exists.return_value = False
        provider = Mock()
        provider.channel = SOSNotificationAttempt.CHANNEL_EMAIL

        with patch.object(SOSEvent.objects, 'filter', return_value=event_queryset), patch.object(
            EmergencyContact.objects,
            'filter',
            return_value=contact_queryset,
        ) as contact_filter:
            with self.assertRaises(ValueError):
                dispatch_notification_attempt(
                    event,
                    contact,
                    SOSNotificationAttempt.CHANNEL_EMAIL,
                    provider=provider,
                )

        contact_filter.assert_called_once_with(pk=contact.pk, user='owner', is_active=True)
        provider.send.assert_not_called()

    def test_explicit_retry_is_limited_and_uncertain_attempts_are_blocked(self):
        failed_first = SimpleNamespace(
            attempt_number=1,
            status=SOSNotificationAttempt.STATUS_FAILED,
        )
        failed_second = SimpleNamespace(
            attempt_number=2,
            status=SOSNotificationAttempt.STATUS_FAILED,
        )
        uncertain = SimpleNamespace(
            attempt_number=1,
            status=SOSNotificationAttempt.STATUS_UNCERTAIN,
        )

        self.assertEqual(_next_attempt_number([failed_first], False, 1), 1)
        self.assertEqual(_next_attempt_number([failed_first], True, 1), 2)
        with self.assertRaises(ValueError):
            _next_attempt_number([failed_second], True, 1)
        with self.assertRaises(ValueError):
            _next_attempt_number([uncertain], True, 1)
        with self.assertRaises(ValueError):
            _next_attempt_number([failed_first], True, 0)
        with self.assertRaises(ValueError):
            _next_attempt_number([failed_first], True, 2)

    def test_contact_dispatch_selects_only_eligible_requested_channels(self):
        event = SimpleNamespace(pk=17, user='owner')
        contact = SimpleNamespace(pk=23)
        eligible = {
            SOSNotificationAttempt.CHANNEL_EMAIL: [contact],
            SOSNotificationAttempt.CHANNEL_SMS: [],
            SOSNotificationAttempt.CHANNEL_VOICE: [contact],
        }
        email_attempt = Mock(channel=SOSNotificationAttempt.CHANNEL_EMAIL)
        voice_attempt = Mock(channel=SOSNotificationAttempt.CHANNEL_VOICE)

        with patch('SafetyApp.sos_notifications._validate_dispatch_target'), patch(
            'SafetyApp.sos_notifications.get_eligible_contacts',
            side_effect=lambda _event, channel: eligible[channel],
        ), patch(
            'SafetyApp.sos_notifications._get_or_create_attempt',
            side_effect=[
                Mock(pk=1),
                Mock(pk=2),
            ],
        ), patch(
            'SafetyApp.sos_notifications.dispatch_notification_attempt',
            side_effect=[email_attempt, voice_attempt],
        ) as dispatch:
            outcomes = dispatch_contact_notifications(event, contact)

        self.assertEqual(outcomes, [email_attempt, voice_attempt])
        self.assertEqual(
            [call.args[2] for call in dispatch.call_args_list],
            [
                SOSNotificationAttempt.CHANNEL_EMAIL,
                SOSNotificationAttempt.CHANNEL_VOICE,
            ],
        )
        self.assertEqual(dispatch.call_count, 2)

    def test_interrupted_sending_attempts_are_marked_uncertain_not_failed(self):
        queryset = Mock()
        queryset.filter.return_value = queryset
        queryset.exclude.return_value = queryset

        with patch.object(
            SOSNotificationAttempt.objects,
            'filter',
            return_value=queryset,
        ) as filter_attempts:
            updated_count = recover_interrupted_attempts()

        self.assertEqual(updated_count, queryset.update.return_value)
        filters = filter_attempts.call_args.kwargs
        self.assertEqual(filters['status'], SOSNotificationAttempt.STATUS_SENDING)
        self.assertEqual(queryset.filter.call_count, 1)
        updates = queryset.update.call_args.kwargs
        self.assertEqual(updates['status'], SOSNotificationAttempt.STATUS_UNCERTAIN)
        self.assertIsNone(updates['claim_token'])
        self.assertIsNone(updates['lease_expires_at'])
        self.assertEqual(updates['error_code'], 'WORKER_INTERRUPTED')
        self.assertIn('Reconcile with the provider', updates['error_message'])
        self.assertNotEqual(updates['status'], SOSNotificationAttempt.STATUS_FAILED)
