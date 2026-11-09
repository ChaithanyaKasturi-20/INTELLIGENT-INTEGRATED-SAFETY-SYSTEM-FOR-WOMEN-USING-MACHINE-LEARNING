import logging
import smtplib
import uuid
from datetime import timedelta
from email.message import EmailMessage

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    EmergencyContact,
    Journey,
    JourneyActivity,
    SafetyCheckIn,
    SafetyCheckInAttempt,
    SafetyTimer,
    SOSEvent,
)


logger = logging.getLogger(__name__)
_IDEMPOTENCY_NAMESPACE = uuid.UUID('c2265e20-22b5-4588-a6f5-147c35ab2214')
_ATTEMPT_LEASE = timedelta(minutes=2)


def _owner(checkin):
    return checkin.journey or checkin.safety_timer


def _owner_kind(owner):
    if isinstance(owner, Journey):
        return 'journey'
    if isinstance(owner, SafetyTimer):
        return 'timer'
    raise ValueError('A check-in must belong to a journey or safety timer.')


def _owner_is_active(owner):
    if isinstance(owner, Journey):
        return owner.status in (
            Journey.STATUS_ACTIVE,
            Journey.STATUS_OVERDUE,
            Journey.STATUS_UNCONFIRMED,
        )
    return owner.status in (
        SafetyTimer.STATUS_ACTIVE,
        SafetyTimer.STATUS_EXPIRED,
        SafetyTimer.STATUS_UNCONFIRMED,
    )


def _next_due_at(owner, now):
    interval = max(1, owner.checkin_interval_minutes)
    if isinstance(owner, Journey):
        return min(now + timedelta(minutes=interval), max(now, owner.expected_arrival))
    return min(now + timedelta(minutes=interval), owner.expires_at)


def schedule_checkin(owner, due_at=None, now=None):
    now = now or timezone.now()
    due_at = due_at or _next_due_at(owner, now)
    checkins = (
        owner.safety_checkins
        if isinstance(owner, Journey)
        else owner.safety_checkins
    )
    previous_sequence = checkins.order_by('-sequence').values_list('sequence', flat=True).first() or 0
    sequence = previous_sequence + 1
    owner_key = '{}:{}'.format(_owner_kind(owner), owner.pk)
    key = uuid.uuid5(_IDEMPOTENCY_NAMESPACE, 'checkin:{}:{}'.format(owner_key, sequence))
    defaults = {
        'journey': owner if isinstance(owner, Journey) else None,
        'safety_timer': owner if isinstance(owner, SafetyTimer) else None,
        'sequence': sequence,
        'scheduled_at': due_at,
        'grace_deadline': due_at + timedelta(minutes=max(1, owner.grace_period_minutes)),
    }
    checkin, _ = SafetyCheckIn.objects.get_or_create(
        idempotency_key=key,
        defaults=defaults,
    )
    return checkin


def _attempt_key(checkin, kind, recipient, attempt_number):
    return uuid.uuid5(
        _IDEMPOTENCY_NAMESPACE,
        'attempt:{}:{}:{}:{}'.format(
            checkin.pk,
            kind,
            (recipient or '').strip().lower(),
            attempt_number,
        ),
    )


def _create_attempt(checkin, kind, recipient_email='', contact=None, attempt_number=1, **values):
    key = _attempt_key(checkin, kind, recipient_email, attempt_number)
    defaults = {
        'contact': contact,
        'recipient_email': recipient_email or '',
        'attempt_number': attempt_number,
        'idempotency_key': key,
    }
    defaults.update(values)
    try:
        attempt, _ = SafetyCheckInAttempt.objects.get_or_create(
            checkin=checkin,
            kind=kind,
            recipient_email=recipient_email or '',
            attempt_number=attempt_number,
            defaults=defaults,
        )
    except IntegrityError:
        attempt = SafetyCheckInAttempt.objects.get(idempotency_key=key)
    return attempt


def _eligible_guardian_contacts(checkin):
    owner = _owner(checkin)
    if not owner.guardian_enabled:
        return []
    contacts = owner.guardian_contacts

    eligible = contacts.filter(
        user=owner.user,
        is_active=True,
        email_consent=True,
        email_verified_at__isnull=False,
    ).exclude(email__isnull=True).exclude(email='').order_by('pk')
    unique_contacts = {}
    for contact in eligible:
        unique_contacts.setdefault(contact.email.strip().lower(), contact)
    return list(unique_contacts.items())


