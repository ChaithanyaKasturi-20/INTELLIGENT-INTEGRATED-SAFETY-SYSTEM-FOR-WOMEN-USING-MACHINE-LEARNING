"""SOS escalation with database-backed, fenced worker leases.

Conditional updates are used on SQLite and PostgreSQL, but SQLite does not
provide PostgreSQL row-locking semantics. Multi-worker concurrency guarantees
must be validated against PostgreSQL before production use.
"""

import json
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    EmergencyContact,
    SOSEscalationState,
    SOSEvent,
    SOSContactAcknowledgement,
    SOSNotificationAttempt,
)
from .sos_notifications import (
    MAX_ADDITIONAL_RETRIES_PER_CHANNEL,
    RETRYABLE_OUTCOME_STATUSES,
    dispatch_contact_notifications,
    get_eligible_contacts,
)


CHANNELS = (
    SOSNotificationAttempt.CHANNEL_EMAIL,
    SOSNotificationAttempt.CHANNEL_SMS,
    SOSNotificationAttempt.CHANNEL_VOICE,
)
PENDING_ATTEMPT_STATUSES = frozenset((
    SOSNotificationAttempt.STATUS_SENDING,
    SOSNotificationAttempt.STATUS_UNCERTAIN,
))
RECHECK_DELAY_SECONDS = 30
EVENT_LEASE_DURATION = timedelta(minutes=5)


class EventLease(object):
    def __init__(self, owner, token, generation):
        self.owner = owner
        self.token = token
        self.generation = generation


def acquire_event_lease(event_id, owner=None, lease_duration=EVENT_LEASE_DURATION):
    if lease_duration <= timedelta(0):
        raise ValueError('The event lease duration must be positive.')
    owner = owner or 'worker-{}'.format(uuid.uuid4())
    if len(owner) > 100:
        raise ValueError('The event lease owner must not exceed 100 characters.')
    now = timezone.now()
    token = uuid.uuid4()
    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            return None
        updated = SOSEscalationState.objects.filter(
            sos_event_id=event_id,
            status=SOSEscalationState.STATUS_RUNNING,
        ).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now),
        ).update(
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=now + lease_duration,
            lease_generation=F('lease_generation') + 1,
            updated_at=now,
        )
        if updated != 1:
            return None
        state = SOSEscalationState.objects.get(sos_event_id=event_id)
        return EventLease(owner, token, state.lease_generation)


def renew_event_lease(event_id, lease, lease_duration=EVENT_LEASE_DURATION):
    if lease_duration <= timedelta(0):
        raise ValueError('The event lease duration must be positive.')
    now = timezone.now()
    return SOSEscalationState.objects.filter(
        sos_event_id=event_id,
        sos_event__status=SOSEvent.STATUS_ACTIVE,
        status=SOSEscalationState.STATUS_RUNNING,
        lease_owner=lease.owner,
        lease_token=lease.token,
        lease_generation=lease.generation,
        lease_expires_at__gt=now,
    ).update(
        lease_expires_at=now + lease_duration,
        updated_at=now,
    ) == 1


def release_event_lease(event_id, lease):
    now = timezone.now()
    return SOSEscalationState.objects.filter(
        sos_event_id=event_id,
        lease_owner=lease.owner,
        lease_token=lease.token,
        lease_generation=lease.generation,
        lease_expires_at__gt=now,
    ).update(
        lease_owner='',
        lease_token=None,
        lease_expires_at=None,
        updated_at=now,
    ) == 1


def _clear_event_lease(state):
    state.lease_owner = ''
    state.lease_token = None
    state.lease_expires_at = None
    state.lease_generation += 1


def _effective_retry_limit(state):
    return min(
        state.max_retries_per_channel,
        MAX_ADDITIONAL_RETRIES_PER_CHANNEL,
    )


def _eligible_contact_snapshot(event):
    eligible_by_id = {}
    for channel in CHANNELS:
        for contact in get_eligible_contacts(event, channel):
            eligible_by_id[contact.pk] = contact
    contacts = sorted(
        eligible_by_id.values(),
        key=lambda contact: (not contact.is_primary, contact.pk),
    )
    return [contact.pk for contact in contacts]


def _parse_contact_sequence(sequence):
    try:
        contact_ids = json.loads(sequence or '[]')
    except (TypeError, ValueError):
        return []
    if not isinstance(contact_ids, list):
        return []

    parsed_ids = []
    for contact_id in contact_ids:
        if isinstance(contact_id, bool):
            return []
        try:
            parsed_id = int(contact_id)
        except (TypeError, ValueError):
            return []
        if parsed_id <= 0:
            return []
        parsed_ids.append(parsed_id)
    return parsed_ids


