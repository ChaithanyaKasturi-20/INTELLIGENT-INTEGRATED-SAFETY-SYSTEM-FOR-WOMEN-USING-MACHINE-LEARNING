import base64
import json
import re
import logging
import os
import smtplib
from email.message import EmailMessage
from threading import Lock
from dataclasses import dataclass
from datetime import timedelta
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    EmergencyContact,
    PersonalSafetyProfile,
    SOSNotificationAttempt,
    SOSEvent,
    normalize_emergency_email,
)


PHONE_NUMBER_PATTERN = re.compile(r'^\+?[0-9\s()-]{8,20}$')
PLACEHOLDER_PHONE_DIGITS = frozenset((
    '0000000000',
    '1234567890',
    '0123456789',
))
logger = logging.getLogger(__name__)
ATTEMPT_LEASE_DURATION = timedelta(minutes=5)
EXOTEL_REQUEST_TIMEOUT_SECONDS = 10
EXOTEL_API_HOSTS = {
    'in': 'api.in.exotel.com',
    'sg': 'api.exotel.com',
}


@dataclass(frozen=True)
class ProviderResult:
    status: str
    provider_reference: str = ''
    error_code: str = ''
    error_message: str = ''


@dataclass(frozen=True)
class ProviderReconciliationResult:
    outcome: str
    provider_reference: str = ''
    error_code: str = ''
    error_message: str = ''


class NotificationProvider(object):
    channel = None

    def send(self, attempt, event, contact):
        """Use attempt.idempotency_key; long calls must renew their claim lease."""
        raise NotImplementedError

    def reconcile(self, attempt):
        raise NotImplementedError


class EmailProvider(NotificationProvider):
    channel = SOSNotificationAttempt.CHANNEL_EMAIL


class SMSProvider(NotificationProvider):
    channel = SOSNotificationAttempt.CHANNEL_SMS


class VoiceProvider(NotificationProvider):
    channel = SOSNotificationAttempt.CHANNEL_VOICE


class MockEmailProvider(EmailProvider):
    def send(self, attempt, event, contact):
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            provider_reference='mock-email-{}'.format(attempt.idempotency_key),
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome='UNKNOWN')


class SMTPEmailProvider(EmailProvider):
    """SMTP adapter for email-only SOS delivery; SMTP acceptance is not receipt."""

    SMTP_HOST = 'smtp.gmail.com'
    SMTP_PORT = 465
    SMTP_TIMEOUT_SECONDS = 20

    def __init__(self, sender_email, sender_password):
        self.sender_email = (sender_email or '').strip()
        self.sender_password = sender_password or ''

    @classmethod
    def from_environment(cls):
        from .views import get_panic_email_config

        sender_email, sender_password = get_panic_email_config()
        return cls(sender_email, sender_password)

    def send(self, attempt, event, contact):
        sender_email = normalize_emergency_email(self.sender_email)
        recipient_email = normalize_emergency_email(contact.email)
        if not sender_email or not _is_valid_email(sender_email):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='SMTP_NOT_CONFIGURED',
                error_message='Set PANIC_EMAIL_USER and PANIC_EMAIL_PASS to enable SMTP email.',
            )
        if not self.sender_password:
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='SMTP_NOT_CONFIGURED',
                error_message='Set PANIC_EMAIL_USER and PANIC_EMAIL_PASS to enable SMTP email.',
            )
        if not recipient_email or not _is_valid_email(recipient_email):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='INVALID_EMAIL_RECIPIENT',
                error_message='The emergency contact has an invalid email address.',
            )

        current_event = SOSEvent.objects.get(pk=event.pk)
        profile = PersonalSafetyProfile.objects.filter(
            username=current_event.user,
            medical_consent=True,
        ).first()
        sender_name = (
            profile.full_name.strip()
            if profile is not None and profile.full_name.strip()
            else current_event.user
        )
        emergency_instructions = (
            profile.emergency_instructions.strip()
            if profile is not None
            else ''
        )
        from .views import build_sos_email_body

        message = EmailMessage()
        message['From'] = sender_email
        message['To'] = recipient_email
        message['Subject'] = 'SOS Emergency Alert'
        message.set_content(build_sos_email_body(
            current_event,
            sender_name,
            emergency_instructions=emergency_instructions,
        ))

        submission_started = False
        try:
            with smtplib.SMTP_SSL(
                self.SMTP_HOST,
                self.SMTP_PORT,
                timeout=self.SMTP_TIMEOUT_SECONDS,
            ) as smtp:
                smtp.login(sender_email, self.sender_password)
                submission_started = True
                refused_recipients = smtp.sendmail(
                    sender_email,
                    [recipient_email],
                    message.as_string(),
                )
            if refused_recipients:
                return ProviderResult(
                    status=SOSNotificationAttempt.STATUS_FAILED,
                    error_code='SMTP_RECIPIENT_REFUSED',
                    error_message='The SMTP server refused this recipient.',
                )
        except smtplib.SMTPAuthenticationError:
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='SMTP_AUTHENTICATION_FAILED',
                error_message='The SMTP server rejected the sender credentials.',
            )
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPResponseException,
        ) as exc:
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='SMTP_RESPONSE_{}'.format(
                    getattr(exc, 'smtp_code', 'REJECTED')
                ),
                error_message='The SMTP server rejected the email submission.',
            )
        except (
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPException,
            TimeoutError,
            OSError,
        ) as exc:
            if submission_started:
                return ProviderResult(
                    status=SOSNotificationAttempt.STATUS_UNCERTAIN,
                    error_code=type(exc).__name__,
                    error_message='The SMTP outcome could not be confirmed; do not retry automatically.',
                )
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message='Could not connect to or authenticate with the SMTP server.',
            )

        logger.info(
            'SOS email accepted by SMTP (attempt id %s).',
            attempt.pk,
        )
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            provider_reference='smtp-accepted-{}'.format(attempt.idempotency_key),
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome='UNKNOWN')


