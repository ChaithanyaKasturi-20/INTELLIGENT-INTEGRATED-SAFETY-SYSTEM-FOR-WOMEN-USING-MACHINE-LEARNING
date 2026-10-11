from datetime import datetime, timedelta

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import EmergencyContact, Journey, JourneyActivity


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
    records = Journey.objects.filter(user=owner).prefetch_related('activities', 'guardian_contacts')
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
    contact_ids = request.POST.getlist('guardian_contact_ids')
    contacts = EmergencyContact.objects.none()
    if guardian_enabled:
        if not contact_ids:
            return JsonResponse({'error': 'Select at least one active email contact for Journey Guardian.'}, status=400)
        contacts = EmergencyContact.objects.filter(
            user=owner,
            is_active=True,
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

    journey.status = Journey.STATUS_ACTIVE
    journey.started_at = timezone.now()
    journey.save(update_fields=['status', 'started_at', 'updated_at'])
    JourneyActivity.objects.create(journey=journey, action=JourneyActivity.ACTION_STARTED)
    return JsonResponse({'status': journey.status, 'message': 'Journey started.'})


def confirm_journey_safe(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('Journeys')
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status not in (Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE):
        messages.error(request, 'Only an active journey can be checked in.')
        return redirect('Journeys')

    details = 'User confirmed they are safe; no SOS was created.'
    if journey.status == Journey.STATUS_OVERDUE:
        journey.status = Journey.STATUS_ACTIVE
        journey.expected_arrival = timezone.now() + timedelta(minutes=15)
        journey.save(update_fields=['status', 'expected_arrival', 'updated_at'])
        details = 'User checked in after the previous deadline; a new 15-minute check-in deadline was set.'
    JourneyActivity.objects.create(
        journey=journey,
        action=JourneyActivity.ACTION_CHECKED_IN,
        details=details,
    )
    messages.success(request, 'Your safety check-in was recorded. No SOS was activated.')
    return redirect('Journeys')


def extend_journey_deadline(request, journey_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('Journeys')
    journey = get_object_or_404(Journey, pk=journey_id, user=owner)
    if journey.status not in (Journey.STATUS_ACTIVE, Journey.STATUS_OVERDUE):
        messages.error(request, 'Only an active journey can have its check-in deadline extended.')
        return redirect('Journeys')
    try:
        minutes = int(request.POST.get('minutes', ''))
    except (TypeError, ValueError):
        minutes = 0
    if minutes not in (5, 10, 15, 30, 60, 120):
        messages.error(request, 'Choose a supported extension duration.')
        return redirect('Journeys')

    journey.expected_arrival = timezone.now() + timedelta(minutes=minutes)
    journey.status = Journey.STATUS_ACTIVE
    journey.save(update_fields=['expected_arrival', 'status', 'updated_at'])
    JourneyActivity.objects.create(
        journey=journey,
        action=JourneyActivity.ACTION_EXTENDED,
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
    journey.status = Journey.STATUS_COMPLETED if was_started else Journey.STATUS_CANCELLED
    journey.ended_at = now
    journey.save(update_fields=['status', 'ended_at', 'updated_at'])
    JourneyActivity.objects.create(
        journey=journey,
        action=JourneyActivity.ACTION_ENDED if was_started else JourneyActivity.ACTION_CANCELLED,
    )
    messages.success(request, 'Journey status: {}.'.format(journey.get_status_display()))
    return redirect('Journeys')