def _cancel_queued_attempts(event, now=None):
    now = now or timezone.now()
    return SOSNotificationAttempt.objects.filter(
        sos_event=event,
        status=SOSNotificationAttempt.STATUS_QUEUED,
    ).update(
        status=SOSNotificationAttempt.STATUS_CANCELLED,
        completed_at=now,
        updated_at=now,
    )


def _invalidate_acknowledgement_tokens(event, now=None):
    now = now or timezone.now()
    return SOSContactAcknowledgement.objects.filter(
        sos_event=event,
        token_hash__isnull=False,
    ).exclude(
        token_hash='',
    ).update(
        token_expires_at=now,
        used_at=now,
    )


def _stop_locked(event, state, status, now):
    if state is not None:
        state.status = status
        state.completed_at = now
        state.acknowledgement_deadline = None
        state.next_action_at = None
        _clear_event_lease(state)
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
    _invalidate_acknowledgement_tokens(event, now)
    return state


def _terminal_status_for_event(event):
    if event.status == SOSEvent.STATUS_CANCELLED:
        return SOSEscalationState.STATUS_CANCELLED
    if event.status == SOSEvent.STATUS_RESOLVED:
        return SOSEscalationState.STATUS_RESOLVED
    return None


def start_sos_escalation(event):
    if event is None or event.pk is None:
        raise ValueError('A persisted SOS event is required.')

    with transaction.atomic():
        persisted_event = SOSEvent.objects.select_for_update().filter(pk=event.pk).first()
        if persisted_event is None or persisted_event.status != SOSEvent.STATUS_ACTIVE:
            raise ValueError('Escalation can only start for an active SOS event.')

        state = SOSEscalationState.objects.select_for_update().filter(
            sos_event_id=persisted_event.pk,
        ).first()
        if state is None:
            contact_ids = _eligible_contact_snapshot(persisted_event)
            now = timezone.now()
            try:
                with transaction.atomic():
                    state = SOSEscalationState.objects.create(
                        sos_event=persisted_event,
                        status=(
                            SOSEscalationState.STATUS_RUNNING
                            if contact_ids
                            else SOSEscalationState.STATUS_EXHAUSTED
                        ),
                        contact_sequence=json.dumps(contact_ids),
                        current_contact_id=contact_ids[0] if contact_ids else None,
                        current_contact_order=1 if contact_ids else None,
                        acknowledgement_deadline=None,
                        next_action_at=now if contact_ids else None,
                        started_at=now,
                        completed_at=None if contact_ids else now,
                    )
            except IntegrityError:
                state = SOSEscalationState.objects.select_for_update().filter(
                    sos_event_id=persisted_event.pk,
                ).first()
                if state is None:
                    raise

    if state.status == SOSEscalationState.STATUS_RUNNING:
        return process_escalation_for_event(event)
    return state


def process_due_escalations(on_error=None):
    processed = 0
    active_event_ids_without_state = list(SOSEvent.objects.filter(
        status=SOSEvent.STATUS_ACTIVE,
    ).exclude(
        pk__in=SOSEscalationState.objects.values_list('sos_event_id', flat=True),
    ).order_by('activated_at', 'pk').values_list('pk', flat=True))

    for event_id in active_event_ids_without_state:
        event = SOSEvent.objects.filter(pk=event_id).first()
        if event is None:
            continue
        try:
            start_sos_escalation(event)
            processed += 1
        except Exception as exc:
            if on_error is None:
                raise
            on_error(event_id, exc)

    now = timezone.now()
    event_ids = list(
        SOSEscalationState.objects.filter(
            status=SOSEscalationState.STATUS_RUNNING,
        ).filter(
            Q(next_action_at__isnull=True)
            | Q(next_action_at__lte=now)
            | Q(sos_event__status__in=(
                SOSEvent.STATUS_CANCELLED,
                SOSEvent.STATUS_RESOLVED,
            )),
        ).order_by('next_action_at', 'pk').values_list('sos_event_id', flat=True)
    )
    for event_id in event_ids:
        event = SOSEvent.objects.filter(pk=event_id).first()
        if event is not None:
            try:
                if event.status in (
                    SOSEvent.STATUS_CANCELLED,
                    SOSEvent.STATUS_RESOLVED,
                ):
                    process_escalation_for_event(event)
                    processed += 1
                    continue
                lease = acquire_event_lease(event_id)
                if lease is None:
                    continue
                process_escalation_for_event(event, lease=lease)
                processed += 1
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(event_id, exc)
    return processed