class MockSMSProvider(SMSProvider):
    def send(self, attempt, event, contact):
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            provider_reference='mock-sms-{}'.format(attempt.idempotency_key),
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome='UNKNOWN')


class MockVoiceProvider(VoiceProvider):
    def send(self, attempt, event, contact):
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            provider_reference='mock-voice-{}'.format(attempt.idempotency_key),
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome='UNKNOWN')


class DisabledNotificationProvider(NotificationProvider):
    def __init__(self, channel):
        self.channel = channel

    def send(self, attempt, event, contact):
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_FAILED,
            error_code='PROVIDER_DISABLED',
            error_message='The notification provider is disabled.',
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome=RECONCILIATION_UNKNOWN)


class ConfigurationFailureProvider(NotificationProvider):
    def __init__(self, channel, error_type):
        self.channel = channel
        self.error_type = error_type

    def send(self, attempt, event, contact):
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_FAILED,
            error_code='PROVIDER_CONFIGURATION_INVALID',
            error_message='The {} notification provider configuration is invalid ({}).'.format(
                self.channel,
                self.error_type,
            ),
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome=RECONCILIATION_UNKNOWN)


def _build_exotel_post_request(url, parameters, api_key, api_token):
    credentials = base64.b64encode(
        '{}:{}'.format(api_key, api_token).encode('utf-8')
    ).decode('ascii')
    return urllib.request.Request(
        url,
        data=urllib.parse.urlencode(parameters).encode('ascii'),
        headers={
            'Authorization': 'Basic {}'.format(credentials),
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )


def _send_exotel_request(request):
    try:
        with urllib.request.urlopen(
            request,
            timeout=EXOTEL_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            status_code = response.getcode()
            response_body = response.read(65537)
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        response_body = b''
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ProviderResult(
            status=SOSNotificationAttempt.STATUS_UNCERTAIN,
            error_code='EXOTEL_TRANSPORT_ERROR',
            error_message='The provider outcome could not be confirmed.',
        )

    if status_code != 200:
        is_uncertain = status_code >= 500
        return None, ProviderResult(
            status=(
                SOSNotificationAttempt.STATUS_UNCERTAIN
                if is_uncertain
                else SOSNotificationAttempt.STATUS_FAILED
            ),
            error_code='EXOTEL_HTTP_{}'.format(status_code),
            error_message=(
                'The provider outcome could not be confirmed.'
                if is_uncertain
                else 'The provider rejected the request.'
            ),
        )
    if len(response_body) > 65536:
        return None, ProviderResult(
            status=SOSNotificationAttempt.STATUS_UNCERTAIN,
            error_code='EXOTEL_INVALID_RESPONSE',
            error_message='The provider outcome could not be confirmed.',
        )
    return response_body, None


class ExotelSMSProvider(SMSProvider):
    """Exotel SMS adapter; enable only with explicitly supplied runtime config."""

    def __init__(
        self,
        api_key,
        api_token,
        account_sid,
        region,
        sender_id,
        message_body,
        dlt_entity_id='',
        dlt_template_id='',
    ):
        if region not in EXOTEL_API_HOSTS:
            raise ValueError('Exotel SMS region must be either "in" or "sg".')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', account_sid or ''):
            raise ValueError('Exotel account SID is invalid.')
        if not api_key or not api_token or not sender_id:
            raise ValueError('Exotel SMS credentials and sender ID are required.')
        if not message_body or len(message_body) > 2000:
            raise ValueError('Exotel SMS body must contain 1 to 2000 characters.')
        if region == 'in' and not dlt_entity_id:
            raise ValueError('An Exotel DLT entity ID is required for the India region.')

        self.api_key = api_key
        self.api_token = api_token
        self.account_sid = account_sid
        self.region = region
        self.sender_id = sender_id
        self.message_body = message_body
        self.dlt_entity_id = dlt_entity_id
        self.dlt_template_id = dlt_template_id

    @classmethod
    def from_environment(cls):
        names = {
            'api_key': 'SOS_EXOTEL_API_KEY',
            'api_token': 'SOS_EXOTEL_API_TOKEN',
            'account_sid': 'SOS_EXOTEL_ACCOUNT_SID',
            'region': 'SOS_EXOTEL_REGION',
            'sender_id': 'SOS_EXOTEL_SMS_SENDER_ID',
            'message_body': 'SOS_EXOTEL_SMS_BODY',
            'dlt_entity_id': 'SOS_EXOTEL_DLT_ENTITY_ID',
            'dlt_template_id': 'SOS_EXOTEL_DLT_TEMPLATE_ID',
        }
        config = {
            setting: os.environ.get(environment_name, '').strip()
            for setting, environment_name in names.items()
        }
        config['region'] = config['region'].lower()
        required = (
            'api_key',
            'api_token',
            'account_sid',
            'region',
            'sender_id',
            'message_body',
        )
        missing = [names[name] for name in required if not config[name]]
        if missing:
            raise ValueError(
                'Exotel SMS is enabled but required runtime settings are missing: {}.'.format(
                    ', '.join(missing)
                )
            )
        return cls(**config)

    def send(self, attempt, event, contact):
        recipient = re.sub(r'[\s()-]', '', (contact.phone_number or '').strip())
        if not re.fullmatch(r'\+[0-9]{8,15}', recipient):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='INVALID_E164_RECIPIENT',
                error_message='The recipient phone number is not in E.164 format.',
            )

        url = 'https://{}/v1/Accounts/{}/Sms/send'.format(
            EXOTEL_API_HOSTS[self.region],
            self.account_sid,
        )
        parameters = {
            'From': self.sender_id,
            'To': recipient,
            'Body': self.message_body,
            'CustomField': str(attempt.idempotency_key),
        }
        if self.dlt_entity_id:
            parameters['DltEntityId'] = self.dlt_entity_id
        if self.dlt_template_id:
            parameters['DltTemplateId'] = self.dlt_template_id

        request = _build_exotel_post_request(
            url,
            parameters,
            self.api_key,
            self.api_token,
        )
        response_body, failure = _send_exotel_request(request)
        if failure is not None:
            return failure
        try:
            payload = json.loads(response_body.decode('utf-8'))
            message = payload.get('SMSMessage') if isinstance(payload, dict) else None
            provider_reference = message.get('Sid') if isinstance(message, dict) else None
        except (UnicodeDecodeError, ValueError, AttributeError):
            provider_reference = None

        if (
            not isinstance(provider_reference, str)
            or not provider_reference.strip()
            or len(provider_reference.strip()) > 255
        ):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_UNCERTAIN,
                error_code='EXOTEL_INVALID_RESPONSE',
                error_message='The provider outcome could not be confirmed.',
            )
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_ACCEPTED,
            provider_reference=provider_reference.strip(),
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome=RECONCILIATION_UNKNOWN)


