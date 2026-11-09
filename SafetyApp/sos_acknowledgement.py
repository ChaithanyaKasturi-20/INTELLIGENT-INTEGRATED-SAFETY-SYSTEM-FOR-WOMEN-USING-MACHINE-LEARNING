import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta

from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from .models import (
    EmergencyContact,
    SOSContactAcknowledgement,
    SOSEscalationState,
    SOSEvent,
    SOSNotificationAttempt,
)


DEFAULT_ACKNOWLEDGEMENT_TOKEN_TTL = timedelta(hours=24)
VOICE_CALLBACK_MAX_AGE = timedelta(minutes=5)
VOICE_CALLBACK_MAX_FUTURE_SKEW = timedelta(seconds=30)
SQLITE_LOCK_RETRIES = 8


class VoiceCallbackValidator(object):
    """Provider adapter contract for authenticating and normalizing voice callbacks.

    Implementations must verify the provider's signature/authenticity and return
    signed callback claims with ``authenticated=True``, a timezone-aware
    ``timestamp``, ``provider_call_reference``, ``event_id``, ``contact_id``,
    and ``dtmf_input``. No provider adapter is configured by this module.
    """

    def validate(self, callback_payload, callback_signature):
        raise NotImplementedError


def _retry_sqlite_lock(operation):
    for retry in range(SQLITE_LOCK_RETRIES):
        try:
            return operation()
        except OperationalError as exc:
            if (
                connection.vendor != 'sqlite'
                or 'locked' not in str(exc).lower()
                or retry == SQLITE_LOCK_RETRIES - 1
            ):
                raise
            time.sleep(0.025 * (retry + 1))


def _snapshot_contains_contact(sequence, contact_id):
    try:
        contact_ids = json.loads(sequence or '[]')
    except (TypeError, ValueError):
        return False
    if not isinstance(contact_ids, list):
        return False
    return any(
        not isinstance(saved_id, bool) and str(saved_id) == str(contact_id)
        for saved_id in contact_ids
    )


def _cancel_queued_attempts(event, now):
    SOSNotificationAttempt.objects.filter(
        sos_event=event,
        status=SOSNotificationAttempt.STATUS_QUEUED,
    ).update(
        status=SOSNotificationAttempt.STATUS_CANCELLED,
        completed_at=now,
        updated_at=now,
    )


def _validate_snapshot_membership(event, contact):
    persisted_contact = EmergencyContact.objects.filter(
        pk=contact.pk,
        user=event.user,
    ).first()
    if persisted_contact is None:
        raise ValueError('The contact does not belong to the SOS event owner.')

    state = SOSEscalationState.objects.select_for_update().filter(
        sos_event_id=event.pk,
    ).first()
    if state is None or not _snapshot_contains_contact(
        state.contact_sequence,
        persisted_contact.pk,
    ):
        raise ValueError('The contact is not part of this SOS escalation snapshot.')
    return persisted_contact, state


def _finalize_acknowledgement(event, contact, state, acknowledgement, now):
    if acknowledgement.acknowledged_at is None:
        acknowledgement.acknowledged_at = now
        acknowledgement.save(update_fields=('acknowledged_at',))

    state.status = SOSEscalationState.STATUS_ACKNOWLEDGED
    state.completed_at = state.completed_at or now
    state.acknowledgement_deadline = None
    state.next_action_at = None
    state.lease_owner = ''
    state.lease_token = None
    state.lease_expires_at = None
    state.lease_generation += 1
    state.save(update_fields=(
        'status',
        'completed_at',
        'acknowledgement_deadline',
        'next_action_at',
        'lease_owner',
        'lease_token',
        'lease_expires_at',
        'lease_generation',
        'updated_at',
    ))
    _cancel_queued_attempts(event, now)
    SOSContactAcknowledgement.objects.filter(
        sos_event=event,
        token_hash__isnull=False,
    ).exclude(
        token_hash='',
    ).update(
        token_expires_at=now,
        used_at=now,
    )
    return acknowledgement


