import json
import uuid
from datetime import datetime, timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
from .safety_checkins import (
    cancel_owner_checkins,
    confirm_owner_checkin,
    process_safety_checkins,
    schedule_checkin,
    update_owner_location,
)


def _session_owner(request):
    if not request.session.get('is_logged_in'):
        return None
    return request.session.get('username')


def _local_datetime(request, value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M')
        offset_minutes = int(request.POST.get('timezone_offset_minutes', ''))
    except (TypeError, ValueError):
        return None
    if offset_minutes < -14 * 60 or offset_minutes > 14 * 60:
        return None
    return timezone.make_aware(parsed, timezone.get_fixed_timezone(-offset_minutes))


def journeys(request):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    process_safety_checkins(
        owner_kind='journey',
        user=owner,
        dispatch_emails=False,
        allow_auto_sos=False,
    )
    records = Journey.objects.filter(user=owner).prefetch_related(
        'activities',
        'guardian_contacts',
        'safety_checkins__attempts',
    )
    for journey in records:
        journey.device_heartbeat_stale = (
            journey.last_device_seen_at is None
            or journey.last_device_seen_at < timezone.now() - timedelta(minutes=2)
        )
        journey.current_checkin = journey.safety_checkins.filter(
            status__in=(
                SafetyCheckIn.STATUS_SCHEDULED,
                SafetyCheckIn.STATUS_DUE,
                SafetyCheckIn.STATUS_UNCONFIRMED,
            ),
        ).order_by('-sequence').first()
    return render(request, 'Journeys.html', {'journeys': records})


def create_journey(request):
    owner = _session_owner(request)
    if owner is None:
        return JsonResponse({'error': 'Please sign in to plan a journey.'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'Use POST to save a journey.'}, status=405)

    origin_label = (request.POST.get('origin_label') or '').strip()
    destination_label = (request.POST.get('destination_label') or '').strip()
    departure = _local_datetime(request, request.POST.get('planned_departure'))
    arrival = _local_datetime(request, request.POST.get('expected_arrival'))
    try:
        origin_latitude = float(request.POST.get('origin_latitude', ''))
        origin_longitude = float(request.POST.get('origin_longitude', ''))
        destination_latitude = float(request.POST.get('destination_latitude', ''))
        destination_longitude = float(request.POST.get('destination_longitude', ''))
        distance_meters = int(request.POST.get('distance_meters', ''))
        duration_seconds = int(request.POST.get('duration_seconds', ''))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'The route preview has invalid distance or duration.'}, status=400)

    now = timezone.now()
    if not origin_label or not destination_label:
        return JsonResponse({'error': 'Choose both a starting location and a destination.'}, status=400)
    if len(origin_label) > 255 or len(destination_label) > 255:
        return JsonResponse({'error': 'Location labels must be 255 characters or fewer.'}, status=400)
    if not (-90 <= origin_latitude <= 90 and -90 <= destination_latitude <= 90
            and -180 <= origin_longitude <= 180 and -180 <= destination_longitude <= 180):
        return JsonResponse({'error': 'The route preview has invalid coordinates.'}, status=400)
    if departure is None or arrival is None or departure < now - timedelta(minutes=5) or arrival <= departure:
        return JsonResponse({'error': 'Choose valid departure and arrival times, with arrival after departure.'}, status=400)
    if distance_meters < 1 or distance_meters > 2000000 or duration_seconds < 1 or duration_seconds > 172800:
        return JsonResponse({'error': 'The route preview is outside the supported range.'}, status=400)

    guardian_enabled = request.POST.get('guardian_enabled') == 'true'
    try:
        checkin_interval = int(request.POST.get('checkin_interval_minutes', '15'))
        grace_period = int(request.POST.get('grace_period_minutes', '15'))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Choose valid check-in and grace intervals.'}, status=400)
    if checkin_interval not in (5, 10, 15) or grace_period not in (5, 10, 15):
        return JsonResponse({'error': 'Choose supported check-in and grace intervals.'}, status=400)
    auto_sos = request.POST.get('auto_sos_on_unconfirmed') == 'true'
    share_location = request.POST.get('share_location_with_contacts') == 'true'
    contact_ids = request.POST.getlist('guardian_contact_ids')
    contacts = EmergencyContact.objects.none()
    if guardian_enabled:
        if not contact_ids:
            return JsonResponse({'error': 'Select at least one active email contact for Journey Guardian.'}, status=400)
        contacts = EmergencyContact.objects.filter(
            user=owner,
            is_active=True,
            email_consent=True,
            email_verified_at__isnull=False,
            pk__in=contact_ids,
        ).exclude(email__isnull=True).exclude(email='')
        if contacts.count() != len(set(contact_ids)):
            return JsonResponse({'error': 'One or more selected contacts are unavailable. Refresh and try again.'}, status=400)

    journey = Journey.objects.create(
        user=owner,
        origin_label=origin_label,
        origin_latitude=origin_latitude,
        origin_longitude=origin_longitude,
        destination_label=destination_label,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        distance_meters=distance_meters,
        estimated_duration_seconds=duration_seconds,
        planned_departure=departure,
        expected_arrival=arrival,
        guardian_enabled=guardian_enabled,
        checkin_interval_minutes=checkin_interval,
        grace_period_minutes=grace_period,
        auto_sos_on_unconfirmed=auto_sos,
        share_location_with_contacts=share_location,
        location_status='PENDING' if share_location else 'NOT_SHARED',
    )
    if guardian_enabled:
        journey.guardian_contacts.set(contacts)
    JourneyActivity.objects.create(journey=journey, action=JourneyActivity.ACTION_PLANNED)
    return JsonResponse({
        'id': journey.pk,
        'status': journey.status,
        'journey_url': '/Journeys',
        'start_url': '/Route?journey_id={}'.format(journey.pk),
        'message': 'Journey plan saved. Start navigation when you are ready.',
    }, status=201)