def _mark_acknowledged(event, state, now):
    state.status = SOSEscalationState.STATUS_ACKNOWLEDGED
    state.completed_at = now
    state.acknowledgement_deadline = None
    state.next_action_at = None
    _clear_event_lease(state)
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
    _invalidate_acknowledgement_tokens(event, now)


def _advance_contact(state, contact_ids, now):
    current_order = state.current_contact_order or 0
    next_index = current_order
    if next_index >= len(contact_ids):
        state.status = SOSEscalationState.STATUS_EXHAUSTED
        state.current_contact = None
        state.current_contact_order = None
        state.completed_at = now
        state.acknowledgement_deadline = None
        state.next_action_at = None
        _clear_event_lease(state)
    else:
        state.current_contact_id = contact_ids[next_index]
        state.current_contact_order = next_index + 1
        state.acknowledgement_deadline = None
        state.next_action_at = now
    state.save(update_fields=(
        'status',
        'current_contact',
        'current_contact_order',
        'completed_at',
        'acknowledgement_deadline',
        'next_action_at',
        'lease_owner',
        'lease_token',
        'lease_expires_at',
        'lease_generation',
        'updated_at',
    ))


def _schedule_recheck(state, now):
    state.next_action_at = now + timedelta(seconds=RECHECK_DELAY_SECONDS)
    state.save(update_fields=('next_action_at', 'updated_at'))


def _dispatch_guard(event_id, contact_id, lease):
    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            return False
        state = SOSEscalationState.objects.select_for_update().filter(
            sos_event_id=event_id,
            status=SOSEscalationState.STATUS_RUNNING,
            current_contact_id=contact_id,
            lease_owner=lease.owner,
            lease_token=lease.token,
            lease_generation=lease.generation,
            lease_expires_at__gt=timezone.now(),
        ).first()
        if state is None:
            return False
        if SOSContactAcknowledgement.objects.filter(
            sos_event=event,
            acknowledged_at__isnull=False,
        ).exists():
            return False
        contact = EmergencyContact.objects.filter(
            pk=contact_id,
            user=event.user,
            is_active=True,
        ).first()
        return contact is not None


def _start_acknowledgement_window(
    event_id,
    contact_id,
    timeout_seconds,
    attempts,
    lease,
):
    """Start a fixed window after preparation, before provider latency begins.

    A slow synchronous provider consumes this window; its return never resets
    or extends the deadline. A retry batch receives its own fixed window.
    """
    if not attempts or any(
        attempt.status != SOSNotificationAttempt.STATUS_QUEUED
        for attempt in attempts
    ):
        return False

    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            return False
        state = SOSEscalationState.objects.select_for_update().filter(
            sos_event_id=event_id,
            status=SOSEscalationState.STATUS_RUNNING,
            current_contact_id=contact_id,
            lease_owner=lease.owner,
            lease_token=lease.token,
            lease_generation=lease.generation,
            lease_expires_at__gt=timezone.now(),
        ).first()
        if state is None or SOSContactAcknowledgement.objects.filter(
            sos_event=event,
            acknowledged_at__isnull=False,
        ).exists():
            return False

        now = timezone.now()
        deadline = now + timedelta(seconds=timeout_seconds)
        state.acknowledgement_deadline = deadline
        state.next_action_at = deadline
        state.save(update_fields=(
            'acknowledgement_deadline',
            'next_action_at',
            'updated_at',
        ))
        return True


def _dispatch_plan(
    event_id,
    contact_id,
    max_retries,
    initial_channels,
    retry_channels,
    timeout_seconds,
    start_deadline=True,
    lease=None,
):
    if lease is None or not renew_event_lease(event_id, lease):
        return
    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            return
        state = SOSEscalationState.objects.select_for_update().filter(
            sos_event_id=event_id,
            status=SOSEscalationState.STATUS_RUNNING,
            current_contact_id=contact_id,
            lease_owner=lease.owner,
            lease_token=lease.token,
            lease_generation=lease.generation,
            lease_expires_at__gt=timezone.now(),
        ).first()
        if state is None:
            return
        contact = EmergencyContact.objects.filter(
            pk=contact_id,
            user=event.user,
            is_active=True,
        ).first()
        if contact is None:
            return

    channels = tuple(initial_channels) + tuple(
        channel for channel in retry_channels if channel not in initial_channels
    )

    def prepare_dispatch(attempts):
        if not _dispatch_guard(event_id, contact_id, lease):
            return False
        if start_deadline:
            return _start_acknowledgement_window(
                event_id,
                contact_id,
                timeout_seconds,
                attempts,
                lease,
            )
        return True

    dispatch_contact_notifications(
        event,
        contact,
        channels=channels,
        retry_channels=retry_channels,
        max_retries_per_channel=max_retries,
        on_attempts_prepared=prepare_dispatch,
        dispatch_guard=lambda _event, _contact: _dispatch_guard(
            event_id,
            contact_id,
            lease,
        ),
    )