def generate_web_acknowledgement_token(
    event,
    contact,
    expires_in=DEFAULT_ACKNOWLEDGEMENT_TOKEN_TTL,
):
    if event is None or event.pk is None or contact is None or contact.pk is None:
        raise ValueError('A persisted SOS event and emergency contact are required.')
    if expires_in <= timedelta(0):
        raise ValueError('Acknowledgement tokens must have a positive lifetime.')

    with transaction.atomic():
        persisted_event = SOSEvent.objects.select_for_update().filter(
            pk=event.pk,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if persisted_event is None:
            raise ValueError('Acknowledgement tokens can only be issued for an active SOS event.')

        persisted_contact, state = _validate_snapshot_membership(persisted_event, contact)
        if state.status != SOSEscalationState.STATUS_RUNNING:
            raise ValueError('Acknowledgement tokens cannot be issued for a stopped escalation.')

        acknowledgement = SOSContactAcknowledgement.objects.select_for_update().filter(
            sos_event=persisted_event,
            emergency_contact=persisted_contact,
        ).first()
        now = timezone.now()
        if acknowledgement is not None and acknowledgement.acknowledged_at is not None:
            raise ValueError('This contact has already acknowledged the SOS event.')

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
        expires_at = now + expires_in
        if acknowledgement is None:
            try:
                with transaction.atomic():
                    SOSContactAcknowledgement.objects.create(
                        sos_event=persisted_event,
                        emergency_contact=persisted_contact,
                        method=SOSContactAcknowledgement.METHOD_WEB_LINK,
                        token_hash=token_hash,
                        token_expires_at=expires_at,
                    )
            except IntegrityError:
                acknowledgement = SOSContactAcknowledgement.objects.select_for_update().get(
                    sos_event=persisted_event,
                    emergency_contact=persisted_contact,
                )
                if acknowledgement.acknowledged_at is not None:
                    raise ValueError('This contact has already acknowledged the SOS event.')
                acknowledgement.method = SOSContactAcknowledgement.METHOD_WEB_LINK
                acknowledgement.token_hash = token_hash
                acknowledgement.token_expires_at = expires_at
                acknowledgement.used_at = None
                acknowledgement.save(update_fields=(
                    'method',
                    'token_hash',
                    'token_expires_at',
                    'used_at',
                ))
        else:
            acknowledgement.method = SOSContactAcknowledgement.METHOD_WEB_LINK
            acknowledgement.token_hash = token_hash
            acknowledgement.token_expires_at = expires_at
            acknowledgement.used_at = None
            acknowledgement.save(update_fields=(
                'method',
                'token_hash',
                'token_expires_at',
                'used_at',
            ))
        return raw_token


def record_web_acknowledgement(token):
    if not isinstance(token, str) or not token:
        raise ValueError('A valid acknowledgement token is required.')
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    event_id = SOSContactAcknowledgement.objects.filter(
        token_hash=token_hash,
        method=SOSContactAcknowledgement.METHOD_WEB_LINK,
    ).values_list('sos_event_id', flat=True).first()
    if event_id is None:
        raise ValueError('The acknowledgement token is invalid, expired, or already used.')

    return _retry_sqlite_lock(
        lambda: _record_web_acknowledgement_transaction(token_hash, event_id)
    )


def _record_web_acknowledgement_transaction(token_hash, event_id):
    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            raise ValueError('Only an active SOS event can be acknowledged.')

        state = SOSEscalationState.objects.select_for_update().filter(
            sos_event_id=event.pk,
        ).first()
        if state is None or state.status not in (
            SOSEscalationState.STATUS_RUNNING,
            SOSEscalationState.STATUS_ACKNOWLEDGED,
        ):
            raise ValueError('This SOS escalation is no longer accepting acknowledgements.')

        acknowledgement = SOSContactAcknowledgement.objects.select_for_update().filter(
            token_hash=token_hash,
            method=SOSContactAcknowledgement.METHOD_WEB_LINK,
        ).first()
        if (
            acknowledgement is None
            or acknowledgement.used_at is not None
            or acknowledgement.acknowledged_at is not None
            or acknowledgement.token_expires_at is None
            or acknowledgement.token_expires_at <= timezone.now()
            or not hmac.compare_digest(acknowledgement.token_hash, token_hash)
        ):
            raise ValueError('The acknowledgement token is invalid, expired, or already used.')

        if acknowledgement.sos_event_id != event.pk:
            raise ValueError('The acknowledgement token is not associated with this SOS event.')

        contact = EmergencyContact.objects.filter(
            pk=acknowledgement.emergency_contact_id,
            user=event.user,
        ).first()
        if contact is None:
            raise ValueError('The acknowledgement contact does not belong to the event owner.')

        persisted_contact, snapshot_state = _validate_snapshot_membership(event, contact)
        if snapshot_state.pk != state.pk:
            raise ValueError('The acknowledgement token is not associated with the escalation snapshot.')
        if state.status not in (
            SOSEscalationState.STATUS_RUNNING,
            SOSEscalationState.STATUS_ACKNOWLEDGED,
        ):
            raise ValueError('This SOS escalation is no longer accepting acknowledgements.')

        now = timezone.now()
        acknowledgement.used_at = now
        acknowledgement.token_expires_at = now
        acknowledgement.save(update_fields=('used_at', 'token_expires_at'))
        return _finalize_acknowledgement(
            event,
            persisted_contact,
            state,
            acknowledgement,
            now,
        )


def record_voice_acknowledgement(
    callback_payload,
    callback_signature,
    callback_validator=None,
):
    """Record a DTMF acknowledgement only through a trusted provider adapter.

    The adapter is responsible for authenticating the signed provider payload.
    This service enforces timestamp freshness, call/event/contact binding, and
    one-time use of the matching stored voice attempt. No production adapter is
    selected or configured here; callers must fail closed when one is absent.
    """
    validate = getattr(callback_validator, 'validate', None)
    if not callable(validate):
        raise ValueError('A trusted provider callback validator is required.')

    verified_callback = validate(callback_payload, callback_signature)
    if not isinstance(verified_callback, dict) or verified_callback.get('authenticated') is not True:
        raise ValueError('The provider callback could not be verified.')

    event_id = verified_callback.get('event_id')
    contact_id = verified_callback.get('contact_id')
    provider_call_reference = verified_callback.get('provider_call_reference')
    callback_timestamp = verified_callback.get('timestamp')
    dtmf_input = verified_callback.get('dtmf_input')
    if dtmf_input != '1':
        raise ValueError('Voice acknowledgement requires the validated DTMF input 1.')
    if (
        event_id is None
        or contact_id is None
        or isinstance(event_id, bool)
        or isinstance(contact_id, bool)
        or not isinstance(provider_call_reference, str)
        or not provider_call_reference.strip()
    ):
        raise ValueError('The verified callback is missing its call, event, or contact binding.')
    if not isinstance(callback_timestamp, datetime) or timezone.is_naive(callback_timestamp):
        raise ValueError('The verified provider callback must include a timezone-aware timestamp.')

    now = timezone.now()
    if (
        callback_timestamp < now - VOICE_CALLBACK_MAX_AGE
        or callback_timestamp > now + VOICE_CALLBACK_MAX_FUTURE_SKEW
    ):
        raise ValueError('The provider callback is stale or has an invalid timestamp.')

    return _retry_sqlite_lock(
        lambda: _record_voice_acknowledgement_transaction(
            event_id,
            contact_id,
            provider_call_reference.strip(),
            callback_timestamp,
        )
    )


def _record_voice_acknowledgement_transaction(
    event_id,
    contact_id,
    provider_call_reference,
    callback_timestamp,
):
    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            raise ValueError('Only an active SOS event can be acknowledged.')
        now = timezone.now()
        if (
            callback_timestamp < now - VOICE_CALLBACK_MAX_AGE
            or callback_timestamp > now + VOICE_CALLBACK_MAX_FUTURE_SKEW
        ):
            raise ValueError('The provider callback is stale or has an invalid timestamp.')
        contact = EmergencyContact.objects.filter(
            pk=contact_id,
            user=event.user,
        ).first()
        if contact is None:
            raise ValueError('The callback contact does not belong to the SOS event owner.')

        persisted_contact, state = _validate_snapshot_membership(event, contact)
        if state.status != SOSEscalationState.STATUS_RUNNING:
            raise ValueError('This SOS escalation is no longer accepting acknowledgements.')

        attempts = list(SOSNotificationAttempt.objects.select_for_update().filter(
            sos_event=event,
            emergency_contact=persisted_contact,
            channel=SOSNotificationAttempt.CHANNEL_VOICE,
            provider_reference=provider_call_reference,
            status__in=(
                SOSNotificationAttempt.STATUS_ACCEPTED,
                SOSNotificationAttempt.STATUS_DELIVERED,
            ),
        )[:2])
        if len(attempts) != 1:
            raise ValueError('The provider call reference is invalid or does not match this contact.')
        attempt = attempts[0]

        acknowledgement = SOSContactAcknowledgement.objects.select_for_update().filter(
            sos_event=event,
            emergency_contact=persisted_contact,
        ).first()
        if acknowledgement is not None and acknowledgement.acknowledged_at is not None:
            raise ValueError('This provider callback has already been used.')

        if acknowledgement is None:
            try:
                with transaction.atomic():
                    acknowledgement = SOSContactAcknowledgement.objects.create(
                        sos_event=event,
                        emergency_contact=persisted_contact,
                        method=SOSContactAcknowledgement.METHOD_VOICE_DTMF,
                    )
            except IntegrityError:
                acknowledgement = SOSContactAcknowledgement.objects.select_for_update().get(
                    sos_event=event,
                    emergency_contact=persisted_contact,
                )

        attempt.status = SOSNotificationAttempt.STATUS_ACKNOWLEDGED
        attempt.completed_at = now
        attempt.save(update_fields=('status', 'completed_at', 'updated_at'))
        acknowledgement.method = SOSContactAcknowledgement.METHOD_VOICE_DTMF
        acknowledgement.acknowledged_at = now
        acknowledgement.used_at = now
        acknowledgement.token_expires_at = now
        acknowledgement.save(update_fields=(
            'method',
            'acknowledged_at',
            'used_at',
            'token_expires_at',
        ))

        return _finalize_acknowledgement(
            event,
            persisted_contact,
            state,
            acknowledgement,
            now,
        )