def _set_parent_due_status(checkin, now):
    owner = _owner(checkin)
    if isinstance(owner, Journey):
        changed = Journey.objects.filter(
            pk=owner.pk,
            status=Journey.STATUS_ACTIVE,
        ).update(status=Journey.STATUS_OVERDUE, updated_at=now)
        if changed:
            JourneyActivity.objects.create(
                journey=owner,
                action=JourneyActivity.ACTION_OVERDUE,
                occurred_at=now,
                details='Scheduled check-in is due; no emergency is inferred.',
            )
    elif owner.expires_at <= now:
        SafetyTimer.objects.filter(
            pk=owner.pk,
            status=SafetyTimer.STATUS_ACTIVE,
        ).update(
            status=SafetyTimer.STATUS_EXPIRED,
            completed_at=owner.expires_at,
            updated_at=now,
        )


def _cancel_checkin(checkin, now):
    SafetyCheckIn.objects.filter(
        pk=checkin.pk,
        status__in=(SafetyCheckIn.STATUS_SCHEDULED, SafetyCheckIn.STATUS_DUE),
    ).update(status=SafetyCheckIn.STATUS_CANCELLED, updated_at=now)
    SafetyCheckInAttempt.objects.filter(
        checkin=checkin,
        status=SafetyCheckInAttempt.STATUS_QUEUED,
    ).update(
        status=SafetyCheckInAttempt.STATUS_SKIPPED,
        completed_at=now,
        error_message='The journey or timer ended before this notification was dispatched.',
        updated_at=now,
    )


def _record_due_checkin(checkin_id, now):
    with transaction.atomic():
        checkin = SafetyCheckIn.objects.select_related('journey', 'safety_timer').get(pk=checkin_id)
        owner = _owner(checkin)
        if not _owner_is_active(owner):
            _cancel_checkin(checkin, now)
            return False
        changed = SafetyCheckIn.objects.filter(
            pk=checkin.pk,
            status=SafetyCheckIn.STATUS_SCHEDULED,
            scheduled_at__lte=now,
        ).update(
            status=SafetyCheckIn.STATUS_DUE,
            reminder_count=1,
            next_reminder_at=now + timedelta(minutes=max(1, owner.reminder_retry_minutes)),
            updated_at=now,
        )
        if not changed:
            return False
        _set_parent_due_status(checkin, now)
        _create_attempt(
            checkin,
            SafetyCheckInAttempt.KIND_USER_REMINDER,
            attempt_number=1,
            status=SafetyCheckInAttempt.STATUS_QUEUED,
        )
        return True


def _record_reminder_retry(checkin_id, now):
    with transaction.atomic():
        checkin = SafetyCheckIn.objects.select_related('journey', 'safety_timer').get(pk=checkin_id)
        owner = _owner(checkin)
        retry_limit = max(0, owner.max_reminder_retries)
        if (
            checkin.status != SafetyCheckIn.STATUS_DUE
            or checkin.grace_deadline <= now
            or checkin.next_reminder_at is None
            or checkin.next_reminder_at > now
            or checkin.reminder_count > retry_limit
        ):
            return False
        next_number = checkin.reminder_count + 1
        changed = SafetyCheckIn.objects.filter(
            pk=checkin.pk,
            status=SafetyCheckIn.STATUS_DUE,
            reminder_count=checkin.reminder_count,
            grace_deadline__gt=now,
            next_reminder_at__lte=now,
        ).update(
            reminder_count=F('reminder_count') + 1,
            next_reminder_at=now + timedelta(minutes=max(1, owner.reminder_retry_minutes)),
            updated_at=now,
        )
        if not changed:
            return False
        _create_attempt(
            checkin,
            SafetyCheckInAttempt.KIND_USER_REMINDER,
            attempt_number=next_number,
            status=SafetyCheckInAttempt.STATUS_QUEUED,
        )
        return True