class ExotelVoiceProvider(VoiceProvider):
    """Exotel call-flow adapter; acceptance does not imply answer or playback."""

    def __init__(
        self,
        api_key,
        api_token,
        account_sid,
        region,
        caller_id,
        app_id,
    ):
        if region not in EXOTEL_API_HOSTS:
            raise ValueError('Exotel voice region must be either "in" or "sg".')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', account_sid or ''):
            raise ValueError('Exotel account SID is invalid.')
        if not api_key or not api_token:
            raise ValueError('Exotel voice credentials are required.')
        if not re.fullmatch(r'\+?[0-9]{5,20}', caller_id or ''):
            raise ValueError('Exotel voice caller ID must be a phone number.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id or ''):
            raise ValueError('Exotel voice app ID is invalid.')

        self.api_key = api_key
        self.api_token = api_token
        self.account_sid = account_sid
        self.region = region
        self.caller_id = caller_id
        self.app_id = app_id

    @classmethod
    def from_environment(cls):
        names = {
            'api_key': 'SOS_EXOTEL_API_KEY',
            'api_token': 'SOS_EXOTEL_API_TOKEN',
            'account_sid': 'SOS_EXOTEL_ACCOUNT_SID',
            'region': 'SOS_EXOTEL_REGION',
            'caller_id': 'SOS_EXOTEL_VOICE_CALLER_ID',
            'app_id': 'SOS_EXOTEL_VOICE_APP_ID',
        }
        config = {
            setting: os.environ.get(environment_name, '').strip()
            for setting, environment_name in names.items()
        }
        config['region'] = config['region'].lower()
        missing = [names[name] for name, value in config.items() if not value]
        if missing:
            raise ValueError(
                'Exotel voice is enabled but required runtime settings are missing: {}.'.format(
                    ', '.join(missing)
                )
            )
        return cls(**config)

    def send(self, attempt, event, contact):
        recipient = re.sub(r'[\s()-]', '', (contact.phone_number or '').strip())
        if not re.fullmatch(r'\+[0-9]{8,15}', recipient):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                error_code='INVALID_E164_RECIPIENT',
                error_message='The recipient phone number is not in E.164 format.',
            )

        url = 'https://{}/v1/Accounts/{}/Calls/connect'.format(
            EXOTEL_API_HOSTS[self.region],
            self.account_sid,
        )
        flow_url = 'http://my.exotel.com/{}/exoml/start_voice/{}'.format(
            self.account_sid,
            self.app_id,
        )
        request = _build_exotel_post_request(
            url,
            {
                'From': recipient,
                'CallerId': self.caller_id,
                'Url': flow_url,
                'CallType': 'trans',
                'CustomField': str(attempt.idempotency_key),
            },
            self.api_key,
            self.api_token,
        )
        response_body, failure = _send_exotel_request(request)
        if failure is not None:
            return failure

        try:
            payload = json.loads(response_body.decode('utf-8'))
            call = payload.get('Call') if isinstance(payload, dict) else None
            provider_reference = call.get('Sid') if isinstance(call, dict) else None
            call_status = call.get('Status') if isinstance(call, dict) else None
        except (UnicodeDecodeError, ValueError, AttributeError):
            provider_reference = None
            call_status = None

        if (
            not isinstance(provider_reference, str)
            or not provider_reference.strip()
            or len(provider_reference.strip()) > 255
            or not isinstance(call_status, str)
        ):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_UNCERTAIN,
                error_code='EXOTEL_INVALID_RESPONSE',
                error_message='The provider outcome could not be confirmed.',
            )

        normalized_status = call_status.strip().lower()
        if normalized_status in ('queued', 'in-progress'):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_ACCEPTED,
                provider_reference=provider_reference.strip(),
            )
        if normalized_status in ('failed', 'busy', 'no-answer', 'canceled', 'cancelled'):
            return ProviderResult(
                status=SOSNotificationAttempt.STATUS_FAILED,
                provider_reference=provider_reference.strip(),
                error_code='EXOTEL_CALL_{}'.format(normalized_status.upper().replace('-', '_')),
                error_message='The provider reported that the call did not proceed.',
            )
        return ProviderResult(
            status=SOSNotificationAttempt.STATUS_UNCERTAIN,
            provider_reference=provider_reference.strip(),
            error_code='EXOTEL_INVALID_CALL_STATUS',
            error_message='The provider outcome could not be confirmed.',
        )

    def reconcile(self, attempt):
        return ProviderReconciliationResult(outcome=RECONCILIATION_UNKNOWN)


