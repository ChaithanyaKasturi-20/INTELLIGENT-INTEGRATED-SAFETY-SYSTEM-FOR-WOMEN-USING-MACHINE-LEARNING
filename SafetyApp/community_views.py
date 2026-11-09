import logging
import uuid
from datetime import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import CommunityReport, CommunityReportFlag


logger = logging.getLogger(__name__)


def _session_owner(request):
    owner = request.session.get('username')
    if not request.session.get('is_logged_in') or not isinstance(owner, str) or not owner:
        return None
    return owner


def _incident_datetime(request):
    try:
        parsed = datetime.strptime(request.POST.get('incident_at', ''), '%Y-%m-%dT%H:%M')
        offset_minutes = int(request.POST.get('timezone_offset_minutes', ''))
    except (TypeError, ValueError):
        return None
    if offset_minutes < -14 * 60 or offset_minutes > 14 * 60:
        return None
    return timezone.make_aware(parsed, timezone.get_fixed_timezone(-offset_minutes))


def _approved_reports_for_request(request):
    reports = CommunityReport.objects.filter(
        status=CommunityReport.STATUS_APPROVED,
    ).order_by('-incident_at', '-created_at')
    category = request.GET.get('category', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    filter_error = ''

    valid_categories = {value for value, label in CommunityReport.CATEGORY_CHOICES}
    if category:
        if category in valid_categories:
            reports = reports.filter(category=category)
        else:
            filter_error = 'Choose a valid report category.'
            return reports.none(), filter_error

    parsed_dates = {}
    for parameter in ('date_from', 'date_to'):
        raw_date = request.GET.get(parameter, '')
        if not raw_date:
            continue
        try:
            parsed_dates[parameter] = datetime.strptime(raw_date, '%Y-%m-%d').date()
        except ValueError:
            filter_error = 'Enter valid incident dates using YYYY-MM-DD.'
            break
    if not filter_error and 'date_from' in parsed_dates and 'date_to' in parsed_dates:
        if parsed_dates['date_from'] > parsed_dates['date_to']:
            filter_error = 'The start date must be on or before the end date.'
    if filter_error:
        return reports.none(), filter_error
    if 'date_from' in parsed_dates:
        reports = reports.filter(incident_at__date__gte=parsed_dates['date_from'])
    if 'date_to' in parsed_dates:
        reports = reports.filter(incident_at__date__lte=parsed_dates['date_to'])
    return reports, ''


def community_reports(request):
    reports, filter_error = _approved_reports_for_request(request)
    category = request.GET.get('category', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reports_page = Paginator(reports, 10).get_page(request.GET.get('page'))
    query = request.GET.copy()
    previous_page_url = next_page_url = ''
    if reports_page.has_previous():
        query['page'] = reports_page.previous_page_number()
        previous_page_url = '?' + query.urlencode()
    if reports_page.has_next():
        query['page'] = reports_page.next_page_number()
        next_page_url = '?' + query.urlencode()

    owner = _session_owner(request)
    my_reports_page = None
    my_reports_previous_url = my_reports_next_url = ''
    submission_key = ''
    if owner is not None:
        my_reports_page = Paginator(
            CommunityReport.objects.filter(reporter=owner).order_by('-created_at'),
            10,
        ).get_page(request.GET.get('my_page'))
        query = request.GET.copy()
        if my_reports_page.has_previous():
            query['my_page'] = my_reports_page.previous_page_number()
            my_reports_previous_url = '?' + query.urlencode()
        if my_reports_page.has_next():
            query['my_page'] = my_reports_page.next_page_number()
            my_reports_next_url = '?' + query.urlencode()
        submission_key = uuid.uuid4()

    return render(request, 'CommunityReports.html', {
        'reports': reports_page.object_list,
        'reports_page': reports_page,
        'previous_page_url': previous_page_url,
        'next_page_url': next_page_url,
        'my_reports': my_reports_page.object_list if my_reports_page else (),
        'my_reports_page': my_reports_page,
        'is_reporter': owner is not None,
        'my_reports_previous_url': my_reports_previous_url,
        'my_reports_next_url': my_reports_next_url,
        'submission_key': submission_key,
        'selected_category': category,
        'date_from': date_from,
        'date_to': date_to,
        'filter_error': filter_error,
        'categories': CommunityReport.CATEGORY_CHOICES,
    })


def community_report_map_data(request):
    reports, filter_error = _approved_reports_for_request(request)
    if filter_error:
        return JsonResponse({'reports': [], 'error': filter_error}, status=400)
    return JsonResponse({
        'reports': [{
            'id': report.pk,
            'category': report.get_category_display(),
            'description': report.description,
            'latitude': report.approximate_latitude,
            'longitude': report.approximate_longitude,
            'incident_at': timezone.localtime(report.incident_at).strftime('%Y-%m-%d %H:%M'),
            'submitted_at': timezone.localtime(report.created_at).strftime('%Y-%m-%d'),
        } for report in reports[:500]],
    })


def submit_community_report(request):
    owner = _session_owner(request)
    if owner is None:
        return redirect('UserLogin')
    if request.method != 'POST':
        return redirect('CommunityReports')

    try:
        submission_key = uuid.UUID(request.POST.get('submission_key', ''))
    except (TypeError, ValueError, AttributeError):
        messages.error(request, 'This submission form has expired. Reload the page and try again.')
        return redirect('CommunityReports')

    existing_report = CommunityReport.objects.filter(submission_key=submission_key).only('reporter').first()
    if existing_report is not None:
        if existing_report.reporter == owner:
            messages.info(request, 'This report was already received; no duplicate was created.')
        else:
            messages.error(request, 'This submission could not be confirmed. Reload the page and try again.')
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
        submission_key=submission_key,
        category=request.POST.get('category', ''),
        description=(request.POST.get('description') or '').strip(),
        approximate_latitude=latitude,
        approximate_longitude=longitude,
        incident_at=incident_at,
        evidence=request.FILES.get('evidence'),
        status=CommunityReport.STATUS_SUBMITTED,
    )
    try:
        with transaction.atomic():
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
    except IntegrityError:
        _discard_unpersisted_evidence(report)
        existing_report = CommunityReport.objects.filter(submission_key=submission_key).only('reporter').first()
        if existing_report is not None and existing_report.reporter == owner:
            messages.info(request, 'This report was already received; no duplicate was created.')
        else:
            messages.error(request, 'We could not confirm this report. Please reload the form before trying again.')
        return redirect('CommunityReports')
    except DatabaseError as exc:
        _discard_unpersisted_evidence(report)
        logger.error('Community report save failed (%s).', exc.__class__.__name__)
        messages.error(request, 'We could not save your report. Please try again later.')
        return redirect('CommunityReports')
    except (NotImplementedError, OSError) as exc:
        _discard_unpersisted_evidence(report)
        logger.error('Community report storage failed (%s).', exc.__class__.__name__)
        messages.error(request, 'We could not store your evidence. Please try again later.')
        return redirect('CommunityReports')

    messages.success(
        request,
        'Your report was submitted for moderator review. It is not visible in the community feed until approved.',
    )
    return redirect('CommunityReports')


def _discard_unpersisted_evidence(report):
    if not report.evidence or not report.evidence.name:
        return
    try:
        report.evidence.storage.delete(report.evidence.name)
    except (OSError, NotImplementedError) as exc:
        logger.warning('Could not remove unpersisted report evidence (%s).', exc.__class__.__name__)


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