def _record_unconfirmed(checkin_id, now):
    with transaction.atomic():
        checkin = SafetyCheckIn.objects.select_related('journey', 'safety_timer').get(pk=checkin_id)
        owner = _owner(checkin)
        if not _owner_is_active(owner):
            _cancel_checkin(checkin, now)
            return False
        changed = SafetyCheckIn.objects.filter(
            pk=checkin.pk,
            status=SafetyCheckIn.STATUS_DUE,
            grace_deadline__lte=now,
        ).update(
            status=SafetyCheckIn.STATUS_UNCONFIRMED,
            unconfirmed_at=now,
            next_reminder_at=None,
            updated_at=now,
        )
        if not changed:
            return False
        if isinstance(owner, Journey):
            changed_parent = Journey.objects.filter(
                pk=owner.pk,
                status__in=(Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE),
            ).update(status=Journey.STATUS_UNCONFIRMED, updated_at=now)
            if changed_parent:
                JourneyActivity.objects.create(
                    journey=owner,
                    action=JourneyActivity.ACTION_UNCONFIRMED,
                    occurred_at=now,
                    details='Grace period ended without a response; this does not prove an emergency.',
                )
        else:
            SafetyTimer.objects.filter(
                pk=owner.pk,
                status__in=(SafetyTimer.STATUS_ACTIVE, SafetyTimer.STATUS_EXPIRED),
            ).update(status=SafetyTimer.STATUS_UNCONFIRMED, updated_at=now)

        SafetyCheckInAttempt.objects.filter(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
            status=SafetyCheckInAttempt.STATUS_QUEUED,
        ).update(
            status=SafetyCheckInAttempt.STATUS_SKIPPED,
            completed_at=now,
            error_message='The check-in grace period ended before this reminder was presented.',
            updated_at=now,
        )
        SafetyCheckInAttempt.objects.filter(
            checkin=checkin,
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
            status=SafetyCheckInAttempt.STATUS_SENDING,
        ).update(
            status=SafetyCheckInAttempt.STATUS_UNCERTAIN,
            completed_at=now,
            lease_expires_at=None,
            error_message='The check-in grace period ended before the browser presentation could be confirmed.',
            updated_at=now,
        )

        recipients = _eligible_guardian_contacts(checkin)
        if recipients:
            for recipient_email, contact in recipients:
                _create_attempt(
                    checkin,
                    SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
                    recipient_email=recipient_email,
                    contact=contact,
                )
        elif getattr(owner, 'guardian_enabled', False):
            _create_attempt(
                checkin,
                SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
                status=SafetyCheckInAttempt.STATUS_SKIPPED,
                completed_at=now,
                error_message='No selected contact has active email consent, a verified email, and an eligible address.',
            )
        return True


def _backfill_missing_checkins(now, owner_kind=None, user=None):
    created = 0
    if owner_kind in (None, 'journey'):
        journeys = Journey.objects.filter(
            status__in=(Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE),
        ).prefetch_related('safety_checkins')
        if user is not None:
            journeys = journeys.filter(user=user)
        for journey in journeys.iterator():
            has_pending = journey.safety_checkins.filter(
                status__in=(
                    SafetyCheckIn.STATUS_SCHEDULED,
                    SafetyCheckIn.STATUS_DUE,
                    SafetyCheckIn.STATUS_UNCONFIRMED,
                ),
            ).exists()
            if has_pending:
                continue
            start_at = journey.started_at or journey.planned_departure
            due_at = min(journey.expected_arrival, start_at + timedelta(minutes=max(1, journey.checkin_interval_minutes)))
            schedule_checkin(journey, due_at=due_at, now=now)
            created += 1

    if owner_kind in (None, 'timer'):
        timers = SafetyTimer.objects.filter(
            status__in=(SafetyTimer.STATUS_ACTIVE, SafetyTimer.STATUS_EXPIRED),
        ).prefetch_related('safety_checkins')
        if user is not None:
            timers = timers.filter(user=user)
        for timer in timers.iterator():
            has_pending = timer.safety_checkins.filter(
                status__in=(
                    SafetyCheckIn.STATUS_SCHEDULED,
                    SafetyCheckIn.STATUS_DUE,
                    SafetyCheckIn.STATUS_UNCONFIRMED,
                ),
            ).exists()
            if has_pending:
                continue
            due_at = min(
                timer.expires_at,
                timer.started_at + timedelta(minutes=max(1, timer.checkin_interval_minutes)),
            )
            schedule_checkin(timer, due_at=due_at, now=now)
            created += 1
    return created