DEFAULT_PROVIDERS = {
    SOSNotificationAttempt.CHANNEL_EMAIL: MockEmailProvider(),
    SOSNotificationAttempt.CHANNEL_SMS: MockSMSProvider(),
    SOSNotificationAttempt.CHANNEL_VOICE: MockVoiceProvider(),
}
DISABLED_PROVIDERS = {
    SOSNotificationAttempt.CHANNEL_EMAIL: DisabledNotificationProvider(
        SOSNotificationAttempt.CHANNEL_EMAIL,
    ),
    SOSNotificationAttempt.CHANNEL_SMS: DisabledNotificationProvider(
        SOSNotificationAttempt.CHANNEL_SMS,
    ),
    SOSNotificationAttempt.CHANNEL_VOICE: DisabledNotificationProvider(
        SOSNotificationAttempt.CHANNEL_VOICE,
    ),
}

PROVIDER_OUTCOME_STATUSES = frozenset((
    SOSNotificationAttempt.STATUS_ACCEPTED,
    SOSNotificationAttempt.STATUS_DELIVERED,
    SOSNotificationAttempt.STATUS_FAILED,
    SOSNotificationAttempt.STATUS_UNCERTAIN,
))
RETRYABLE_OUTCOME_STATUSES = frozenset((
    SOSNotificationAttempt.STATUS_FAILED,
    SOSNotificationAttempt.STATUS_NO_ANSWER,
    SOSNotificationAttempt.STATUS_BUSY,
))
MAX_ADDITIONAL_RETRIES_PER_CHANNEL = 1
_ACTIVE_DISPATCH_ATTEMPT_IDS = set()
_ACTIVE_DISPATCH_ATTEMPTS_LOCK = Lock()
RECONCILIATION_DELIVERED = 'DELIVERED'
RECONCILIATION_ACCEPTED = 'ACCEPTED'
RECONCILIATION_NOT_DELIVERED = 'NOT_DELIVERED'
RECONCILIATION_UNKNOWN = 'UNKNOWN'
RECONCILIATION_OUTCOMES = frozenset((
    RECONCILIATION_DELIVERED,
    RECONCILIATION_ACCEPTED,
    RECONCILIATION_NOT_DELIVERED,
    RECONCILIATION_UNKNOWN,
))


def get_default_provider(channel):
    if channel not in DEFAULT_PROVIDERS:
        raise ValueError('Unsupported SOS notification channel: {}'.format(channel))
    if channel == SOSNotificationAttempt.CHANNEL_EMAIL:
        smtp_enabled = _environment_flag('SOS_SMTP_EMAIL_ENABLED', False)
        mock_enabled = _environment_flag(
            'SOS_MOCK_EMAIL_ENABLED',
            not smtp_enabled,
        )
        if smtp_enabled and mock_enabled:
            raise ValueError(
                'Only one email notification provider may be enabled at a time.'
            )
        if smtp_enabled:
            return SMTPEmailProvider.from_environment()
        if not mock_enabled:
            return DISABLED_PROVIDERS[channel]
        return DEFAULT_PROVIDERS[channel]
    if channel in (SOSNotificationAttempt.CHANNEL_SMS, SOSNotificationAttempt.CHANNEL_VOICE):
        channel_name = 'SMS' if channel == SOSNotificationAttempt.CHANNEL_SMS else 'VOICE'
        mock_flag = 'SOS_MOCK_{}_ENABLED'.format(channel_name)
        exotel_flag = 'SOS_EXOTEL_{}_ENABLED'.format(channel_name)
        exotel_enabled = _environment_flag(exotel_flag, False)
        mock_enabled = _environment_flag(mock_flag, not exotel_enabled)
        if exotel_enabled and mock_enabled:
            raise ValueError(
                'Only one {} notification provider may be enabled at a time.'.format(
                    channel_name.lower()
                )
            )
        if exotel_enabled:
            provider_type = (
                ExotelSMSProvider
                if channel == SOSNotificationAttempt.CHANNEL_SMS
                else ExotelVoiceProvider
            )
            return provider_type.from_environment()
        if not mock_enabled:
            return DISABLED_PROVIDERS[channel]
    return DEFAULT_PROVIDERS[channel]