def _current_contact_plan(event, state, contact):
    eligible_channels = [
        channel
        for channel in CHANNELS
        if contact in get_eligible_contacts(event, channel)
    ]
    attempts = list(
        SOSNotificationAttempt.objects.filter(
            sos_event=event,
            emergency_contact=contact,
        ).order_by('channel', '-attempt_number')
    )
    latest_by_channel = {}
    for attempt in attempts:
        if attempt.channel not in latest_by_channel:
            latest_by_channel[attempt.channel] = attempt

    has_unresolved_attempt = any(
        attempt.status in PENDING_ATTEMPT_STATUSES
        for attempt in latest_by_channel.values()
    )
    queued_channels = [
        channel
        for channel in eligible_channels
        if channel in latest_by_channel
        and latest_by_channel[channel].status == SOSNotificationAttempt.STATUS_QUEUED
    ]
    if has_unresolved_attempt:
        return queued_channels, [], True

    active_attempt_channels = {
        channel
        for channel, attempt in latest_by_channel.items()
        if attempt.status in PENDING_ATTEMPT_STATUSES
    }
    if active_attempt_channels:
        return [], [], True

    now = timezone.now()
    SOSNotificationAttempt.objects.filter(
        sos_event=event,
        emergency_contact=contact,
        status=SOSNotificationAttempt.STATUS_QUEUED,
    ).exclude(
        channel__in=eligible_channels,
    ).update(
        status=SOSNotificationAttempt.STATUS_CANCELLED,
        completed_at=now,
        updated_at=now,
    )

    if queued_channels:
        return queued_channels, [], False

    retry_channels = []
    missing_channels = []
    retry_limit = _effective_retry_limit(state)
    for channel in eligible_channels:
        latest = latest_by_channel.get(channel)
        if latest is None:
            missing_channels.append(channel)
        elif (
            latest.status in RETRYABLE_OUTCOME_STATUSES
            and latest.attempt_number <= retry_limit
        ):
            retry_channels.append(channel)

    return missing_channels, retry_channels, False


def process_escalation_for_event(event, lease=None):
    if event is None or event.pk is None:
        raise ValueError('A persisted SOS event is required.')

    current_status = SOSEvent.objects.filter(pk=event.pk).values_list(
        'status',
        flat=True,
    ).first()
    if current_status is None:
        return None
    if current_status in (
        SOSEvent.STATUS_CANCELLED,
        SOSEvent.STATUS_RESOLVED,
    ):
        return stop_sos_escalation(event, current_status)

    if lease is None:
        lease = acquire_event_lease(event.pk)
    if lease is None:
        return SOSEscalationState.objects.filter(sos_event_id=event.pk).first()
    try:
        return _process_escalation_for_event(event, lease)
    finally:
        release_event_lease(event.pk, lease)