def _attempt_owner_is_eligible(attempt):
    checkin = attempt.checkin
    if checkin.status != SafetyCheckIn.STATUS_UNCONFIRMED:
        return False
    owner = _owner(checkin)
    if not _owner_is_active(owner):
        return False
    if attempt.contact_id is None:
        return False
    current_contact = EmergencyContact.objects.filter(
        pk=attempt.contact_id,
        user=owner.user,
        is_active=True,
        email_consent=True,
        email_verified_at__isnull=False,
        email__iexact=attempt.recipient_email,
    ).exists()
    return current_contact


def _location_text(owner, now):
    if not owner.share_location_with_contacts:
        return 'Location sharing was not enabled for this journey or timer.'
    if owner.location_status == 'PENDING':
        return 'Location sharing was enabled, but no foreground GPS update has been recorded yet.'
    if owner.location_status != 'AVAILABLE' or owner.last_known_latitude is None:
        return (
            'Location sharing was enabled, but GPS coordinates are currently unavailable. '
            'A missing location is not evidence of an emergency.'
        )

    updated_at = owner.last_location_updated_at
    if updated_at is None:
        return 'The saved coordinates have no location timestamp and are not represented as current.'
    age = max(0, int((now - updated_at).total_seconds()))
    freshness = 'fresh' if age <= 300 else 'stale'
    accuracy = (
        'approximately {:.0f} m accuracy'.format(owner.last_known_accuracy_meters)
        if owner.last_known_accuracy_meters is not None
        else 'accuracy unavailable'
    )
    return (
        'Last location: {:.6f}, {:.6f}; updated {}; {} ({} seconds old). '
        'Map: https://www.google.com/maps?q={:.6f},{:.6f}'.format(
            owner.last_known_latitude,
            owner.last_known_longitude,
            timezone.localtime(updated_at).strftime('%Y-%m-%d %H:%M %Z'),
            accuracy,
            age,
            owner.last_known_latitude,
            owner.last_known_longitude,
        )
    )


def _email_message(attempt, now, sender_email):
    checkin = attempt.checkin
    owner = _owner(checkin)
    if isinstance(owner, Journey):
        subject = 'Journey Guardian: check-in is unconfirmed'
        journey_details = 'Planned journey: {} to {}\n'.format(
            owner.origin_label,
            owner.destination_label,
        )
    else:
        subject = 'Safety Timer: check-in is unconfirmed'
        journey_details = 'A safety timer started at {} has an unconfirmed check-in.\n'.format(
            timezone.localtime(owner.started_at).strftime('%Y-%m-%d %H:%M %Z'),
        )
    message = EmailMessage()
    message['From'] = sender_email
    message['To'] = attempt.recipient_email
    message['Subject'] = subject
    message.set_content(
        'A check-in was not confirmed after the configured grace period. This is not proof '
        'of an emergency, and the application has not contacted emergency services.\n\n'
        '{}Expected check-in: {}\n'
        'Location information: {}\n'
        'Last browser heartbeat: {}\n\n'
        'Please use your own judgment. Do not assume the person is in danger solely because '
        'a response or location update is missing.'.format(
            journey_details,
            timezone.localtime(checkin.scheduled_at).strftime('%Y-%m-%d %H:%M %Z'),
            _location_text(owner, now),
            (
                timezone.localtime(owner.last_device_seen_at).strftime('%Y-%m-%d %H:%M %Z')
                if owner.last_device_seen_at
                else 'not available; the page may be closed or offline'
            ),
        )
    )
    return message


def _record_attempt_result(attempt_id, token, generation, status, now, error='', retry_after=None):
    return SafetyCheckInAttempt.objects.filter(
        pk=attempt_id,
        status=SafetyCheckInAttempt.STATUS_SENDING,
        claim_token=token,
        claim_generation=generation,
    ).update(
        status=status,
        completed_at=now,
        lease_expires_at=None,
        retry_after=retry_after,
        error_message=(error or '')[:500],
        updated_at=now,
    )


