from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.template import RequestContext
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.core.cache import cache
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
import os
import io
import json
import hashlib
import ipaddress
import math
import base64
import secrets
import sqlite3
import logging
import re
import socket
import urllib.parse
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import pickle
import smtplib
from datetime import date, timedelta
import seaborn as sns
from django.utils import timezone

from .models import EmergencyContact, SafetyTimer as SafetyTimerModel, SOSEvent, SOSEmailDelivery, SOSLiveLocationShare, normalize_emergency_email

logger = logging.getLogger(__name__)

global username, email
dataset = pd.read_csv("Dataset/CrimesOnWomenData.csv")
states = np.unique(dataset['State'])


def load_local_env():
    env_path = os.path.join(settings.BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return

    with open(env_path, 'r', encoding='utf-8') as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_panic_email_config():
    load_local_env()
    sender_email = (os.environ.get('PANIC_EMAIL_USER', '') or '').strip()
    sender_password = ''.join(ch for ch in (os.environ.get('PANIC_EMAIL_PASS', '') or '') if not ch.isspace())
    return sender_email, sender_password


def get_register_db_connection():
    conn = sqlite3.connect(os.path.join(settings.BASE_DIR, 'db.sqlite3'))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_register_table():
    with get_register_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS register (
                username TEXT PRIMARY KEY,
                password TEXT,
                contact_no TEXT,
                email TEXT,
                address TEXT
            )
        """)
        conn.commit()


def require_logged_in_user(request):
    current_user = request.session.get('username') or globals().get('username')
    if not current_user:
        return None, redirect('UserLogin')
    return current_user, None


def get_active_sos_for_user(user):
    return SOSEvent.objects.filter(user=user, status=SOSEvent.STATUS_ACTIVE).order_by('-activated_at').first()


def get_current_safety_timer_for_user(user):
    if not user:
        return None
    return SafetyTimerModel.objects.filter(user=user).order_by('-started_at').first()


def process_expired_safety_timer_for_user(user):
    if not user:
        return None

    timer = SafetyTimerModel.objects.filter(user=user, status=SafetyTimerModel.STATUS_ACTIVE).order_by('-started_at').first()
    if timer is None:
        return None

    if timezone.now() < timer.expires_at:
        return None

    timer.mark_expired()
    active_event = get_active_sos_for_user(user)
    if active_event is None:
        active_event = SOSEvent.objects.create(
            user=user,
            latitude=None,
            longitude=None,
            status=SOSEvent.STATUS_ACTIVE,
            activated_at=timezone.now(),
        )
        share_token = create_sos_live_location_share(active_event)
        send_sos_emails_for_event(active_event, share_token=share_token)

    return timer


def get_registered_emergency_email_for_user(user):
    if not user:
        return ''

    with get_register_db_connection() as conn:
        row = conn.execute(
            "SELECT email FROM register WHERE username = ?",
            (user,),
        ).fetchone()

    if row is None:
        return ''
    return (row['email'] or '').strip()


def sync_signup_emergency_contact_for_user(user, email_value=None, phone_number=None):
    if not user:
        return None

    signup_email = (email_value or get_registered_emergency_email_for_user(user) or '').strip()
    signup_email = normalize_emergency_email(signup_email)
    if not signup_email:
        return None

    existing_contacts = EmergencyContact.objects.filter(user=user)
    matching_contact = existing_contacts.filter(email__iexact=signup_email).first()
    if matching_contact is not None:
        if not matching_contact.is_active or not matching_contact.is_primary:
            matching_contact.is_active = True
            matching_contact.is_primary = True
            matching_contact.name = matching_contact.name or 'Emergency Contact'
            matching_contact.phone_number = (matching_contact.phone_number or phone_number or '0000000000').strip() or '0000000000'
            matching_contact.relationship = matching_contact.relationship or 'Registered emergency contact'
            matching_contact.save(update_fields=['is_active', 'is_primary', 'name', 'phone_number', 'relationship'])
        return matching_contact

    fallback_name = 'Emergency Contact'
    fallback_phone = (phone_number or '0000000000').strip() or '0000000000'
    contact = EmergencyContact.objects.create(
        user=user,
        email=signup_email,
        name=fallback_name,
        phone_number=fallback_phone,
        relationship='Registered emergency contact',
        is_primary=True,
        is_active=True,
    )
    return contact


def get_signup_emergency_contact_for_user(user):
    return sync_signup_emergency_contact_for_user(user)


def SafetyTimer(request):
    if request.method != 'GET':
        return redirect('UserLogin')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    timer = process_expired_safety_timer_for_user(current_user)
    if timer is None:
        timer = get_current_safety_timer_for_user(current_user)

    if timer is None and not get_active_sos_for_user(current_user):
        return redirect('Panic')

    active_event = get_active_sos_for_user(current_user)
    return render(request, 'SafetyTimer.html', {
        'current_user': current_user,
        'timer': timer,
        'active_event': active_event,
        'emergency_contact_count': EmergencyContact.objects.filter(user=current_user, is_active=True).count(),
        'delivery_summary': get_sos_delivery_summary(active_event),
        'public_location_link_configured': bool(build_sos_live_location_url('configuration-check')),
    })


def SafetyTimerAction(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    duration_value = request.POST.get('duration', '15')
    try:
        duration_minutes = int(duration_value)
    except (TypeError, ValueError):
        duration_minutes = 15
    duration_minutes = max(1, duration_minutes)

    SafetyTimerModel.objects.filter(user=current_user, status=SafetyTimerModel.STATUS_ACTIVE).update(
        status=SafetyTimerModel.STATUS_CANCELLED,
        completed_at=timezone.now(),
    )

    timer = SafetyTimerModel.objects.create(
        user=current_user,
        started_at=timezone.now(),
        expires_at=timezone.now() + timedelta(minutes=duration_minutes),
        duration=duration_minutes,
        status=SafetyTimerModel.STATUS_ACTIVE,
    )
    return redirect('SafetyTimer')


def SafetyTimerSafe(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    timer = get_current_safety_timer_for_user(current_user)
    if timer is not None and timer.status in (SafetyTimerModel.STATUS_ACTIVE, SafetyTimerModel.STATUS_EXPIRED):
        timer.mark_completed()

    event = get_active_sos_for_user(current_user)
    if event is not None:
        resolve_sos_event(event)
        messages.success(request, 'SOS resolved. Location updates and recipient access have ended.')

    return redirect('SafetyTimer')


def SafetyTimerCancel(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    timer = get_current_safety_timer_for_user(current_user)
    if timer is not None and timer.status == SafetyTimerModel.STATUS_ACTIVE:
        timer.mark_cancelled()

    return redirect('SafetyTimer')


def build_sos_email_subject(user_name):
    safe_user_name = (user_name or 'User').strip() or 'User'
    return 'SOS Emergency Alert - {}'.format(safe_user_name)


SOS_LIVE_LOCATION_TTL = timedelta(hours=24)
SOS_LIVE_LOCATION_STALE_AFTER = timedelta(minutes=2)


def create_sos_live_location_share(sos_event):
    token = secrets.token_urlsafe(32)
    SOSLiveLocationShare.objects.create(
        sos_event=sos_event,
        token_hash=hashlib.sha256(token.encode('utf-8')).hexdigest(),
        expires_at=timezone.now() + SOS_LIVE_LOCATION_TTL,
    )
    return token


def resolve_sos_event(event, status=SOSEvent.STATUS_RESOLVED):
    if status == SOSEvent.STATUS_CANCELLED:
        event.mark_cancelled()
    else:
        event.mark_resolved()

    try:
        event.live_location_share.revoke()
    except SOSLiveLocationShare.DoesNotExist:
        pass


def build_sos_live_location_url(token):
    public_base_url = get_valid_sos_public_base_url()
    if not public_base_url:
        return ''

    page_url = public_base_url + reverse('SOSLiveLocationPage')
    return '{}#{}'.format(page_url, token) if page_url else ''


def get_valid_sos_public_base_url():
    load_local_env()
    public_base_url = (
        os.environ.get('SOS_PUBLIC_BASE_URL')
        or getattr(settings, 'SOS_PUBLIC_BASE_URL', '')
        or ''
    ).strip().rstrip('/')
    try:
        parsed_base_url = urllib.parse.urlsplit(public_base_url)
        hostname = parsed_base_url.hostname
        port = parsed_base_url.port
    except ValueError:
        return ''

    if (
        parsed_base_url.scheme != 'https'
        or not parsed_base_url.netloc
        or not hostname
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.path not in ('', '/')
        or parsed_base_url.query
        or parsed_base_url.fragment
    ):
        return ''

    hostname = hostname.rstrip('.').lower()
    try:
        hostname = hostname.encode('idna').decode('ascii')
    except UnicodeError:
        return ''
    if '%' in hostname or (port is not None and not 1 <= port <= 65535):
        return ''

    blocked_suffixes = ('.localhost', '.local', '.internal', '.lan', '.home', '.test', '.invalid', '.example')
    if hostname == 'localhost' or hostname.endswith(blocked_suffixes) or '.' not in hostname:
        return ''
    if any(
        not label
        or len(label) > 63
        or label.startswith('-')
        or label.endswith('-')
        or not re.match(r'^[a-z0-9-]+$', label)
        for label in hostname.split('.')
    ):
        return ''
    if len(hostname) > 253:
        return ''

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        return public_base_url if address.is_global else ''

    try:
        resolved_addresses = {
            ipaddress.ip_address(result[4][0].split('%', 1)[0])
            for result in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError, IndexError):
        return ''
    if not resolved_addresses or not all(address.is_global for address in resolved_addresses):
        return ''

    return public_base_url


def build_sos_email_body(sos_event, user_name, live_location_url=''):
    name = (user_name or 'User').strip() or 'User'
    lines = [
        'Hello,',
        '{} has activated an SOS emergency alert.'.format(name),
        'Activation time: {}'.format(timezone.localtime(sos_event.activated_at).strftime('%Y-%m-%d %H:%M:%S %Z')),
    ]

    if sos_event.latitude is not None and sos_event.longitude is not None:
        map_link = 'https://www.google.com/maps?q={},{}'.format(sos_event.latitude, sos_event.longitude)
        lines.append('Location: {}, {}'.format(sos_event.latitude, sos_event.longitude))
        lines.append('Google Maps: {}'.format(map_link))
    else:
        lines.append('Location is currently unavailable.')

    if live_location_url:
        lines.extend([
            'Live location sharing link (expires in 24 hours and is revoked when the SOS ends):',
            live_location_url,
            'The live map updates only while the user’s device and browser remain online with location access enabled.',
        ])
    else:
        lines.append(
            'A secure external live-location link is not configured for this alert. '
            'This email contains only the initial location, if available.'
        )

    return '\n\n'.join(lines)


def get_sos_delivery_summary(event):
    if event is None:
        return {
            'sent': 0,
            'failed': 0,
            'pending': 0,
            'no_email': 0,
            'unattempted': 0,
            'can_retry': False,
            'failures': [],
        }

    deliveries = SOSEmailDelivery.objects.filter(sos_event=event)
    contact_queryset = EmergencyContact.objects.filter(user=event.user, is_active=True)
    eligible_contacts = contact_queryset.exclude(email__isnull=True).exclude(email='')
    no_email = contact_queryset.filter(Q(email__isnull=True) | Q(email=''))

    registered_email = get_registered_emergency_email_for_user(event.user)
    if not eligible_contacts.exists() and registered_email:
        fallback_contact = get_signup_emergency_contact_for_user(event.user)
        if fallback_contact is not None:
            eligible_contacts = EmergencyContact.objects.filter(pk=fallback_contact.pk)
            no_email = EmergencyContact.objects.none()

    failed_deliveries = deliveries.filter(
        status=SOSEmailDelivery.STATUS_FAILED
    ).select_related('emergency_contact')
    retry_safe = list(failed_deliveries.values_list('error_message', flat=True))

    return {
        'sent': deliveries.filter(status=SOSEmailDelivery.STATUS_SENT).count(),
        'failed': deliveries.filter(status=SOSEmailDelivery.STATUS_FAILED).count(),
        'pending': deliveries.filter(status=SOSEmailDelivery.STATUS_PENDING).count(),
        'no_email': no_email.count(),
        'eligible_contacts': eligible_contacts.count(),
        'unattempted': max(eligible_contacts.count() - deliveries.count(), 0),
        'can_retry': (
            event.status == SOSEvent.STATUS_ACTIVE
            and bool(retry_safe)
            and all(is_sos_delivery_retry_safe(error) for error in retry_safe)
            and not deliveries.filter(status=SOSEmailDelivery.STATUS_SENT).exists()
        ),
        'failures': [
            {
                'contact_name': delivery.emergency_contact.name,
                'reason': safe_sos_email_failure_reason(delivery.error_message),
            }
            for delivery in failed_deliveries
        ],
    }


def safe_sos_email_failure_reason(error):
    if isinstance(error, Exception):
        if isinstance(error, smtplib.SMTPAuthenticationError):
            return 'The SMTP server rejected the sender credentials. Check the Gmail App Password configuration.'
        if isinstance(error, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError)):
            return 'Could not connect to the SMTP server. Check the network and SMTP availability.'
        if isinstance(error, smtplib.SMTPRecipientsRefused):
            return 'The SMTP server refused this recipient.'
        if isinstance(error, smtplib.SMTPResponseException):
            return 'The SMTP server rejected the message (response code {}).'.format(error.smtp_code)
        error = str(error)

    message = (error or '').lower()
    if 'sos_public_base_url' in message or 'publicly reachable https' in message:
        return 'No email was sent because SOS_PUBLIC_BASE_URL is not configured as a public HTTPS origin.'
    if 'smtp is not configured' in message or 'panic_email_user' in message or 'panic_email_pass' in message:
        return 'Email sending is not configured. Set PANIC_EMAIL_USER and PANIC_EMAIL_PASS.'
    if 'invalid recipient' in message or 'invalid email' in message:
        return 'This emergency contact has an invalid email address.'
    if 'authentication' in message or 'credentials' in message or 'login' in message:
        return 'The SMTP server rejected the sender credentials. Check the Gmail App Password configuration.'
    if 'connect' in message or 'timeout' in message or 'disconnected' in message:
        return 'Could not connect to the SMTP server. Check the network and SMTP availability.'
    return 'The SMTP provider did not accept this email. Sensitive technical details are hidden.'


def is_sos_delivery_retry_safe(error_message):
    message = (error_message or '').lower()
    return any(marker in message for marker in (
        'sos_public_base_url',
        'smtp is not configured',
        'sender credentials',
        'could not connect to the smtp server',
        'smtp server refused this recipient',
        'invalid email address',
        'invalid recipient',
        'smtp server rejected the message (response code',
    ))


def send_sos_emails_for_event(sos_event, share_token=None, delivery_ids=None):
    if (
        sos_event is None
        or sos_event.status != SOSEvent.STATUS_ACTIVE
        or not SOSEvent.objects.filter(pk=sos_event.pk, status=SOSEvent.STATUS_ACTIVE).exists()
    ):
        return False

    public_link_configured = bool(build_sos_live_location_url('configuration-check'))
    if share_token is None and public_link_configured:
        try:
            share_token = create_sos_live_location_share(sos_event)
        except Exception:
            logger.exception('Unable to create live location sharing access for SOS event %s', sos_event.pk)
            share_token = None

    eligible_contacts = list(EmergencyContact.objects.filter(user=sos_event.user, is_active=True).exclude(email__isnull=True).exclude(email='').order_by('name'))
    if not eligible_contacts:
        fallback_contact = get_signup_emergency_contact_for_user(sos_event.user)
        if fallback_contact is not None:
            eligible_contacts = [fallback_contact]
        else:
            return True
    if delivery_ids is not None:
        retry_contact_ids = set(SOSEmailDelivery.objects.filter(
            pk__in=delivery_ids,
            sos_event=sos_event,
        ).values_list('emergency_contact_id', flat=True))
        eligible_contacts = [contact for contact in eligible_contacts if contact.pk in retry_contact_ids]

    live_location_url = build_sos_live_location_url(share_token) if share_token else ''

    sender_email, sender_password = get_panic_email_config()

    if not sender_email or not sender_password:
        for contact in eligible_contacts:
            delivery, created = SOSEmailDelivery.objects.get_or_create(sos_event=sos_event, emergency_contact=contact)
            if delivery.status == SOSEmailDelivery.STATUS_SENT or delivery.status == SOSEmailDelivery.STATUS_FAILED:
                continue
            delivery.mark_failed(safe_sos_email_failure_reason('SMTP is not configured for this environment.'))
        return True

    sendable_contacts = []
    for contact in eligible_contacts:
        delivery, created = SOSEmailDelivery.objects.get_or_create(sos_event=sos_event, emergency_contact=contact)
        if delivery.status == SOSEmailDelivery.STATUS_SENT or delivery.status == SOSEmailDelivery.STATUS_FAILED:
            continue
        try:
            validate_email(contact.email)
        except ValidationError:
            delivery.mark_failed('This emergency contact has an invalid email address.')
            continue
        sendable_contacts.append((contact, delivery))

    if not sendable_contacts:
        return True

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as connection:
            connection.login(sender_email, sender_password)
            for contact, delivery in sendable_contacts:
                message = build_sos_email_body(sos_event, sos_event.user, live_location_url)
                msg = "From: {}\nTo: {}\nSubject: {}\n\n{}".format(
                    sender_email,
                    contact.email,
                    build_sos_email_subject(sos_event.user),
                    message,
                )
                try:
                    refused = connection.sendmail(from_addr=sender_email, to_addrs=[contact.email], msg=msg)
                    if isinstance(refused, dict) and refused:
                        refusal = next(iter(refused.values()))
                        code = refusal[0] if isinstance(refusal, tuple) else None
                        reason = 'The SMTP server refused this recipient.'
                        if code:
                            reason = 'The SMTP server refused this recipient (response code {}).'.format(code)
                        delivery.mark_failed(reason)
                    else:
                        delivery.mark_sent()
                except Exception as exc:
                    reason = safe_sos_email_failure_reason(exc)
                    logger.warning('SOS email delivery failed for event %s: %s', sos_event.pk, reason)
                    delivery.mark_failed(reason)
    except Exception as exc:
        reason = safe_sos_email_failure_reason(exc)
        logger.warning('SOS email batch failed for event %s: %s', sos_event.pk, reason)
        for contact, delivery in sendable_contacts:
            if delivery.status == SOSEmailDelivery.STATUS_SENT or delivery.status == SOSEmailDelivery.STATUS_FAILED:
                continue
            delivery.mark_failed(reason)
    return True


def SOSDashboard(request):
    return Panic(request)


def ActivateSOS(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    if request.method != 'POST':
        return redirect('SOSDashboard')

    active_event = get_active_sos_for_user(current_user)
    if active_event is not None:
        return redirect('SOSDashboard')

    latitude, longitude = parse_sos_coordinates(
        request.POST.get('latitude', ''),
        request.POST.get('longitude', ''),
    )

    event = SOSEvent.objects.create(
        user=current_user,
        latitude=latitude,
        longitude=longitude,
        location_updated_at=timezone.now() if latitude is not None else None,
        status=SOSEvent.STATUS_ACTIVE,
        activated_at=timezone.now(),
    )
    share_token = create_sos_live_location_share(event)
    send_sos_emails_for_event(event, share_token=share_token)
    return redirect('SOSDashboard')


def parse_sos_coordinates(latitude_value, longitude_value):
    try:
        latitude = float(latitude_value)
        longitude = float(longitude_value)
    except (TypeError, ValueError):
        return None, None

    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None, None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None, None
    return latitude, longitude


def UpdateSOSStatus(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    if request.method != 'POST':
        return redirect('SOSDashboard')

    status_value = (request.POST.get('status', '') or '').strip().upper()
    event = SOSEvent.objects.filter(user=current_user, status=SOSEvent.STATUS_ACTIVE).order_by('-activated_at').first()
    if event is None:
        return redirect('SOSDashboard')

    if status_value == SOSEvent.STATUS_CANCELLED:
        resolve_sos_event(event, SOSEvent.STATUS_CANCELLED)
        messages.success(request, 'SOS cancelled. Location updates and recipient access have ended.')
    elif status_value == SOSEvent.STATUS_RESOLVED:
        resolve_sos_event(event)
        messages.success(request, 'SOS resolved. Location updates and recipient access have ended.')

    return redirect('SOSDashboard')


def SOSLocationUpdate(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)

    current_user = request.session.get('username')
    if not current_user:
        return JsonResponse({'error': 'Authentication is required.'}, status=401)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid location data.'}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({'error': 'Invalid location data.'}, status=400)
    latitude, longitude = parse_sos_coordinates(payload.get('latitude'), payload.get('longitude'))
    if latitude is None or longitude is None:
        return JsonResponse({'error': 'Valid latitude and longitude are required.'}, status=400)

    event = get_active_sos_for_user(current_user)
    if event is None:
        return JsonResponse({'error': 'There is no active SOS.'}, status=409)

    updated_at = timezone.now()
    updated_count = SOSEvent.objects.filter(
        pk=event.pk,
        user=current_user,
        status=SOSEvent.STATUS_ACTIVE,
    ).update(
        latitude=latitude,
        longitude=longitude,
        location_updated_at=updated_at,
    )
    if not updated_count:
        return JsonResponse({'error': 'There is no active SOS.'}, status=409)
    return JsonResponse({
        'status': 'updated',
        'latitude': latitude,
        'longitude': longitude,
        'updated_at': updated_at.isoformat(),
    })


def SOSLocationStatus(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'GET is required.'}, status=405)

    current_user = request.session.get('username')
    if not current_user:
        return JsonResponse({'error': 'Authentication is required.'}, status=401)

    event = get_active_sos_for_user(current_user)
    if event is None:
        return JsonResponse({'error': 'There is no active SOS.'}, status=409)

    response = JsonResponse({
        'status': event.status,
        'latitude': event.latitude,
        'longitude': event.longitude,
        'updated_at': event.location_updated_at.isoformat() if event.location_updated_at else None,
    })
    response['Cache-Control'] = 'no-store, private'
    return response


def rotate_sos_live_location_share(event):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    try:
        share = event.live_location_share
    except SOSLiveLocationShare.DoesNotExist:
        share = SOSLiveLocationShare(sos_event=event)

    share.token_hash = token_hash
    share.expires_at = timezone.now() + SOS_LIVE_LOCATION_TTL
    share.revoked_at = None
    share.save()
    return token


def RetrySOSEmail(request):
    if request.method != 'POST':
        return redirect('SOSDashboard')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    event = get_active_sos_for_user(current_user)
    if event is None:
        messages.error(request, 'There is no active SOS to retry.')
        return redirect('SOSDashboard')

    sender_email, sender_password = get_panic_email_config()
    if not sender_email or not sender_password:
        messages.error(request, 'Email retry is unavailable until PANIC_EMAIL_USER and PANIC_EMAIL_PASS are configured.')
        return redirect('SOSDashboard')

    claimed_deliveries = []
    with transaction.atomic():
        event = SOSEvent.objects.select_for_update().filter(
            pk=event.pk,
            user=current_user,
            status=SOSEvent.STATUS_ACTIVE,
        ).first()
        if event is None:
            messages.error(request, 'There is no active SOS to retry.')
            return redirect('SOSDashboard')

        deliveries = SOSEmailDelivery.objects.filter(sos_event=event)
        if deliveries.filter(status=SOSEmailDelivery.STATUS_SENT).exists():
            messages.error(
                request,
                'Retry is blocked because a notification was already accepted; rotating its secure location link could invalidate it.',
            )
            return redirect('SOSDashboard')

        failed_deliveries = list(deliveries.filter(status=SOSEmailDelivery.STATUS_FAILED))
        failed_ids = [delivery.pk for delivery in failed_deliveries]
        if not failed_ids:
            messages.info(request, 'There are no failed notifications available to retry.')
            return redirect('SOSDashboard')
        if not all(is_sos_delivery_retry_safe(delivery.error_message) for delivery in failed_deliveries):
            messages.error(
                request,
                'Retry is blocked because a previous SMTP outcome may be ambiguous; retrying could send a duplicate alert.',
            )
            return redirect('SOSDashboard')

        claimed_count = SOSEmailDelivery.objects.filter(
            pk__in=failed_ids,
            status=SOSEmailDelivery.STATUS_FAILED,
        ).update(status=SOSEmailDelivery.STATUS_PENDING, error_message='')
        if not claimed_count:
            messages.info(request, 'These notifications are already being retried.')
            return redirect('SOSDashboard')

        claimed_deliveries = list(SOSEmailDelivery.objects.filter(
            pk__in=failed_ids,
            status=SOSEmailDelivery.STATUS_PENDING,
        ))
        share_token = None
        if build_sos_live_location_url('configuration-check'):
            share_token = rotate_sos_live_location_share(event)

    send_sos_emails_for_event(
        event,
        share_token=share_token,
        delivery_ids=[delivery.pk for delivery in claimed_deliveries],
    )
    messages.info(
        request,
        '{} failed notification(s) retried. Check their current status below; SMTP acceptance is not proof of receipt.'.format(
            len(claimed_deliveries)
        ),
    )
    return redirect('SOSDashboard')


def sos_public_poll_rate_limit(request, subject):
    rate_key = 'sos-live-location-poll:{}'.format(
        hashlib.sha256(subject.encode('utf-8')).hexdigest()
    )
    if cache.add(rate_key, 1, 60):
        return None

    try:
        request_count = cache.incr(rate_key)
    except ValueError:
        cache.add(rate_key, 1, 60)
        request_count = 1
    if request_count <= 30:
        return None

    response = sos_location_error_response(
        'Too many location requests. Please wait before retrying.',
        status=429,
    )
    response['Retry-After'] = '60'
    return response


def sos_location_error_response(message, status):
    response = JsonResponse({'error': message}, status=status)
    response['Cache-Control'] = 'no-store, private'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@csrf_exempt
def SOSLiveLocationData(request):
    if request.method != 'POST':
        return sos_location_error_response('POST is required.', status=405)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        limited = sos_public_poll_rate_limit(request, request.META.get('REMOTE_ADDR', ''))
        if limited is not None:
            return limited
        return sos_location_error_response('Invalid access token.', status=400)
    token = payload.get('token', '') if isinstance(payload, dict) else ''
    if not isinstance(token, str) or not token or len(token) > 128:
        limited = sos_public_poll_rate_limit(request, request.META.get('REMOTE_ADDR', ''))
        if limited is not None:
            return limited
        return sos_location_error_response('Invalid or expired sharing link.', status=404)

    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    limited = sos_public_poll_rate_limit(request, token_hash)
    if limited is not None:
        return limited

    share = SOSLiveLocationShare.objects.select_related('sos_event').filter(token_hash=token_hash).first()
    if share is None:
        return sos_location_error_response('Invalid or expired sharing link.', status=404)
    if not share.is_valid():
        return sos_location_error_response('This SOS location share has ended or expired.', status=410)

    event = share.sos_event
    updated_at = event.location_updated_at
    is_stale = updated_at is None or timezone.now() - updated_at > SOS_LIVE_LOCATION_STALE_AFTER
    response = JsonResponse({
        'status': 'stale' if is_stale else 'sharing',
        'latitude': event.latitude,
        'longitude': event.longitude,
        'updated_at': updated_at.isoformat() if updated_at is not None else None,
        'stale_after_seconds': int(SOS_LIVE_LOCATION_STALE_AFTER.total_seconds()),
    })
    response['Cache-Control'] = 'no-store, private'
    response['Referrer-Policy'] = 'no-referrer'
    return response


def SOSLiveLocationPage(request):
    if request.method != 'GET':
        return HttpResponse(status=405)
    response = render(request, 'SOSLiveLocation.html')
    response['Cache-Control'] = 'no-store, private'
    response['Referrer-Policy'] = 'no-referrer'
    return response


def EmergencyContacts(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    contacts = EmergencyContact.objects.filter(user=current_user).order_by('-is_primary', 'name')
    return render(request, 'EmergencyContacts.html', {'contacts': contacts, 'current_user': current_user})


def EmergencyContactAdd(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    if request.method == 'POST':
        name = (request.POST.get('name', '') or '').strip()
        phone_number = (request.POST.get('phone_number', '') or '').strip()
        email = (request.POST.get('email', '') or '').strip() or None
        relationship = (request.POST.get('relationship', '') or '').strip() or None
        is_primary = request.POST.get('is_primary') == 'on'
        is_active = request.POST.get('is_active') != 'off'
        return_to = (request.POST.get('return_to', '') or '').strip()

        contact = EmergencyContact(
            user=current_user,
            name=name,
            phone_number=phone_number,
            email=email,
            relationship=relationship,
            is_primary=is_primary,
            is_active=is_active,
        )

        try:
            contact.full_clean()
            contact.save()
            return redirect('Panic' if return_to == 'Panic' else 'EmergencyContacts')
        except ValidationError as exc:
            contacts = EmergencyContact.objects.filter(user=current_user).order_by('-is_primary', 'name')
            return render(request, 'EmergencyContacts.html', {
                'contacts': contacts,
                'current_user': current_user,
                'form_errors': exc.message_dict,
                'form_data': {
                    'name': name,
                    'phone_number': phone_number,
                    'email': email or '',
                    'relationship': relationship or '',
                    'is_primary': is_primary,
                    'is_active': is_active,
                },
                'show_form': True,
            })

    return render(request, 'EmergencyContacts.html', {'contacts': EmergencyContact.objects.filter(user=current_user).order_by('-is_primary', 'name'), 'current_user': current_user, 'show_form': True})


def EmergencyContactEdit(request, contact_id):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    contact = EmergencyContact.objects.filter(pk=contact_id, user=current_user).first()
    if contact is None:
        return redirect('EmergencyContacts')

    if request.method == 'POST':
        contact.name = (request.POST.get('name', '') or '').strip()
        contact.phone_number = (request.POST.get('phone_number', '') or '').strip()
        contact.email = (request.POST.get('email', '') or '').strip() or None
        contact.relationship = (request.POST.get('relationship', '') or '').strip() or None
        contact.is_primary = request.POST.get('is_primary') == 'on'
        contact.is_active = request.POST.get('is_active') != 'off'
        try:
            contact.full_clean()
            contact.save()
            return redirect('EmergencyContacts')
        except ValidationError as exc:
            return render(request, 'EmergencyContacts.html', {
                'contacts': EmergencyContact.objects.filter(user=current_user).order_by('-is_primary', 'name'),
                'current_user': current_user,
                'show_form': True,
                'edit_contact': contact,
                'form_errors': exc.message_dict,
            })

    return render(request, 'EmergencyContacts.html', {
        'contacts': EmergencyContact.objects.filter(user=current_user).order_by('-is_primary', 'name'),
        'current_user': current_user,
        'show_form': True,
        'edit_contact': contact,
    })


def EmergencyContactDelete(request, contact_id):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    contact = EmergencyContact.objects.filter(pk=contact_id, user=current_user).first()
    if contact is None:
        return redirect('EmergencyContacts')

    if request.method == 'POST':
        contact.delete()
        return redirect('EmergencyContacts')

    return render(request, 'EmergencyContactDelete.html', {'contact': contact, 'current_user': current_user})


def EmergencyContactSetPrimary(request, contact_id):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    contact = EmergencyContact.objects.filter(pk=contact_id, user=current_user).first()
    if contact is not None:
        contact.is_primary = True
        contact.is_active = True
        contact.save()
    return redirect('EmergencyContacts')


def EmergencyContactToggleActive(request, contact_id):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    contact = EmergencyContact.objects.filter(pk=contact_id, user=current_user).first()
    if contact is not None:
        contact.is_active = not contact.is_active
        if not contact.is_active and contact.is_primary:
            contact.is_primary = False
        contact.save()
    return redirect('EmergencyContacts')


def build_google_maps_directions_url(destination, origin_lat=None, origin_lng=None, travel_mode='driving'):
    cleaned_destination = (destination or '').strip()
    if not cleaned_destination:
        return ''

    api_key = (os.environ.get('GOOGLE_MAPS_EMBED_API_KEY', '') or '').strip()
    if not api_key:
        return ''

    destination_encoded = urllib.parse.quote_plus(cleaned_destination)
    params = [
        'key=' + api_key,
        'destination=' + destination_encoded,
        'mode=' + (travel_mode or 'driving'),
    ]
    if origin_lat is not None and origin_lng is not None:
        params.insert(1, 'origin=' + urllib.parse.quote_plus('{lat},{lng}'.format(lat=origin_lat, lng=origin_lng)))
    return 'https://www.google.com/maps/embed/v1/directions?' + '&'.join(params)


def Route(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    if request.method == 'GET':
        session_user = request.session.get('username')
        resume_journey = None
        journey_id = request.GET.get('journey_id')
        if session_user and journey_id:
            from .models import Journey
            resume_journey = Journey.objects.filter(
                pk=journey_id,
                user=session_user,
                status__in=(Journey.STATUS_PLANNED, Journey.STATUS_ACTIVE),
            ).first()
        return render(request, 'Route.html', {
            'route_map_url': '',
            'route_destination': '',
            'route_error': '',
            'google_maps_embed_api_key': (os.environ.get('GOOGLE_MAPS_EMBED_API_KEY', '') or '').strip(),
            'journey_contacts': EmergencyContact.objects.filter(
                user=session_user,
                is_active=True,
            ).exclude(email__isnull=True).exclude(email='') if session_user else EmergencyContact.objects.none(),
            'resume_journey': resume_journey,
        })

    return redirect('Route')


def RouteAction(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    if request.method == 'POST':
        destination = (request.POST.get('destination') or request.POST.get('t1') or '').strip()
        origin_lat = request.POST.get('origin_lat', '').strip()
        origin_lng = request.POST.get('origin_lng', '').strip()

        if not destination:
            return render(request, 'Route.html', {
                'route_map_url': '',
                'route_destination': '',
                'route_error': 'Please enter a destination to find a safe route.',
                'google_maps_embed_api_key': (os.environ.get('GOOGLE_MAPS_EMBED_API_KEY', '') or '').strip(),
            })

        origin_lat_value = float(origin_lat) if origin_lat else None
        origin_lng_value = float(origin_lng) if origin_lng else None

        if origin_lat_value is None or origin_lng_value is None:
            return render(request, 'Route.html', {
                'route_map_url': '',
                'route_destination': destination,
                'route_error': 'Unable to access your current location. Please allow location access to use Safe Places.',
                'google_maps_embed_api_key': (os.environ.get('GOOGLE_MAPS_EMBED_API_KEY', '') or '').strip(),
            })

        return render(request, 'Route.html', {
            'route_map_url': '',
            'route_destination': destination,
            'route_error': '',
            'google_maps_embed_api_key': (os.environ.get('GOOGLE_MAPS_EMBED_API_KEY', '') or '').strip(),
        })

    return redirect('Route')


def Heatmap(request):
    if request.method == 'GET':
        crime_categories = {
            'All Crimes Against Women': 'All Crimes Against Women',
            'Rape': 'Rape',
            'K&A': 'Kidnapping and Abduction',
            'DD': 'Dowry Deaths',
            'AoW': 'Assault on Women',
            'DV': 'Domestic Violence',
            'WT': 'Women Trafficking'
        }

        india_state_map = {
            'A & N ISLANDS': 'Andaman and Nicobar',
            'A & N Islands': 'Andaman and Nicobar',
            'ANDHRA PRADESH': 'Andhra Pradesh',
            'Andhra Pradesh': 'Andhra Pradesh',
            'ARUNACHAL PRADESH': 'Arunachal Pradesh',
            'Arunachal Pradesh': 'Arunachal Pradesh',
            'ASSAM': 'Assam',
            'Assam': 'Assam',
            'BIHAR': 'Bihar',
            'Bihar': 'Bihar',
            'CHANDIGARH': 'Chandigarh',
            'Chandigarh': 'Chandigarh',
            'CHHATTISGARH': 'Chhattisgarh',
            'Chhattisgarh': 'Chhattisgarh',
            'D & N HAVELI': 'Dadra and Nagar Haveli',
            'D&N Haveli': 'Dadra and Nagar Haveli',
            'DAMAN & DIU': 'Daman and Diu',
            'Daman & Diu': 'Daman and Diu',
            'DELHI UT': 'Delhi',
            'Delhi UT': 'Delhi',
            'GOA': 'Goa',
            'Goa': 'Goa',
            'GUJARAT': 'Gujarat',
            'Gujarat': 'Gujarat',
            'HARYANA': 'Haryana',
            'Haryana': 'Haryana',
            'HIMACHAL PRADESH': 'Himachal Pradesh',
            'Himachal Pradesh': 'Himachal Pradesh',
            'JAMMU & KASHMIR': 'Jammu and Kashmir',
            'Jammu & Kashmir': 'Jammu and Kashmir',
            'JHARKHAND': 'Jharkhand',
            'Jharkhand': 'Jharkhand',
            'KARNATAKA': 'Karnataka',
            'Karnataka': 'Karnataka',
            'KERALA': 'Kerala',
            'Kerala': 'Kerala',
            'LAKSHADWEEP': 'Lakshadweep',
            'Lakshadweep': 'Lakshadweep',
            'MADHYA PRADESH': 'Madhya Pradesh',
            'Madhya Pradesh': 'Madhya Pradesh',
            'MAHARASHTRA': 'Maharashtra',
            'Maharashtra': 'Maharashtra',
            'MANIPUR': 'Manipur',
            'Manipur': 'Manipur',
            'MEGHALAYA': 'Meghalaya',
            'Meghalaya': 'Meghalaya',
            'MIZORAM': 'Mizoram',
            'Mizoram': 'Mizoram',
            'NAGALAND': 'Nagaland',
            'Nagaland': 'Nagaland',
            'ODISHA': 'Odisha',
            'Odisha': 'Odisha',
            'PUNJAB': 'Punjab',
            'Punjab': 'Punjab',
            'RAJASTHAN': 'Rajasthan',
            'Rajasthan': 'Rajasthan',
            'SIKKIM': 'Sikkim',
            'Sikkim': 'Sikkim',
            'TAMIL NADU': 'Tamil Nadu',
            'Tamil Nadu': 'Tamil Nadu',
            'TELANGANA': 'Telangana',
            'Telangana': 'Telangana',
            'TRIPURA': 'Tripura',
            'Tripura': 'Tripura',
            'UTTAR PRADESH': 'Uttar Pradesh',
            'Uttar Pradesh': 'Uttar Pradesh',
            'UTTARAKHAND': 'Uttarakhand',
            'Uttarakhand': 'Uttarakhand',
            'WEST BENGAL': 'West Bengal',
            'West Bengal': 'West Bengal',
        }

        crime_columns = ['Rape', 'K&A', 'DD', 'AoW', 'AoM', 'DV', 'WT']
        active_data = []

        for _, row in dataset.iterrows():
            state_name = str(row.get('State', '')).strip()
            geo_state = india_state_map.get(state_name, state_name)
            if not geo_state:
                continue
            year_value = int(row.get('Year', 0))
            for crime_key, crime_label in crime_categories.items():
                if crime_key == 'All Crimes Against Women':
                    value = 0
                    for col in crime_columns:
                        value += int(row.get(col, 0) or 0)
                else:
                    value = int(row.get(crime_key, 0) or 0)
                active_data.append({
                    'state': state_name,
                    'geo_state': geo_state,
                    'year': year_value,
                    'crime': crime_key,
                    'crime_label': crime_label,
                    'value': int(value)
                })

        district_dataset = pd.read_csv('dstrCAW_2013.csv')
        district_data = []
        district_column_map = {
            'Rape': 'Rape',
            'K&A': 'Kidnapping and Abduction',
            'DD': 'Dowry Deaths',
            'AoW': 'Assault on women with intent to outrage her modesty',
            'AoM': 'Insult to modesty of Women',
            'DV': 'Cruelty by Husband or his Relatives',
            'WT': 'Importation of Girls',
        }

        for _, row in district_dataset.iterrows():
            state_name = str(row.get('STATE/UT', '')).strip()
            district_name = str(row.get('DISTRICT', '')).strip()
            year_value = int(row.get('Year', 0))
            for crime_key, crime_label in crime_categories.items():
                if crime_key == 'All Crimes Against Women':
                    value = 0
                    for col in district_column_map.values():
                        value += int(row.get(col, 0) or 0)
                else:
                    key_name = district_column_map.get(crime_key)
                    value = int(row.get(key_name, 0) or 0)
                district_data.append({
                    'state': state_name,
                    'district': district_name,
                    'year': year_value,
                    'crime': crime_key,
                    'crime_label': crime_label,
                    'value': int(value)
                })

        years = sorted(dataset['Year'].dropna().astype(int).unique().tolist())
        context = {
            'crime_options': json.dumps(list(crime_categories.keys())),
            'year_options': json.dumps(years),
            'map_data': json.dumps(active_data),
            'district_data': json.dumps(district_data),
            'crime_labels': json.dumps(crime_categories)
        }
        return render(request, 'Heatmap.html', context)


def TrainML(request):
    if request.method == 'GET':
        f = open('model/model_history.pckl', 'rb')
        hist = pickle.load(f)
        f.close()
        plt.figure(figsize=(5,3))
        plt.plot(hist['accuracy'], color = 'blue', label = 'Training Accuracy')
        plt.plot(hist['val_accuracy'], color = 'green', label = 'Validation Accuracy')
        plt.title('Neural Network Accuracy Graph')
        plt.xlabel('Epochs')
        plt.ylabel('Accuracy')
        plt.legend()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        plt.clf()
        plt.cla()
        context= {'data':'Neural Network Accuracy Graph', 'img': img_b64}
        return render(request, 'UserScreen.html', context)


def CrimePredict(request):
    if request.method == 'GET':
        global dataset, states
        output = '<tr><td><font size="3" color="black">Choose&nbsp;State</td><td><select name="t1">'
        for i in range(len(states)):
            output += '<option value="'+states[i]+'">'+states[i]+'</option>'
        output +="</select></td></tr>"
        context= {'data1':output}
        return render(request, 'CrimePredict.html', context)


def CrimePredictAction(request):
    if request.method == 'POST':
        global dataset
        areaname = request.POST.get('t1', False)
        crime = dataset.loc[dataset['State'] == areaname]
        crime = crime['Rape'].ravel()[0]
        output = "<font size=3 color=blue>Predicted Number of Crimes on Women in "+areaname+" = "+str(crime)+"</font>"
        context= {'data':output}
        return render(request, 'UserScreen.html', context)


def Panic(request):
    if request.method == 'GET':
        current_user, redirect_response = require_logged_in_user(request)
        if redirect_response is not None:
            return redirect_response

        active_event = get_active_sos_for_user(current_user)
        active_contacts = EmergencyContact.objects.filter(user=current_user, is_active=True)
        signup_contact = get_signup_emergency_contact_for_user(current_user)
        if signup_contact is not None and not active_contacts.exists():
            active_contacts = EmergencyContact.objects.filter(pk=signup_contact.pk)

        emergency_contact_count = active_contacts.count()
        email_contact_count = active_contacts.exclude(email__isnull=True).exclude(email='').count()
        delivery_summary = get_sos_delivery_summary(active_event)
        return render(request, 'Panic.html', {
            'current_user': current_user,
            'active_event': active_event,
            'active_contacts': active_contacts,
            'emergency_contact_count': emergency_contact_count,
            'email_contact_count': email_contact_count,
            'delivery_summary': delivery_summary,
            'public_location_link_configured': bool(build_sos_live_location_url('configuration-check')),
        })


def PanicAction(request):
    if request.method == 'POST':
        global username, email
        areaname = request.POST.get('t1', False)

        if not username or not email:
            context = {'data': 'Please login first before sending an emergency alert.'}
            return render(request, 'Panic.html', context)

        sender_email, sender_password = get_panic_email_config()
        message_text = username + " sent emergency message from location " + areaname + " to you."

        if not sender_email or not sender_password:
            context = {'data': 'Emergency alert recorded for ' + username + '. Email sending is not configured on this machine.'}
            return render(request, 'Panic.html', context)

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as connection:
                connection.login(sender_email, sender_password)
                connection.sendmail(from_addr=sender_email, to_addrs=[email], msg=message_text)
            context = {'data': 'Emergency message sent to registered E-Mail'}
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPException, OSError) as exc:
            context = {'data': 'Emergency alert recorded for ' + username + '. Email could not be sent because SMTP credentials are invalid or unavailable.'}

        return render(request, 'Panic.html', context)


def UserLoginAction(request):
    global username
    if request.method == 'POST':
        global username, email
        status = "none"
        users = request.POST.get('t1', False)
        password = request.POST.get('t2', False)
        ensure_register_table()
        with get_register_db_connection() as con:
            cur = con.cursor()
            cur.execute("SELECT username, password, email FROM register")
            rows = cur.fetchall()
            for row in rows:
                if row['username'] == users and row['password'] == password:
                    email = row['email']
                    username = users
                    request.session['username'] = users
                    request.session['email'] = row['email']
                    request.session['is_logged_in'] = True
                    status = "success"
                    break
        if status == 'success':
            context= {'data':'Welcome '+username}
            return render(request, "UserScreen.html", context)
        else:
            context= {'data':'Invalid username'}
            return render(request, 'UserLogin.html', context)


def RegisterAction(request):
    if request.method != 'POST':
        return redirect('Register')

    account = {
        'username': (request.POST.get('t1') or '').strip(),
        'password': request.POST.get('t2') or '',
        'contact': (request.POST.get('t3') or '').strip(),
        'email': (request.POST.get('t4') or '').strip(),
        'address': (request.POST.get('t5') or '').strip(),
    }
    if not all(account.values()):
        messages.error(request, 'Please complete all registration fields.')
        return redirect('Register')

    ensure_register_table()
    try:
        with get_register_db_connection() as connection:
            connection.execute(
                "INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                (
                    account['username'],
                    account['password'],
                    account['contact'],
                    account['email'],
                    account['address'],
                ),
            )
    except sqlite3.IntegrityError:
        with get_register_db_connection() as connection:
            existing_user = connection.execute(
                "SELECT 1 FROM register WHERE username = ?",
                (account['username'],),
            ).fetchone()
        if existing_user is not None:
            messages.error(
                request,
                'An account with this username already exists. Please log in or choose another username.',
            )
            return redirect('Register')
        logger.exception('Registration insert failed due to a database integrity constraint.')
        messages.error(request, 'We could not create your account. Please try again.')
        return redirect('Register')

    if account['email']:
        EmergencyContact.objects.get_or_create(
            user=account['username'],
            email=account['email'],
            defaults={
                'name': 'Emergency Contact',
                'phone_number': account['contact'] or '0000000000',
                'relationship': 'Registered emergency contact',
                'is_primary': True,
                'is_active': True,
            },
        )

    messages.success(
        request,
        'Account created successfully! Please log in with your new credentials.',
    )
    return redirect('UserLogin')


def UserLogin(request):
    if request.method == 'GET':
       return render(request, 'UserLogin.html', {})


def Dashboard(request):
    if request.method == 'GET':
        current_user = request.session.get('username') or globals().get('username')
        if not current_user:
            return redirect('UserLogin')
        context = {'data': 'Welcome ' + current_user}
        return render(request, 'UserScreen.html', context)


def Logout(request):
    if request.method == 'GET':
        request.session.flush()
        globals()['username'] = None
        globals()['email'] = None
        return redirect('UserLogin')


def index(request):
    if request.method == 'GET':
       return render(request, 'index.html', {})


def Register(request):
    if request.method == 'GET':
       return render(request, 'Register.html', {})