def _environment_flag(name, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ('1', 'true', 'yes', 'on'):
        return True
    if normalized in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError('{} must be a boolean setting.'.format(name))


def is_valid_phone_number(value):
    phone_number = (value or '').strip()
    if not PHONE_NUMBER_PATTERN.fullmatch(phone_number):
        return False

    digits = ''.join(character for character in phone_number if character.isdigit())
    return (
        len(digits) >= 8
        and len(set(digits)) > 1
        and digits not in PLACEHOLDER_PHONE_DIGITS
    )


def _is_valid_email(value):
    email = normalize_emergency_email(value)
    if not email:
        return False
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def get_eligible_contacts(event, channel):
    if channel not in dict(SOSNotificationAttempt.CHANNEL_CHOICES):
        raise ValueError('Unsupported SOS notification channel: {}'.format(channel))

    contacts = EmergencyContact.objects.filter(
        user=event.user,
        is_active=True,
    ).order_by('-is_primary', 'pk')

    eligible = []
    for contact in contacts:
        if channel == SOSNotificationAttempt.CHANNEL_EMAIL:
            is_eligible = (
                bool(contact.email)
                and contact.email_consent
                and contact.email_verified_at is not None
                and _is_valid_email(contact.email)
            )
        else:
            has_consent = (
                contact.sms_consent
                if channel == SOSNotificationAttempt.CHANNEL_SMS
                else contact.voice_consent
            )
            is_eligible = (
                has_consent
                and contact.phone_verified_at is not None
                and is_valid_phone_number(contact.phone_number)
            )
        if is_eligible:
            eligible.append(contact)
    return eligible


def _next_attempt_number(attempts, retry_requested, max_retries_per_channel):
    if not isinstance(max_retries_per_channel, int) or not (
        0 <= max_retries_per_channel <= MAX_ADDITIONAL_RETRIES_PER_CHANNEL
    ):
        raise ValueError('At most one additional retry per channel is supported.')

    if not attempts:
        if retry_requested:
            raise ValueError('A retry requires a previous notification attempt.')
        return 1

    latest = max(attempts, key=lambda attempt: attempt.attempt_number)
    if not retry_requested:
        return latest.attempt_number

    if latest.status == SOSNotificationAttempt.STATUS_UNCERTAIN:
        raise ValueError('Uncertain notification outcomes require provider reconciliation.')
    if latest.status not in RETRYABLE_OUTCOME_STATUSES:
        raise ValueError('The previous notification outcome is not eligible for retry.')
    retries_used = latest.attempt_number - 1
    if retries_used >= max_retries_per_channel:
        raise ValueError('The maximum retry count for this channel has been reached.')
    return latest.attempt_number + 1


def _get_or_create_attempt(
    event,
    contact,
    channel,
    retry_requested=False,
    max_retries_per_channel=MAX_ADDITIONAL_RETRIES_PER_CHANNEL,
):
    attempt_queryset = SOSNotificationAttempt.objects.filter(
        sos_event=event,
        emergency_contact=contact,
        channel=channel,
    )
    try:
        with transaction.atomic():
            attempts = list(attempt_queryset.select_for_update().order_by('-attempt_number'))
            attempt_number = _next_attempt_number(
                attempts,
                retry_requested,
                max_retries_per_channel,
            )
            if attempts and attempt_number == attempts[0].attempt_number:
                return attempts[0]
            return SOSNotificationAttempt.objects.create(
                sos_event=event,
                emergency_contact=contact,
                channel=channel,
                attempt_number=attempt_number,
                status=SOSNotificationAttempt.STATUS_QUEUED,
                scheduled_at=timezone.now(),
            )
    except IntegrityError:
        return SOSNotificationAttempt.objects.get(
            sos_event=event,
            emergency_contact=contact,
            channel=channel,
            attempt_number=attempt_number,
        )


def _claim_attempt(attempt_id, lease_duration=ATTEMPT_LEASE_DURATION):
    if lease_duration <= timedelta(0):
        raise ValueError('The attempt lease duration must be positive.')
    now = timezone.now()
    claim_token = uuid.uuid4()
    claimed = SOSNotificationAttempt.objects.filter(
        pk=attempt_id,
        status=SOSNotificationAttempt.STATUS_QUEUED,
    ).update(
        status=SOSNotificationAttempt.STATUS_SENDING,
        started_at=now,
        claim_token=claim_token,
        claim_generation=F('claim_generation') + 1,
        lease_expires_at=now + lease_duration,
        updated_at=now,
    )
    if not claimed:
        return None

    return SOSNotificationAttempt.objects.get(pk=attempt_id)


def renew_attempt_lease(attempt_id, claim_token, claim_generation,
                        lease_duration=ATTEMPT_LEASE_DURATION):
    if lease_duration <= timedelta(0):
        raise ValueError('The attempt lease duration must be positive.')
    now = timezone.now()
    return SOSNotificationAttempt.objects.filter(
        pk=attempt_id,
        status=SOSNotificationAttempt.STATUS_SENDING,
        claim_token=claim_token,
        claim_generation=claim_generation,
        lease_expires_at__gt=now,
    ).update(
        lease_expires_at=now + lease_duration,
        updated_at=now,
    ) == 1


def _record_result(attempt, result, claim_token=None, claim_generation=None):
    if not isinstance(result, ProviderResult):
        raise TypeError('Providers must return a ProviderResult.')
    if result.status not in PROVIDER_OUTCOME_STATUSES:
        raise ValueError('Provider returned an unsupported notification outcome.')
    if not isinstance(attempt, SOSNotificationAttempt):
        raise TypeError('A persisted SOSNotificationAttempt is required.')
    if attempt.pk is None or attempt._state.adding or attempt._state.db is None:
        logger.error('Notification result rejected because the attempt is not persisted.')
        return False

    if claim_token is None:
        claim_token = attempt.claim_token
    claim_generation = (
        attempt.claim_generation if claim_generation is None else claim_generation
    )
    if claim_token is None or attempt.pk is None:
        logger.error('Notification result rejected because the attempt has no current claim.')
        return False

    now = timezone.now()
    updated = SOSNotificationAttempt.objects.using(attempt._state.db).filter(
        pk=attempt.pk,
        status=SOSNotificationAttempt.STATUS_SENDING,
        claim_token=claim_token,
        claim_generation=claim_generation,
        lease_expires_at__gt=now,
    ).update(
        status=result.status,
        provider_reference=result.provider_reference or '',
        error_code=result.error_code or '',
        error_message=result.error_message or '',
        completed_at=now,
        claim_token=None,
        lease_expires_at=None,
        updated_at=now,
    )
    if updated != 1:
        logger.error(
            'Notification result rejected after attempt ownership changed (attempt id %s).',
            attempt.pk,
        )
        return False

    attempt.status = result.status
    attempt.provider_reference = result.provider_reference or ''
    attempt.error_code = result.error_code or ''
    attempt.error_message = result.error_message or ''
    attempt.completed_at = now
    attempt.claim_token = None
    attempt.lease_expires_at = None
    return True


def reconcile_uncertain_attempt(attempt_id, provider=None,
                                lease_duration=ATTEMPT_LEASE_DURATION):
    if connection.in_atomic_block:
        raise RuntimeError('Provider reconciliation must run outside database transactions.')
    if lease_duration <= timedelta(0):
        raise ValueError('The reconciliation lease duration must be positive.')

    attempt = SOSNotificationAttempt.objects.filter(pk=attempt_id).first()
    if attempt is None:
        raise SOSNotificationAttempt.DoesNotExist()
    if attempt.status != SOSNotificationAttempt.STATUS_UNCERTAIN:
        raise ValueError('Only uncertain notification attempts can be reconciled.')
    if provider is None:
        provider = get_default_provider(attempt.channel)
    if provider.channel != attempt.channel:
        raise ValueError('Provider channel does not match the notification attempt.')

    now = timezone.now()
    claim_token = uuid.uuid4()
    claimed = SOSNotificationAttempt.objects.filter(
        pk=attempt_id,
        status=SOSNotificationAttempt.STATUS_UNCERTAIN,
    ).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now),
    ).update(
        claim_token=claim_token,
        claim_generation=F('claim_generation') + 1,
        lease_expires_at=now + lease_duration,
        updated_at=now,
    )
    if not claimed:
        return SOSNotificationAttempt.objects.get(pk=attempt_id)

    attempt = SOSNotificationAttempt.objects.get(pk=attempt_id)
    try:
        outcome = provider.reconcile(attempt)
    except Exception as exc:
        logger.warning(
            'Notification reconciliation remained unknown (attempt id %s, error type %s).',
            attempt_id,
            type(exc).__name__,
        )
        outcome = ProviderReconciliationResult(outcome=RECONCILIATION_UNKNOWN)

    if (
        not isinstance(outcome, ProviderReconciliationResult)
        or outcome.outcome not in RECONCILIATION_OUTCOMES
    ):
        raise ValueError('Provider returned an unsupported reconciliation outcome.')

    resolved_status = {
        RECONCILIATION_DELIVERED: SOSNotificationAttempt.STATUS_DELIVERED,
        RECONCILIATION_ACCEPTED: SOSNotificationAttempt.STATUS_ACCEPTED,
        RECONCILIATION_NOT_DELIVERED: SOSNotificationAttempt.STATUS_FAILED,
        RECONCILIATION_UNKNOWN: SOSNotificationAttempt.STATUS_UNCERTAIN,
    }[outcome.outcome]
    now = timezone.now()
    reconciliation_updates = {
        'status': resolved_status,
        'claim_token': None,
        'lease_expires_at': None,
        'updated_at': now,
    }
    if outcome.provider_reference:
        reconciliation_updates['provider_reference'] = outcome.provider_reference
    if resolved_status != SOSNotificationAttempt.STATUS_UNCERTAIN:
        reconciliation_updates.update({
            'error_code': outcome.error_code or '',
            'error_message': outcome.error_message or '',
            'completed_at': now,
        })
    updated = SOSNotificationAttempt.objects.filter(
        pk=attempt_id,
        status=SOSNotificationAttempt.STATUS_UNCERTAIN,
        claim_token=claim_token,
        claim_generation=attempt.claim_generation,
        lease_expires_at__gt=now,
    ).update(**reconciliation_updates)
    if updated != 1:
        logger.error(
            'Reconciliation result rejected after attempt ownership changed (attempt id %s).',
            attempt_id,
        )
    return SOSNotificationAttempt.objects.get(pk=attempt_id)