def _dispatch_guardian_attempt(attempt_id, now):
    from .views import get_panic_email_config

    with transaction.atomic():
        attempt = SafetyCheckInAttempt.objects.select_related(
            'checkin',
            'checkin__journey',
            'checkin__safety_timer',
            'contact',
        ).get(pk=attempt_id)
        if attempt.status != SafetyCheckInAttempt.STATUS_QUEUED:
            return None
        if not _attempt_owner_is_eligible(attempt):
            SafetyCheckInAttempt.objects.filter(
                pk=attempt.pk,
                status=SafetyCheckInAttempt.STATUS_QUEUED,
            ).update(
                status=SafetyCheckInAttempt.STATUS_SKIPPED,
                completed_at=now,
                error_message='The selected contact is no longer eligible for email notification.',
                updated_at=now,
            )
            return 'skipped'
        token = uuid.uuid4()
        generation = attempt.claim_generation + 1
        claimed = SafetyCheckInAttempt.objects.filter(
            pk=attempt.pk,
            status=SafetyCheckInAttempt.STATUS_QUEUED,
            claim_generation=attempt.claim_generation,
        ).update(
            status=SafetyCheckInAttempt.STATUS_SENDING,
            claim_token=token,
            claim_generation=generation,
            lease_expires_at=now + _ATTEMPT_LEASE,
            attempted_at=now,
            updated_at=now,
        )
        if not claimed:
            return None

    sender_email, sender_password = get_panic_email_config()
    if not sender_email or not sender_password:
        _record_attempt_result(
            attempt.pk,
            token,
            generation,
            SafetyCheckInAttempt.STATUS_FAILED,
            timezone.now(),
            'SMTP is not configured for this environment.',
            timezone.now() + timedelta(minutes=5),
        )
        return 'failed'

    dispatch_started = False
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=20) as connection:
            connection.login(sender_email, sender_password)
            message = _email_message(attempt, timezone.now(), sender_email)
            dispatch_started = True
            refused = connection.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    except smtplib.SMTPAuthenticationError as exc:
        _record_attempt_result(
            attempt.pk,
            token,
            generation,
            SafetyCheckInAttempt.STATUS_FAILED,
            timezone.now(),
            'SMTP rejected the attempt ({})'.format(type(exc).__name__),
        )
        return 'failed'
    except smtplib.SMTPRecipientsRefused as exc:
        _record_attempt_result(
            attempt.pk,
            token,
            generation,
            SafetyCheckInAttempt.STATUS_FAILED,
            timezone.now(),
            'SMTP rejected the recipient ({})'.format(type(exc).__name__),
        )
        return 'failed'
    except smtplib.SMTPConnectError as exc:
        _record_attempt_result(
            attempt.pk,
            token,
            generation,
            SafetyCheckInAttempt.STATUS_FAILED,
            timezone.now(),
            'SMTP connection failed before dispatch ({})'.format(type(exc).__name__),
            timezone.now() + timedelta(minutes=5),
        )
        return 'failed'
    except Exception as exc:
        result_status = (
            SafetyCheckInAttempt.STATUS_UNCERTAIN
            if dispatch_started
            else SafetyCheckInAttempt.STATUS_FAILED
        )
        _record_attempt_result(
            attempt.pk,
            token,
            generation,
            result_status,
            timezone.now(),
            'SMTP attempt ended with {}.'.format(type(exc).__name__),
            None if result_status == SafetyCheckInAttempt.STATUS_UNCERTAIN else timezone.now() + timedelta(minutes=5),
        )
        return 'uncertain' if result_status == SafetyCheckInAttempt.STATUS_UNCERTAIN else 'failed'

    _record_attempt_result(
        attempt.pk,
        token,
        generation,
        SafetyCheckInAttempt.STATUS_ACCEPTED,
        timezone.now(),
    )
    return 'accepted'


