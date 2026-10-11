from datetime import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import CommunityReport, CommunityReportFlag


def _session_owner(request):
    if not request.session.get('is_logged_in'):
        return None
    return request.session.get('username')


def _incident_datetime(request):
    try:
        parsed = datetime.strptime(request.POST.get('incident_at', ''), '%Y-%m-%dT%H:%M')
        offset_minutes = int(request.POST.get('timezone_offset_minutes', ''))
    except (TypeError, ValueError):
        return None
    if offset_minutes < -14 * 60 or offset_minutes > 14 * 60:
        return None
    return timezone.make_aware(parsed, timezone.get_fixed_timezone(-offset_minutes))


def community_reports(request):
    reports = CommunityReport.objects.filter(status=CommunityReport.STATUS_APPROVED).order_by('-incident_at')
    return render(request, 'CommunityReports.html', {
        'reports': reports,
        'categories': CommunityReport.CATEGORY_CHOICES,
    })


def community_report_map_data(request):
    reports = CommunityReport.objects.filter(
        status=CommunityReport.STATUS_APPROVED,
    ).order_by('-incident_at')
    return JsonResponse({
        'reports': [{
            'id': report.pk,
            'category': report.get_category_display(),
            'description': report.description,
            'latitude': report.approximate_latitude,
            'longitude': report.approximate_longitude,
            'incident_at': timezone.localtime(report.incident_at).strftime('%Y-%m-%d %H:%M'),
        } for report in reports[:500]],
    })


def submit_community_report(request):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('CommunityReports')

    incident_at = _incident_datetime(request)
    try:
        latitude = float(request.POST.get('approximate_latitude', ''))
        longitude = float(request.POST.get('approximate_longitude', ''))
    except (TypeError, ValueError):
        latitude = None
        longitude = None
    if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        messages.error(request, 'Choose an approximate incident location on the map or with device location.')
        return redirect('CommunityReports')
    if incident_at is None or incident_at > timezone.now():
        messages.error(request, 'Choose a valid incident date and time in the past.')
        return redirect('CommunityReports')

    report = CommunityReport(
        reporter=owner,
        category=request.POST.get('category', ''),
        description=(request.POST.get('description') or '').strip(),
        approximate_latitude=latitude,
        approximate_longitude=longitude,
        incident_at=incident_at,
        evidence=request.FILES.get('evidence'),
        status=CommunityReport.STATUS_SUBMITTED,
    )
    try:
        report.full_clean()
        report.save()
    except ValidationError as exc:
        if hasattr(exc, 'message_dict'):
            errors = [message for field_errors in exc.message_dict.values() for message in field_errors]
        else:
            errors = exc.messages
        for error in errors:
            messages.error(request, error)
        return redirect('CommunityReports')

    messages.success(
        request,
        'Your report was submitted for moderator review. It is not visible in the community feed until approved.',
    )
    return redirect('CommunityReports')


def flag_community_report(request, report_id):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('CommunityReports')

    report = get_object_or_404(
        CommunityReport,
        pk=report_id,
        status=CommunityReport.STATUS_APPROVED,
    )
    reason = (request.POST.get('reason') or '').strip()
    valid_reasons = ('Inaccurate information', 'Abusive or harmful', 'Personal information', 'Other concern')
    if reason not in valid_reasons:
        messages.error(request, 'Choose a valid reason for flagging this report.')
        return redirect('CommunityReports')
    flag, created = CommunityReportFlag.objects.get_or_create(
        report=report,
        reporter=owner,
        defaults={'reason': reason},
    )
    if created:
        messages.success(request, 'The report was flagged for moderator review.')
    else:
        messages.info(request, 'You have already flagged this report.')
    return redirect('CommunityReports')