def _process_escalation_for_event(event, lease):
    while True:
        dispatch_plan = None
        with transaction.atomic():
            persisted_event = SOSEvent.objects.select_for_update().filter(
                pk=event.pk,
            ).first()
            if persisted_event is None:
                return None

            state = SOSEscalationState.objects.select_for_update().filter(
                sos_event_id=persisted_event.pk,
                status=SOSEscalationState.STATUS_RUNNING,
                lease_owner=lease.owner,
                lease_token=lease.token,
                lease_generation=lease.generation,
                lease_expires_at__gt=timezone.now(),
            ).first()
            if state is None:
                return SOSEscalationState.objects.filter(
                    sos_event_id=persisted_event.pk,
                ).first()

            terminal_status = _terminal_status_for_event(persisted_event)
            if terminal_status is not None:
                return _stop_locked(persisted_event, state, terminal_status, timezone.now())
            if persisted_event.status != SOSEvent.STATUS_ACTIVE:
                return state
            if state.status != SOSEscalationState.STATUS_RUNNING:
                return state

            now = timezone.now()
            if SOSContactAcknowledgement.objects.filter(
                sos_event=persisted_event,
                acknowledged_at__isnull=False,
            ).exists():
                _mark_acknowledged(persisted_event, state, now)
                return state

            contact_ids = _parse_contact_sequence(state.contact_sequence)
            if not contact_ids:
                state.status = SOSEscalationState.STATUS_EXHAUSTED
                state.completed_at = now
                state.current_contact = None
                state.current_contact_order = None
                state.acknowledgement_deadline = None
                state.next_action_at = None
                _clear_event_lease(state)
                state.save(update_fields=(
                    'status',
                    'completed_at',
                    'current_contact',
                    'current_contact_order',
                    'acknowledgement_deadline',
                    'next_action_at',
                    'lease_owner',
                    'lease_token',
                    'lease_expires_at',
                    'lease_generation',
                    'updated_at',
                ))
                return state

            current_order = state.current_contact_order or 0
            if (
                current_order < 1
                or current_order > len(contact_ids)
                or state.current_contact_id != contact_ids[current_order - 1]
            ):
                _advance_contact(state, contact_ids, now)
                if state.status != SOSEscalationState.STATUS_RUNNING:
                    return state
                continue

            contact = EmergencyContact.objects.filter(
                pk=contact_ids[current_order - 1],
                user=persisted_event.user,
            ).first()
            if contact is None:
                _advance_contact(state, contact_ids, now)
                if state.status != SOSEscalationState.STATUS_RUNNING:
                    return state
                continue

            eligible_channels = []
            if contact.is_active:
                eligible_channels = [
                    channel
                    for channel in CHANNELS
                    if contact in get_eligible_contacts(persisted_event, channel)
                ]
            if not eligible_channels:
                unresolved_attempt = SOSNotificationAttempt.objects.filter(
                    sos_event=persisted_event,
                    emergency_contact=contact,
                    status__in=PENDING_ATTEMPT_STATUSES,
                ).exists()
                if unresolved_attempt:
                    _schedule_recheck(state, now)
                    return state
                SOSNotificationAttempt.objects.filter(
                    sos_event=persisted_event,
                    emergency_contact=contact,
                    status=SOSNotificationAttempt.STATUS_QUEUED,
                ).update(
                    status=SOSNotificationAttempt.STATUS_CANCELLED,
                    completed_at=now,
                    updated_at=now,
                )
                _advance_contact(state, contact_ids, now)
                if state.status != SOSEscalationState.STATUS_RUNNING:
                    return state
                continue

            deadline = state.acknowledgement_deadline
            if deadline is not None and deadline > now:
                return state

            if deadline is None:
                initial_channels = eligible_channels
                retry_channels = []
                unresolved = False
            else:
                initial_channels, retry_channels, unresolved = _current_contact_plan(
                    persisted_event,
                    state,
                    contact,
                )
                if unresolved:
                    _schedule_recheck(state, now)
                    if not initial_channels:
                        return state

            if not initial_channels and not retry_channels:
                _advance_contact(state, contact_ids, now)
                if state.status != SOSEscalationState.STATUS_RUNNING:
                    return state
                continue

            dispatch_plan = (
                persisted_event,
                contact,
                _effective_retry_limit(state),
                initial_channels,
                retry_channels,
                state.acknowledgement_timeout_seconds,
                True,
            )

        (
            persisted_event,
            contact,
            max_retries,
            initial_channels,
            retry_channels,
            timeout_seconds,
            start_deadline,
        ) = dispatch_plan
        if not renew_event_lease(persisted_event.pk, lease):
            return SOSEscalationState.objects.filter(
                sos_event_id=persisted_event.pk,
            ).first()
        _dispatch_plan(
            persisted_event.pk,
            contact.pk,
            max_retries,
            initial_channels,
            retry_channels,
            timeout_seconds,
            start_deadline,
            lease,
        )
        return SOSEscalationState.objects.get(sos_event_id=persisted_event.pk)


def stop_sos_escalation(event, reason):
    if event is None or event.pk is None:
        raise ValueError('A persisted SOS event is required.')

    normalized_reason = (reason or '').strip().upper()
    with transaction.atomic():
        persisted_event = SOSEvent.objects.select_for_update().filter(pk=event.pk).first()
        if persisted_event is None:
            return None

        if (
            normalized_reason == SOSEvent.STATUS_RESOLVED
            or persisted_event.status == SOSEvent.STATUS_RESOLVED
        ):
            status = SOSEscalationState.STATUS_RESOLVED
        else:
            status = SOSEscalationState.STATUS_CANCELLED
        state = SOSEscalationState.objects.select_for_update().filter(
            sos_event_id=persisted_event.pk,
        ).first()
        return _stop_locked(persisted_event, state, status, timezone.now())