def _queue_due_retries(now, owner_kind=None, user=None):
    failed = SafetyCheckInAttempt.objects.filter(
        kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        status=SafetyCheckInAttempt.STATUS_FAILED,
        retry_after__lte=now,
    ).select_related('checkin', 'checkin__journey', 'checkin__safety_timer', 'contact')
    if user is not None:
        failed = failed.filter(
            checkin__journey__user=user
        ) if owner_kind == 'journey' else failed.filter(checkin__safety_timer__user=user)
    queued = 0
    for previous in failed.iterator():
        owner = _owner(previous.checkin)
        if owner_kind and _owner_kind(owner) != owner_kind:
            continue
        if not _attempt_owner_is_eligible(previous):
            SafetyCheckInAttempt.objects.filter(
                pk=previous.pk,
                status=SafetyCheckInAttempt.STATUS_FAILED,
            ).update(
                status=SafetyCheckInAttempt.STATUS_SKIPPED,
                completed_at=now,
                error_message='Retry stopped because the recipient is no longer eligible.',
                updated_at=now,
            )
            continue
        if previous.attempt_number > owner.max_reminder_retries:
            continue
        if SafetyCheckInAttempt.objects.filter(
            checkin=previous.checkin,
            kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
            recipient_email=previous.recipient_email,
            attempt_number=previous.attempt_number + 1,
        ).exists():
            continue
        _create_attempt(
            previous.checkin,
            SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
            recipient_email=previous.recipient_email,
            contact=previous.contact,
            attempt_number=previous.attempt_number + 1,
        )
        queued += 1
    return queued


def _start_automatic_sos(checkin, now):
    owner = _owner(checkin)
    if (
        not owner.auto_sos_on_unconfirmed
        or checkin.unconfirmed_at is None
        or checkin.unconfirmed_at + timedelta(minutes=owner.auto_sos_delay_minutes) > now
    ):
        return None

    from .views import create_sos_live_location_share, get_active_sos_for_user
    from .sos_escalation import start_sos_escalation

    with transaction.atomic():
        current = SafetyCheckIn.objects.select_related('auto_sos_event').get(pk=checkin.pk)
        if current.status != SafetyCheckIn.STATUS_UNCONFIRMED:
            return None
        event = current.auto_sos_event
        if event is None:
            event = get_active_sos_for_user(owner.user)
            if event is None:
                latitude = owner.last_known_latitude if owner.share_location_with_contacts else None
                longitude = owner.last_known_longitude if owner.share_location_with_contacts else None
                location_time = owner.last_location_updated_at if latitude is not None else None
                try:
                    with transaction.atomic():
                        event = SOSEvent.objects.create(
                            user=owner.user,
                            latitude=latitude,
                            longitude=longitude,
                            location_updated_at=location_time,
                            status=SOSEvent.STATUS_ACTIVE,
                            activated_at=now,
                        )
                except IntegrityError:
                    event = get_active_sos_for_user(owner.user)
                if event is not None and latitude is not None:
                    create_sos_live_location_share(event)
            if event is None:
                return None
            SafetyCheckIn.objects.filter(
                pk=current.pk,
                status=SafetyCheckIn.STATUS_UNCONFIRMED,
                auto_sos_event__isnull=True,
            ).update(auto_sos_event=event, updated_at=now)
    try:
        start_sos_escalation(event)
    except Exception as exc:
        logger.error(
            'Automatic Safety Check-In SOS escalation failed for event %s (error type: %s).',
            event.pk,
            type(exc).__name__,
        )
    return event