def _validate_dispatch_target(event, contact):
    if event is None or event.pk is None:
        raise ValueError('Notifications can only be dispatched for an active SOS event.')
    event_owner = SOSEvent.objects.filter(
        pk=event.pk,
        status=SOSEvent.STATUS_ACTIVE,
    ).values_list('user', flat=True).first()
    if event_owner is None:
        raise ValueError('Notifications can only be dispatched for an active SOS event.')

    if contact is None or contact.pk is None or not EmergencyContact.objects.filter(
        pk=contact.pk,
        user=event_owner,
        is_active=True,
    ).exists():
        raise ValueError('The emergency contact must be active and belong to the SOS event user.')


def _cancel_claimed_attempt(attempt):
    now = timezone.now()
    SOSNotificationAttempt.objects.filter(
        pk=attempt.pk,
        status=SOSNotificationAttempt.STATUS_SENDING,
        claim_token=attempt.claim_token,
        claim_generation=attempt.claim_generation,
        lease_expires_at__gt=now,
    ).update(
        status=SOSNotificationAttempt.STATUS_CANCELLED,
        completed_at=now,
        claim_token=None,
        lease_expires_at=None,
        updated_at=now,
    )


def _cancel_queued_attempt(attempt):
    now = timezone.now()
    SOSNotificationAttempt.objects.filter(
        pk=attempt.pk,
        status=SOSNotificationAttempt.STATUS_QUEUED,
    ).update(
        status=SOSNotificationAttempt.STATUS_CANCELLED,
        completed_at=now,
        updated_at=now,
    )