def start_journey(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return JsonResponse({'error': 'Please sign in to start a journey.'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'Use POST to start a journey.'}, status=405)
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status == Journey.STATUS_ACTIVE:
        return JsonResponse({'status': journey.status, 'message': 'Journey is already active.'})
    if journey.status != Journey.STATUS_PLANNED:
        return JsonResponse({'error': 'Only a planned journey can be started.'}, status=409)
    if Journey.objects.filter(
        user=owner,
        status__in=(Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE),
    ).exists():
        return JsonResponse({'error': 'End your current active journey before starting another.'}, status=409)

    now = timezone.now()
    with transaction.atomic():
        changed = Journey.objects.filter(
            pk=journey.pk,
            user=owner,
            status=Journey.STATUS_PLANNED,
        ).update(
            status=Journey.STATUS_ACTIVE,
            started_at=now,
            updated_at=now,
        )
        if not changed:
            journey.refresh_from_db()
            return JsonResponse(
                {'error': 'This journey has already been started or ended.'},
                status=409,
            )
        journey.status = Journey.STATUS_ACTIVE
        journey.started_at = now
        schedule_checkin(journey, now=now)
        JourneyActivity.objects.create(journey=journey, action=JourneyActivity.ACTION_STARTED)
    return JsonResponse({'status': journey.status, 'message': 'Journey started.'})


def confirm_journey_safe(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('Journeys')
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status not in (
        Journey.STATUS_ACTIVE,
        Journey.STATUS_OVERDUE,
        Journey.STATUS_UNCONFIRMED,
    ):
        messages.error(request, 'Only an active journey can be checked in.')
        return redirect('Journeys')

    now = timezone.now()
    with transaction.atomic():
        current_checkin = journey.safety_checkins.filter(
            status__in=(
                SafetyCheckIn.STATUS_SCHEDULED,
                SafetyCheckIn.STATUS_DUE,
                SafetyCheckIn.STATUS_UNCONFIRMED,
            ),
        ).order_by('-sequence').first()
        if current_checkin is None:
            current_checkin = schedule_checkin(journey, now=now)
        confirmed = confirm_owner_checkin(journey, now)
        if confirmed is None:
            messages.error(request, 'This check-in was already processed. Refresh to see the current journey status.')
            return redirect('Journeys')

        if journey.expected_arrival <= now:
            journey.expected_arrival = now + timedelta(minutes=max(1, journey.checkin_interval_minutes))
            Journey.objects.filter(
                pk=journey.pk,
                status__in=(Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE, Journey.STATUS_UNCONFIRMED),
            ).update(
                status=Journey.STATUS_ACTIVE,
                expected_arrival=journey.expected_arrival,
                updated_at=now,
            )
            details = 'User confirmed they are safe after the deadline; the journey deadline was extended by one check-in interval.'
        else:
            Journey.objects.filter(
                pk=journey.pk,
                status__in=(Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE, Journey.STATUS_UNCONFIRMED),
            ).update(status=Journey.STATUS_ACTIVE, updated_at=now)
            details = 'User confirmed they are safe; the next scheduled check-in was created.'
        schedule_checkin(journey, now=now)
        JourneyActivity.objects.create(
            journey=journey,
            action=JourneyActivity.ACTION_CHECKED_IN,
            occurred_at=now,
            details=details,
        )

    if confirmed.auto_sos_event_id is not None:
        from .views import resolve_sos_event
        active_event = SOSEvent.objects.filter(
            pk=confirmed.auto_sos_event_id,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if active_event is not None:
            resolve_sos_event(active_event)
    messages.success(request, 'Your safety check-in was recorded.')
    return redirect('Journeys')


def extend_journey_deadline(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('Journeys')
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status not in (
        Journey.STATUS_ACTIVE,
        Journey.STATUS_OVERDUE,
        Journey.STATUS_UNCONFIRMED,
    ):
        messages.error(request, 'Only an active journey can have its check-in deadline extended.')
        return redirect('Journeys')
    try:
        minutes = int(request.POST.get('minutes', ''))
    except (TypeError, ValueError):
        minutes = 0
    if minutes not in (5, 10, 15, 30, 60, 120):
        messages.error(request, 'Choose a supported extension duration.')
        return redirect('Journeys')

    now = timezone.now()
    with transaction.atomic():
        changed = Journey.objects.filter(
            pk=journey.pk,
            status__in=(Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE, Journey.STATUS_UNCONFIRMED),
        ).update(
            expected_arrival=now + timedelta(minutes=minutes),
            status=Journey.STATUS_ACTIVE,
            updated_at=now,
        )
        if not changed:
            messages.error(request, 'The journey changed while extending its check-in deadline. Refresh and retry.')
            return redirect('Journeys')
        cancel_owner_checkins(journey, now)
        journey.expected_arrival = now + timedelta(minutes=minutes)
        journey.status = Journey.STATUS_ACTIVE
        schedule_checkin(journey, now=now)
        JourneyActivity.objects.create(
            journey=journey,
            action=JourneyActivity.ACTION_EXTENDED,
            occurred_at=now,
            details='Check-in deadline extended by {} minutes.'.format(minutes),
        )
    messages.success(request, 'The check-in deadline was extended.')
    return redirect('Journeys')


def end_journey(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('Journeys')
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status not in (Journey.STATUS_PLANNED, Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE):
        messages.error(request, 'This journey has already ended.')
        return redirect('Journeys')

    now = timezone.now()
    was_started = journey.started_at is not None
    with transaction.atomic():
        changed = Journey.objects.filter(
            pk=journey.pk,
            user=owner,
            status__in=(
                Journey.STATUS_PLANNED,
                Journey.STATUS_ACTIVE,
                Journey.STATUS_OVERDUE,
                Journey.STATUS_UNCONFIRMED,
            ),
        ).update(
            status=Journey.STATUS_COMPLETED if was_started else Journey.STATUS_CANCELLED,
            ended_at=now,
            updated_at=now,
        )
        if not changed:
            messages.error(request, 'This journey has already ended.')
            return redirect('Journeys')
        cancel_owner_checkins(journey, now)
        JourneyActivity.objects.create(
            journey=journey,
            action=JourneyActivity.ACTION_ENDED if was_started else JourneyActivity.ACTION_CANCELLED,
            occurred_at=now,
        )
    messages.success(request, 'Journey status: {}.'.format(journey.get_status_display()))
    return redirect('Journeys')


def journey_checkin_policy(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('Journeys')
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status != Journey.STATUS_PLANNED:
        messages.error(request, 'Check-in policy can be changed only before the journey starts.')
        return redirect('Journeys')

    try:
        interval = int(request.POST.get('checkin_interval_minutes', '15'))
        grace = int(request.POST.get('grace_period_minutes', '15'))
    except (TypeError, ValueError):
        messages.error(request, 'Choose valid check-in and grace intervals.')
        return redirect('Journeys')
    if interval not in (5, 10, 15) or grace not in (5, 10, 15):
        messages.error(request, 'Choose supported check-in and grace intervals.')
        return redirect('Journeys')

    journey.checkin_interval_minutes = interval
    journey.grace_period_minutes = grace
    journey.auto_sos_on_unconfirmed = request.POST.get('auto_sos_on_unconfirmed') == 'true'
    journey.share_location_with_contacts = request.POST.get('share_location_with_contacts') == 'true'
    journey.location_status = (
        'PENDING'
        if journey.share_location_with_contacts
        else 'NOT_SHARED'
    )
    journey.save(update_fields=[
        'checkin_interval_minutes',
        'grace_period_minutes',
        'auto_sos_on_unconfirmed',
        'share_location_with_contacts',
        'location_status',
        'updated_at',
    ])
    messages.success(request, 'Journey check-in policy saved. Automatic SOS remains off unless you selected it.')
    return redirect('Journeys')


def safety_checkin_location(request):
    owner = _session_owner(request)
    if owner is None:
        return JsonResponse({'error': 'Please sign in to update check-in status.'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'Use POST to update check-in status.'}, status=405)
    try:
        data = json.loads(request.body.decode('utf-8'))
        owner_id = int(data.get('owner_id'))
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'error': 'The status update is malformed.'}, status=400)

    owner_type = data.get('owner_type')
    if owner_type == 'journey':
        record = get_object_or_404(Journey, pk=owner_id, user=owner)
    elif owner_type == 'timer':
        record = get_object_or_404(SafetyTimer, pk=owner_id, user=owner)
    else:
        return JsonResponse({'error': 'Unknown check-in owner type.'}, status=400)

    if not update_owner_location(record, data):
        return JsonResponse({'error': 'The update is invalid, sharing is not enabled, or the journey has ended.'}, status=400)
    return JsonResponse({
        'location_status': data.get('location_status'),
        'last_device_seen_at': timezone.now().isoformat(),
    })


def safety_checkin_reminders(request):
    owner = _session_owner(request)
    if owner is None:
        return JsonResponse({'error': 'Please sign in to receive check-in reminders.'}, status=401)

    if request.method == 'GET':
        now = timezone.now()
        pending = SafetyCheckInAttempt.objects.filter(
            kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
            status=SafetyCheckInAttempt.STATUS_QUEUED,
            checkin__status=SafetyCheckIn.STATUS_DUE,
            checkin__grace_deadline__gt=now,
        ).filter(
            Q(checkin__journey__user=owner)
            | Q(checkin__safety_timer__user=owner)
        ).select_related(
            'checkin',
            'checkin__journey',
            'checkin__safety_timer',
        ).order_by('created_at', 'pk')[:5]

        reminders = []
        for attempt in pending:
            claim_token = uuid.uuid4()
            claim_generation = attempt.claim_generation + 1
            claimed = SafetyCheckInAttempt.objects.filter(
                pk=attempt.pk,
                status=SafetyCheckInAttempt.STATUS_QUEUED,
                claim_generation=attempt.claim_generation,
            ).update(
                status=SafetyCheckInAttempt.STATUS_SENDING,
                claim_token=claim_token,
                claim_generation=claim_generation,
                lease_expires_at=now + timedelta(minutes=2),
                attempted_at=now,
                updated_at=now,
            )
            if not claimed:
                continue
            is_journey = attempt.checkin.journey_id is not None
            reminders.append({
                'attempt_id': attempt.pk,
                'idempotency_key': str(attempt.idempotency_key),
                'claim_token': str(claim_token),
                'claim_generation': claim_generation,
                'title': 'Safety check-in due',
                'message': (
                    'Please confirm you are safe. A missed check-in or unavailable '
                    'location does not prove an emergency.'
                ),
                'url': reverse('Journeys') if is_journey else reverse('SafetyTimer'),
            })
        return JsonResponse({'reminders': reminders})

    if request.method != 'POST':
        return JsonResponse(
            {'error': 'Use GET to receive reminders or POST to record presentation.'},
            status=405,
        )

    try:
        payload = json.loads(request.body.decode('utf-8'))
        attempt_id = int(payload.get('attempt_id'))
        claim_generation = int(payload.get('claim_generation'))
        claim_token = uuid.UUID(str(payload.get('claim_token')))
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'error': 'The reminder presentation receipt is invalid.'}, status=400)

    channel = payload.get('delivery_channel')
    if channel not in (
        SafetyCheckInAttempt.DELIVERY_CHANNEL_IN_APP,
        SafetyCheckInAttempt.DELIVERY_CHANNEL_BROWSER,
    ):
        return JsonResponse({'error': 'The reminder delivery channel is invalid.'}, status=400)

    now = timezone.now()
    owned_attempt = SafetyCheckInAttempt.objects.filter(
        pk=attempt_id,
        kind=SafetyCheckInAttempt.KIND_USER_REMINDER,
        status=SafetyCheckInAttempt.STATUS_SENDING,
        claim_token=claim_token,
        claim_generation=claim_generation,
        lease_expires_at__gt=now,
        checkin__status=SafetyCheckIn.STATUS_DUE,
        checkin__grace_deadline__gt=now,
    ).filter(
        Q(checkin__journey__user=owner)
        | Q(checkin__safety_timer__user=owner)
    )
    updated = owned_attempt.update(
        status=SafetyCheckInAttempt.STATUS_ACCEPTED,
        delivery_channel=channel,
        completed_at=now,
        lease_expires_at=None,
        error_message='Presented by the browser; this does not confirm the user saw or acted on it.',
        updated_at=now,
    )
    if not updated:
        return JsonResponse({'error': 'This reminder is no longer available.'}, status=409)
    return JsonResponse({'accepted': True})