def process_safety_checkins(
    now=None,
    owner_kind=None,
    user=None,
    dispatch_emails=True,
    allow_auto_sos=True,
):
    now = now or timezone.now()
    if owner_kind not in (None, 'journey', 'timer'):
        raise ValueError('owner_kind must be journey, timer, or None.')

    stale_user_reminders = SafetyCheckInAttempt.objects.filter(
        kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
        status=SafetyCheckInAttempt.STATUS_SENDING,
        lease_expires_at__lte=now,
    )
    if user is not None:
        stale_user_reminders = (
            stale_user_reminders.filter(checkin__journey__user=user)
            if owner_kind == 'journey'
            else stale_user_reminders.filter(checkin__safety_timer__user=user)
        )
    stale_user_reminders.update(
        status=SafetyCheckInAttempt.STATUS_QUEUED,
        claim_token=None,
        lease_expires_at=None,
        error_message='The browser did not confirm presentation; the same idempotent reminder can be offered again.',
        updated_at=now,
    )

    stale_attempts = SafetyCheckInAttempt.objects.filter(
        kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        status=SafetyCheckInAttempt.STATUS_SENDING,
        lease_expires_at__lte=now,
    )
    if user is not None:
        stale_attempts = (
            stale_attempts.filter(checkin__journey__user=user)
            if owner_kind == 'journey'
            else stale_attempts.filter(checkin__safety_timer__user=user)
        )
    uncertain = stale_attempts.update(
        status=SafetyCheckInAttempt.STATUS_UNCERTAIN,
        completed_at=now,
        lease_expires_at=None,
        error_message='The previous notification attempt lease expired; delivery is unknown and was not resent.',
        updated_at=now,
    )

    backfilled = _backfill_missing_checkins(now, owner_kind, user)
    scheduled = SafetyCheckIn.objects.filter(
        status=SafetyCheckIn.STATUS_SCHEDULED,
        scheduled_at__lte=now,
    ).select_related('journey', 'safety_timer')
    if owner_kind == 'journey':
        scheduled = scheduled.filter(journey__isnull=False)
    elif owner_kind == 'timer':
        scheduled = scheduled.filter(safety_timer__isnull=False)
    if user is not None:
        scheduled = scheduled.filter(journey__user=user) if owner_kind == 'journey' else scheduled.filter(safety_timer__user=user)
    due_count = sum(1 for checkin_id in scheduled.values_list('pk', flat=True).iterator() if _record_due_checkin(checkin_id, now))

    due_checkins = SafetyCheckIn.objects.filter(
        status=SafetyCheckIn.STATUS_DUE,
    ).select_related('journey', 'safety_timer')
    if owner_kind == 'journey':
        due_checkins = due_checkins.filter(journey__isnull=False)
    elif owner_kind == 'timer':
        due_checkins = due_checkins.filter(safety_timer__isnull=False)
    if user is not None:
        due_checkins = due_checkins.filter(journey__user=user) if owner_kind == 'journey' else due_checkins.filter(safety_timer__user=user)

    retry_count = 0
    unconfirmed_count = 0
    for checkin_id in due_checkins.values_list('pk', flat=True).iterator():
        if _record_unconfirmed(checkin_id, now):
            unconfirmed_count += 1
        elif _record_reminder_retry(checkin_id, now):
            retry_count += 1

    if owner_kind in (None, 'timer'):
        expired_timers = SafetyTimer.objects.filter(
            status=SafetyTimer.STATUS_ACTIVE,
            expires_at__lte=now,
        )
        if user is not None:
            expired_timers = expired_timers.filter(user=user)
        expired_timers.update(
            status=SafetyTimer.STATUS_EXPIRED,
            completed_at=F('expires_at'),
            updated_at=now,
        )

    retry_queued = _queue_due_retries(now, owner_kind, user)
    queued_attempts = SafetyCheckInAttempt.objects.filter(
        kind=SafetyCheckInAttempt.KIND_GUARDIAN_EMAIL,
        status=SafetyCheckInAttempt.STATUS_QUEUED,
    ).select_related('checkin', 'checkin__journey', 'checkin__safety_timer')
    if owner_kind == 'journey':
        queued_attempts = queued_attempts.filter(checkin__journey__isnull=False)
    elif owner_kind == 'timer':
        queued_attempts = queued_attempts.filter(checkin__safety_timer__isnull=False)
    if user is not None:
        queued_attempts = queued_attempts.filter(checkin__journey__user=user) if owner_kind == 'journey' else queued_attempts.filter(checkin__safety_timer__user=user)
    outcomes = {'accepted': 0, 'failed': 0, 'uncertain': 0, 'skipped': 0}
    if dispatch_emails:
        for attempt_id in queued_attempts.values_list('pk', flat=True).iterator():
            outcome = _dispatch_guardian_attempt(attempt_id, now)
            if outcome in outcomes:
                outcomes[outcome] += 1

    candidates = SafetyCheckIn.objects.filter(
        status=SafetyCheckIn.STATUS_UNCONFIRMED,
        unconfirmed_at__lte=now,
    ).select_related('journey', 'safety_timer', 'auto_sos_event')
    if owner_kind == 'journey':
        candidates = candidates.filter(journey__isnull=False)
    elif owner_kind == 'timer':
        candidates = candidates.filter(safety_timer__isnull=False)
    if user is not None:
        candidates = candidates.filter(journey__user=user) if owner_kind == 'journey' else candidates.filter(safety_timer__user=user)
    automatic_sos_count = 0
    if allow_auto_sos:
        for checkin in candidates.iterator():
            event = _start_automatic_sos(checkin, now)
            if event is not None:
                automatic_sos_count += 1

    return {
        'backfilled_checkins': backfilled,
        'due_checkins': due_count,
        'reminder_retries': retry_count,
        'unconfirmed_checkins': unconfirmed_count,
        'retry_attempts_queued': retry_queued,
        'guardian_emails_accepted': outcomes['accepted'],
        'guardian_emails_failed': outcomes['failed'],
        'guardian_emails_uncertain': outcomes['uncertain'] + uncertain,
        'guardian_emails_skipped': outcomes['skipped'],
        'automatic_sos_events_reused_or_started': automatic_sos_count,
    }