def dispatch_notification_attempt(
    event,
    contact,
    channel,
    provider=None,
    retry_requested=False,
    max_retries_per_channel=MAX_ADDITIONAL_RETRIES_PER_CHANNEL,
    attempt=None,
    dispatch_guard=None,
):
    if connection.in_atomic_block:
        raise RuntimeError(
            'Notification provider dispatch must run outside database transactions.'
        )
    if channel not in dict(SOSNotificationAttempt.CHANNEL_CHOICES):
        raise ValueError('Unsupported SOS notification channel: {}'.format(channel))

    try:
        _validate_dispatch_target(event, contact)
    except ValueError:
        if (
            attempt is not None
            and dispatch_guard is not None
            and not dispatch_guard(event, contact)
        ):
            _cancel_queued_attempt(attempt)
            return SOSNotificationAttempt.objects.get(pk=attempt.pk)
        raise
    if contact not in get_eligible_contacts(event, channel):
        raise ValueError('The emergency contact is not eligible for this notification channel.')

    if provider is None:
        provider = get_default_provider(channel)
    if provider.channel != channel:
        raise ValueError('Provider channel does not match the notification attempt.')

    if attempt is None:
        attempt = _get_or_create_attempt(
            event,
            contact,
            channel,
            retry_requested=retry_requested,
            max_retries_per_channel=max_retries_per_channel,
        )
    with _ACTIVE_DISPATCH_ATTEMPTS_LOCK:
        _ACTIVE_DISPATCH_ATTEMPT_IDS.add(attempt.pk)
    try:
        claimed_attempt = _claim_attempt(attempt.pk)
        if claimed_attempt is None:
            return SOSNotificationAttempt.objects.get(pk=attempt.pk)

        if isinstance(claimed_attempt, SOSNotificationAttempt) and not renew_attempt_lease(
            claimed_attempt.pk,
            claimed_attempt.claim_token,
            claimed_attempt.claim_generation,
        ):
            logger.warning(
                'Notification dispatch skipped after attempt lease ownership changed '
                '(attempt id %s).',
                claimed_attempt.pk,
            )
            return SOSNotificationAttempt.objects.get(pk=attempt.pk)

        if dispatch_guard is not None and not dispatch_guard(event, contact):
            _cancel_claimed_attempt(claimed_attempt)
            return SOSNotificationAttempt.objects.get(pk=attempt.pk)

        claim_token = claimed_attempt.claim_token
        claim_generation = claimed_attempt.claim_generation
        try:
            result = provider.send(claimed_attempt, event, contact)
        except Exception as exc:
            recorded = _record_result(
                claimed_attempt,
                ProviderResult(
                    status=SOSNotificationAttempt.STATUS_UNCERTAIN,
                    error_code=type(exc).__name__,
                    error_message='Provider outcome could not be confirmed.',
                ),
                claim_token=claim_token,
                claim_generation=claim_generation,
            )
            if not recorded:
                return SOSNotificationAttempt.objects.get(pk=attempt.pk)
            return claimed_attempt

        if not _record_result(
            claimed_attempt,
            result,
            claim_token=claim_token,
            claim_generation=claim_generation,
        ):
            return SOSNotificationAttempt.objects.get(pk=attempt.pk)
        return claimed_attempt
    finally:
        with _ACTIVE_DISPATCH_ATTEMPTS_LOCK:
            _ACTIVE_DISPATCH_ATTEMPT_IDS.discard(attempt.pk)


def dispatch_contact_notifications(
    event,
    contact,
    channels=None,
    providers=None,
    retry_requested=False,
    max_retries_per_channel=MAX_ADDITIONAL_RETRIES_PER_CHANNEL,
    retry_channels=None,
    on_attempts_prepared=None,
    dispatch_guard=None,
):
    """Prepare every channel, then submit each provider call synchronously.

    The application has no task queue or async provider runner. Channels are
    tracked independently and all are prepared before submission, but provider
    calls are sequential; this function does not claim parallel execution.
    """
    _validate_dispatch_target(event, contact)
    if channels is None:
        channels = (
            SOSNotificationAttempt.CHANNEL_EMAIL,
            SOSNotificationAttempt.CHANNEL_SMS,
            SOSNotificationAttempt.CHANNEL_VOICE,
        )
    channels = tuple(channels)
    if len(set(channels)) != len(channels):
        raise ValueError('Notification channels must not contain duplicates.')

    if providers is None:
        providers = {}
    retry_channels = set(retry_channels or ())
    if not retry_channels.issubset(set(channels)):
        raise ValueError('Retry channels must be included in the dispatch channels.')

    prepared_attempts = []
    for channel in channels:
        if channel not in dict(SOSNotificationAttempt.CHANNEL_CHOICES):
            raise ValueError('Unsupported SOS notification channel: {}'.format(channel))
        if contact not in get_eligible_contacts(event, channel):
            continue
        channel_retry_requested = retry_requested or channel in retry_channels
        attempt = _get_or_create_attempt(
            event,
            contact,
            channel,
            retry_requested=channel_retry_requested,
            max_retries_per_channel=max_retries_per_channel,
        )
        prepared_attempts.append((channel, attempt, channel_retry_requested))

    if on_attempts_prepared is not None and prepared_attempts:
        may_dispatch = on_attempts_prepared(
            [attempt for _, attempt, _ in prepared_attempts]
        )
        if not may_dispatch:
            for _, attempt, _ in prepared_attempts:
                SOSNotificationAttempt.objects.filter(
                    pk=attempt.pk,
                    status=SOSNotificationAttempt.STATUS_QUEUED,
                ).update(
                    status=SOSNotificationAttempt.STATUS_CANCELLED,
                    completed_at=timezone.now(),
                    updated_at=timezone.now(),
                )
            return [
                SOSNotificationAttempt.objects.get(pk=attempt.pk)
                for _, attempt, _ in prepared_attempts
            ]

    outcomes = []
    for channel, attempt, channel_retry_requested in prepared_attempts:
        if channel in providers:
            provider = providers[channel]
        else:
            try:
                provider = get_default_provider(channel)
            except ValueError as exc:
                logger.error(
                    'SOS notification provider configuration is invalid '
                    '(event id %s, channel %s, error type %s): %s',
                    event.pk,
                    channel,
                    type(exc).__name__,
                    exc,
                )
                provider = ConfigurationFailureProvider(
                    channel,
                    type(exc).__name__,
                )
        outcomes.append(
            dispatch_notification_attempt(
                event,
                contact,
                channel,
                provider=provider,
                retry_requested=channel_retry_requested,
                max_retries_per_channel=max_retries_per_channel,
                attempt=attempt,
                dispatch_guard=dispatch_guard,
            )
        )
    return outcomes


def recover_interrupted_attempts(stale_after=timedelta(minutes=5)):
    if stale_after <= timedelta(0):
        raise ValueError('The stale-attempt threshold must be positive.')

    now = timezone.now()
    with _ACTIVE_DISPATCH_ATTEMPTS_LOCK:
        active_attempt_ids = tuple(_ACTIVE_DISPATCH_ATTEMPT_IDS)

    stale_attempts = SOSNotificationAttempt.objects.filter(
        status=SOSNotificationAttempt.STATUS_SENDING,
    ).filter(
        Q(lease_expires_at__lte=now)
        | Q(
            lease_expires_at__isnull=True,
        ) & (
            Q(started_at__lte=now - stale_after)
            | Q(started_at__isnull=True)
        ),
    )
    if active_attempt_ids:
        stale_attempts = stale_attempts.exclude(pk__in=active_attempt_ids)
    return stale_attempts.update(
        status=SOSNotificationAttempt.STATUS_UNCERTAIN,
        completed_at=now,
        claim_token=None,
        claim_generation=F('claim_generation') + 1,
        lease_expires_at=None,
        error_code='WORKER_INTERRUPTED',
        error_message=(
            'The worker stopped before recording a provider outcome. '
            'Reconcile with the provider before any retry.'
        ),
        updated_at=now,
    )