def cancel_owner_checkins(owner, now=None):
    now = now or timezone.now()
    checkins = owner.safety_checkins.filter(
        status__in=(SafetyCheckIn.STATUS_SCHEDULED, SafetyCheckIn.STATUS_DUE),
    )
    checkin_ids = list(checkins.values_list('pk', flat=True))
    changed = checkins.update(status=SafetyCheckIn.STATUS_CANCELLED, updated_at=now)
    SafetyCheckInAttempt.objects.filter(
        checkin_id__in=checkin_ids,
        status=SafetyCheckInAttempt.STATUS_QUEUED,
    ).update(
        status=SafetyCheckInAttempt.STATUS_SKIPPED,
        completed_at=now,
        error_message='The journey or timer was confirmed, cancelled, or ended.',
        updated_at=now,
    )
    return changed


def confirm_owner_checkin(owner, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        checkin = owner.safety_checkins.filter(
            status__in=(
                SafetyCheckIn.STATUS_SCHEDULED,
                SafetyCheckIn.STATUS_DUE,
                SafetyCheckIn.STATUS_UNCONFIRMED,
            ),
        ).order_by('-sequence').first()
        if checkin is None:
            return None
        changed = SafetyCheckIn.objects.filter(
            pk=checkin.pk,
            status__in=(
                SafetyCheckIn.STATUS_SCHEDULED,
                SafetyCheckIn.STATUS_DUE,
                SafetyCheckIn.STATUS_UNCONFIRMED,
            ),
        ).update(
            status=SafetyCheckIn.STATUS_CONFIRMED,
            confirmed_at=now,
            updated_at=now,
        )
        if not changed:
            return None
        cancel_owner_checkins(owner, now)
        return checkin


def update_owner_location(owner, data, now=None):
    now = now or timezone.now()
    if not owner.share_location_with_contacts:
        if data.get('location_status') != 'NOT_SHARED':
            return False
        return owner.__class__.objects.filter(
            pk=owner.pk,
            share_location_with_contacts=False,
            status__in=(
                (Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE, Journey.STATUS_UNCONFIRMED)
                if isinstance(owner, Journey)
                else (SafetyTimer.STATUS_ACTIVE, SafetyTimer.STATUS_EXPIRED, SafetyTimer.STATUS_UNCONFIRMED)
            ),
        ).update(last_device_seen_at=now, location_status='NOT_SHARED', updated_at=now) == 1

    status = data.get('location_status')
    if status not in ('AVAILABLE', 'UNAVAILABLE'):
        return False
    updates = {
        'location_status': status,
        'last_device_seen_at': now,
        'updated_at': now,
    }
    if status == 'AVAILABLE':
        try:
            latitude = float(data.get('latitude'))
            longitude = float(data.get('longitude'))
            accuracy = float(data.get('accuracy_meters'))
        except (TypeError, ValueError):
            return False
        if (
            not (-90 <= latitude <= 90)
            or not (-180 <= longitude <= 180)
            or not (0 <= accuracy <= 10000)
        ):
            return False
        updates.update({
            'last_known_latitude': latitude,
            'last_known_longitude': longitude,
            'last_known_accuracy_meters': accuracy,
            'last_location_updated_at': now,
        })
    return owner.__class__.objects.filter(
        pk=owner.pk,
        share_location_with_contacts=True,
        status__in=(
            (Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE, Journey.STATUS_UNCONFIRMED)
            if isinstance(owner, Journey)
            else (SafetyTimer.STATUS_ACTIVE, SafetyTimer.STATUS_EXPIRED, SafetyTimer.STATUS_UNCONFIRMED)
        ),
    ).update(**updates) == 1
