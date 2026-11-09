from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.contrib import messages
from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.utils.crypto import constant_time_compare
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F, Q
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
import time
import socket
import urllib.parse
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import pickle
import smtplib
from datetime import timedelta
from django.utils import timezone

from .models import CommunityReport, EmergencyContact, Journey, JourneyActivity, PersonalSafetyProfile, SafetyCheckIn, SafetyCheckInAttempt, SafetyTimer as SafetyTimerModel, SOSEvent, SOSEmailDelivery, SOSLiveLocationShare, SOSNotificationAttempt, normalize_emergency_email
from .heatmap_data import CRIME_CATEGORIES, build_state_heatmap_data
from .sos_acknowledgement import record_web_acknowledgement
from .sos_escalation import start_sos_escalation, stop_sos_escalation
from .sos_notifications import get_eligible_contacts
from .safety_checkins import (
    cancel_owner_checkins,
    confirm_owner_checkin,
    process_safety_checkins,
    schedule_checkin,
)

logger = logging.getLogger(__name__)

FOLLOWED_MODE_SESSION_KEY = 'safety_timer_mode'
FOLLOWED_MODE = 'FOLLOWED'

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


class _ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exception_type, exception_value, traceback):
        try:
            return super(_ClosingSQLiteConnection, self).__exit__(
                exception_type,
                exception_value,
                traceback,
            )
        finally:
            self.close()


def get_register_db_connection():
    database_name = connection.settings_dict.get('NAME') or ''
    test_database_name = connection.settings_dict.get('TEST', {}).get('NAME')
    is_test_database = (
        database_name == ':memory:'
        or database_name.startswith('file:memorydb_')
        or os.path.basename(database_name).lower().startswith('test_')
        or (test_database_name and database_name == test_database_name)
    )
    test_base_dir = getattr(settings, 'REGISTER_TEST_DB_DIR', None)
    if test_base_dir or is_test_database:
        if not test_base_dir:
            raise RuntimeError(
                'Tests using the register database must set '
                'REGISTER_TEST_DB_DIR to a per-test temporary directory.'
            )
        register_database_path = os.path.join(
            test_base_dir,
            'register-test.sqlite3',
        )
        development_database_path = os.path.join(settings.BASE_DIR, 'db.sqlite3')
        if os.path.normcase(os.path.realpath(register_database_path)) == os.path.normcase(
            os.path.realpath(development_database_path)
        ):
            raise RuntimeError(
                'Test register database cannot use the development database path.'
            )
    else:
        register_database_path = os.path.join(settings.BASE_DIR, 'db.sqlite3')

    conn = sqlite3.connect(register_database_path, factory=_ClosingSQLiteConnection)
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


def verify_and_upgrade_registered_password(username, raw_password, encoded_password):
    try:
        identify_hasher(encoded_password)
        return check_password(raw_password, encoded_password)
    except ValueError:
        password_verified = constant_time_compare(encoded_password or '', raw_password or '')

    if password_verified:
        upgraded_hash = make_password(raw_password)
        with get_register_db_connection() as connection:
            connection.execute(
                'UPDATE register SET password = ? WHERE username = ?',
                (upgraded_hash, username),
            )

    return password_verified


def require_logged_in_user(request):
    if request.session.get('is_logged_in') is not True:
        return None, redirect('UserLogin')

    current_user = request.session.get('username')
    if not isinstance(current_user, str) or not current_user.strip():
        return None, redirect('UserLogin')
    return current_user.strip(), None


def add_private_no_store_headers(response):
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, private'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def get_registered_account(user):
    ensure_register_table()
    with get_register_db_connection() as connection:
        row = connection.execute(
            'SELECT username, contact_no, email, address FROM register WHERE username = ?',
            (user,),
        ).fetchone()
    return {
        'username': user,
        'mobile_number': row['contact_no'] if row is not None else '',
        'email': row['email'] if row is not None else '',
        'address': row['address'] if row is not None else '',
    }


def MyAccount(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    profile, _ = PersonalSafetyProfile.objects.get_or_create(username=current_user)
    account = get_registered_account(current_user)
    form_errors = {}

    if request.method == 'POST':
        full_name = (request.POST.get('full_name', '') or '').strip()
        date_of_birth_text = (request.POST.get('date_of_birth', '') or '').strip()
        gender = (request.POST.get('gender', '') or '').strip()
        email = (request.POST.get('email', '') or '').strip()
        mobile_number = (request.POST.get('mobile_number', '') or '').strip()
        address = (request.POST.get('address', '') or '').strip()

        date_of_birth = None
        if date_of_birth_text:
            try:
                date_of_birth = timezone.datetime.strptime(date_of_birth_text, '%Y-%m-%d').date()
                if date_of_birth >= timezone.localdate():
                    form_errors['date_of_birth'] = ['Date of birth must be in the past.']
            except ValueError:
                form_errors['date_of_birth'] = ['Enter a valid date of birth.']

        if len(full_name) > 150:
            form_errors['full_name'] = ['Full name must be 150 characters or fewer.']
        if email:
            try:
                validate_email(email)
            except ValidationError:
                form_errors['email'] = ['Enter a valid email address.']
        else:
            form_errors['email'] = ['Email address is required.']
        if not re.fullmatch(r'^\+?[0-9\s()-]{8,20}$', mobile_number):
            form_errors['mobile_number'] = ['Enter a valid mobile number.']
        if len(address) > 500:
            form_errors['address'] = ['Address must be 500 characters or fewer.']

        medical_consent = request.POST.get('medical_consent') == 'on'
        medical_fields = {
            'blood_group': (request.POST.get('blood_group', '') or '').strip(),
            'medical_conditions': (request.POST.get('medical_conditions', '') or '').strip(),
            'allergies': (request.POST.get('allergies', '') or '').strip(),
            'medications': (request.POST.get('medications', '') or '').strip(),
            'medical_notes': (request.POST.get('medical_notes', '') or '').strip(),
            'disability_information': (request.POST.get('disability_information', '') or '').strip(),
        }
        if medical_consent is False:
            for field_name in list(medical_fields):
                medical_fields[field_name] = ''

        if not form_errors:
            with transaction.atomic():
                profile.full_name = full_name
                profile.date_of_birth = date_of_birth
                profile.gender = gender
                profile.medical_consent = medical_consent
                for field_name, value in medical_fields.items():
                    setattr(profile, field_name, value[:5000])
                profile.emergency_instructions = (request.POST.get('emergency_instructions', '') or '').strip()[:5000]
                profile.safety_notes = (request.POST.get('safety_notes', '') or '').strip()[:5000]
                profile.additional_emergency_information = (request.POST.get('additional_emergency_information', '') or '').strip()[:5000]
                profile.save()

                with get_register_db_connection() as connection:
                    connection.execute(
                        'UPDATE register SET contact_no = ?, email = ?, address = ? WHERE username = ?',
                        (mobile_number, normalize_emergency_email(email), address, current_user),
                    )
                    connection.commit()
                request.session['email'] = normalize_emergency_email(email)

            sync_signup_emergency_contact_for_user(current_user, normalize_emergency_email(email), mobile_number)
            messages.success(request, 'Your personal safety profile was saved.')
            return redirect('MyAccount')

    contacts = EmergencyContact.objects.filter(user=current_user).order_by('-is_primary', 'name')
    account = get_registered_account(current_user)
    return render(request, 'MyAccount.html', {
        'profile': profile,
        'account': account,
        'contacts': contacts,
        'form_errors': form_errors,
        'blood_group_choices': PersonalSafetyProfile.BLOOD_GROUP_CHOICES,
        'gender_choices': PersonalSafetyProfile.GENDER_CHOICES,
    })


def get_active_sos_for_user(user):
    return SOSEvent.objects.filter(user=user, status=SOSEvent.STATUS_ACTIVE).order_by('-activated_at').first()


def get_current_safety_timer_for_user(user):
    if not user:
        return None
    return SafetyTimerModel.objects.filter(user=user).order_by('-started_at').first()


def process_expired_safety_timer_for_user(user):
    if not user:
        return None
    process_safety_checkins(
        owner_kind='timer',
        user=user,
        dispatch_emails=False,
        allow_auto_sos=False,
    )
    return get_current_safety_timer_for_user(user)


def get_registered_emergency_email_for_user(user):
    if not user:
        return ''

    ensure_register_table()
    with get_register_db_connection() as conn:
        row = conn.execute(
            "SELECT email FROM register WHERE username = ?",
            (user,),
        ).fetchone()

    if row is None:
        return ''
    return (row['email'] or '').strip()


def get_sos_email_contacts_for_user(user):
    contacts = list(
        EmergencyContact.objects.filter(user=user, is_active=True)
        .exclude(email__isnull=True)
        .exclude(email='')
        .order_by('name')
    )
    if contacts:
        return contacts
    fallback_contact = get_signup_emergency_contact_for_user(user)
    return [fallback_contact] if fallback_contact is not None else []


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
    checkin = (
        timer.safety_checkins.order_by('-sequence').first()
        if timer is not None
        else None
    )
    device_heartbeat_stale = (
        timer is None
        or timer.last_device_seen_at is None
        or timer.last_device_seen_at < timezone.now() - timedelta(minutes=2)
    )
    return render(request, 'SafetyTimer.html', {
        'current_user': current_user,
        'timer': timer,
        'checkin': checkin,
        'device_heartbeat_stale': device_heartbeat_stale,
        'followed_mode': request.session.get(FOLLOWED_MODE_SESSION_KEY) == FOLLOWED_MODE,
        'duration_options': (5, 10, 15, 30),
        'checkin_intervals': (5, 10, 15),
        'grace_periods': (5, 10, 15),
        'guardian_contacts': EmergencyContact.objects.filter(
            user=current_user,
            is_active=True,
            email_consent=True,
            email_verified_at__isnull=False,
        ).exclude(email__isnull=True).exclude(email='').order_by('name'),
        'active_event': active_event,
        'emergency_contact_count': EmergencyContact.objects.filter(user=current_user, is_active=True).count(),
        'delivery_summary': get_sos_delivery_summary(active_event),
        'public_location_link_configured': bool(build_sos_live_location_url('configuration-check')),
        'public_location_link_diagnostic': get_sos_public_base_url_diagnostic(),
    })


def SafetyTimerAction(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    followed_mode = request.POST.get('mode') == FOLLOWED_MODE
    if followed_mode:
        duration_minutes = 5
    else:
        duration_value = request.POST.get('duration', '15')
        try:
            duration_minutes = int(duration_value)
        except (TypeError, ValueError):
            duration_minutes = 15
        if duration_minutes not in (5, 10, 15, 30):
            duration_minutes = 15
    interval_value = request.POST.get('checkin_interval_minutes', '15')
    grace_value = request.POST.get('grace_period_minutes', '15')
    try:
        interval_minutes = int(interval_value)
        grace_minutes = int(grace_value)
    except (TypeError, ValueError):
        interval_minutes = 15
        grace_minutes = 15
    if interval_minutes not in (5, 10, 15):
        interval_minutes = 15
    if grace_minutes not in (5, 10, 15):
        grace_minutes = 15

    guardian_enabled = request.POST.get('guardian_enabled') == 'true'
    contact_ids = request.POST.getlist('guardian_contact_ids')
    contacts = EmergencyContact.objects.filter(
        user=current_user,
        is_active=True,
        email_consent=True,
        email_verified_at__isnull=False,
        pk__in=contact_ids,
    ).exclude(email__isnull=True).exclude(email='')
    if guardian_enabled and (not contact_ids or contacts.count() != len(set(contact_ids))):
        messages.error(request, 'Select only active emergency contacts with verified, consented email addresses.')
        return redirect('SafetyTimer')

    now = timezone.now()
    with transaction.atomic():
        old_timers = SafetyTimerModel.objects.filter(
            user=current_user,
            status__in=(SafetyTimerModel.STATUS_ACTIVE, SafetyTimerModel.STATUS_EXPIRED),
        )
        for old_timer in old_timers:
            cancel_owner_checkins(old_timer, now)
        old_timers.update(
            status=SafetyTimerModel.STATUS_CANCELLED,
            completed_at=now,
            updated_at=now,
        )

        timer = SafetyTimerModel.objects.create(
            user=current_user,
            started_at=now,
            expires_at=now + timedelta(minutes=duration_minutes),
            duration=duration_minutes,
            checkin_interval_minutes=interval_minutes,
            grace_period_minutes=grace_minutes,
            guardian_enabled=guardian_enabled,
            auto_sos_on_unconfirmed=request.POST.get('auto_sos_on_unconfirmed') == 'true',
            share_location_with_contacts=request.POST.get('share_location_with_contacts') == 'true',
            location_status=(
                'PENDING'
                if request.POST.get('share_location_with_contacts') == 'true'
                else 'NOT_SHARED'
            ),
            status=SafetyTimerModel.STATUS_ACTIVE,
        )
        if guardian_enabled:
            timer.guardian_contacts.set(contacts)
        schedule_checkin(timer, now=now)
    if followed_mode:
        request.session[FOLLOWED_MODE_SESSION_KEY] = FOLLOWED_MODE
    else:
        request.session.pop(FOLLOWED_MODE_SESSION_KEY, None)
    return redirect('SafetyTimer')


def SafetyTimerSafe(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    timer = get_current_safety_timer_for_user(current_user)
    if timer is not None and timer.status in (
        SafetyTimerModel.STATUS_ACTIVE,
        SafetyTimerModel.STATUS_EXPIRED,
        SafetyTimerModel.STATUS_UNCONFIRMED,
    ):
        now = timezone.now()
        with transaction.atomic():
            changed = SafetyTimerModel.objects.filter(
                pk=timer.pk,
                status__in=(
                    SafetyTimerModel.STATUS_ACTIVE,
                    SafetyTimerModel.STATUS_EXPIRED,
                    SafetyTimerModel.STATUS_UNCONFIRMED,
                ),
            ).update(
                status=SafetyTimerModel.STATUS_COMPLETED,
                completed_at=now,
                updated_at=now,
            )
            if changed:
                confirm_owner_checkin(timer, now)
                cancel_owner_checkins(timer, now)

    event = get_active_sos_for_user(current_user)
    if event is not None:
        resolve_sos_event(event)
        messages.success(request, 'SOS resolved. Location updates and recipient access have ended.')

    request.session.pop(FOLLOWED_MODE_SESSION_KEY, None)
    return redirect('SafetyTimer')


def SafetyTimerCancel(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    timer = get_current_safety_timer_for_user(current_user)
    if timer is not None and timer.status in (
        SafetyTimerModel.STATUS_ACTIVE,
        SafetyTimerModel.STATUS_EXPIRED,
        SafetyTimerModel.STATUS_UNCONFIRMED,
    ):
        now = timezone.now()
        with transaction.atomic():
            changed = SafetyTimerModel.objects.filter(
                pk=timer.pk,
                status__in=(
                    SafetyTimerModel.STATUS_ACTIVE,
                    SafetyTimerModel.STATUS_EXPIRED,
                    SafetyTimerModel.STATUS_UNCONFIRMED,
                ),
            ).update(
                status=SafetyTimerModel.STATUS_CANCELLED,
                completed_at=now,
                updated_at=now,
            )
            if changed:
                cancel_owner_checkins(timer, now)

    request.session.pop(FOLLOWED_MODE_SESSION_KEY, None)
    return redirect('SafetyTimer')


def SafetyTimerExtend(request):
    if request.method != 'POST':
        return redirect('SafetyTimer')

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response
    timer = get_current_safety_timer_for_user(current_user)
    if timer is None:
        messages.error(request, 'There is no active or grace-period timer to extend.')
        return redirect('SafetyTimer')
    try:
        extension_minutes = int(request.POST.get('minutes', ''))
    except (TypeError, ValueError):
        extension_minutes = 0
    if extension_minutes not in (5, 10, 15, 30):
        messages.error(request, 'Choose a supported timer extension.')
        return redirect('SafetyTimer')

    now = timezone.now()
    with transaction.atomic():
        timer = SafetyTimerModel.objects.select_for_update().filter(
            pk=timer.pk,
            user=current_user,
            status__in=(SafetyTimerModel.STATUS_ACTIVE, SafetyTimerModel.STATUS_EXPIRED),
        ).first()
        if timer is None:
            messages.error(request, 'The timer is no longer active or in its grace period.')
            return redirect('SafetyTimer')

        current_checkin = timer.safety_checkins.filter(
            status__in=(
                SafetyCheckIn.STATUS_SCHEDULED,
                SafetyCheckIn.STATUS_DUE,
                SafetyCheckIn.STATUS_UNCONFIRMED,
            ),
        ).order_by('-sequence').first()
        if current_checkin and current_checkin.status == SafetyCheckIn.STATUS_UNCONFIRMED:
            messages.error(request, 'The check-in is already unconfirmed; confirm safety or cancel the timer.')
            return redirect('SafetyTimer')
        if current_checkin:
            cancelled = SafetyCheckIn.objects.filter(
                pk=current_checkin.pk,
                status__in=(SafetyCheckIn.STATUS_SCHEDULED, SafetyCheckIn.STATUS_DUE),
            ).update(
                status=SafetyCheckIn.STATUS_CANCELLED,
                updated_at=now,
            )
            if not cancelled:
                messages.error(request, 'The check-in changed while extending the timer. Refresh and retry.')
                return redirect('SafetyTimer')
            SafetyCheckInAttempt.objects.filter(
                checkin=current_checkin,
                status=SafetyCheckInAttempt.STATUS_QUEUED,
            ).update(
                status=SafetyCheckInAttempt.STATUS_SKIPPED,
                completed_at=now,
                error_message='The timer was extended before this reminder was presented.',
                updated_at=now,
            )

        updated = SafetyTimerModel.objects.filter(
            pk=timer.pk,
            status__in=(SafetyTimerModel.STATUS_ACTIVE, SafetyTimerModel.STATUS_EXPIRED),
        ).update(
            expires_at=now + timedelta(minutes=extension_minutes),
            duration=F('duration') + extension_minutes,
            status=SafetyTimerModel.STATUS_ACTIVE,
            completed_at=None,
            updated_at=now,
        )
        if not updated:
            messages.error(request, 'The timer changed while extending it. Refresh and retry.')
            return redirect('SafetyTimer')
        timer.refresh_from_db()
        cancel_owner_checkins(timer, now)
        schedule_checkin(timer, now=now)

    messages.success(request, 'Safety timer extended. A new check-in has been scheduled.')
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
    if event is None or event.pk is None:
        raise ValueError('A persisted SOS event is required.')

    with transaction.atomic():
        persisted_event = SOSEvent.objects.select_for_update().filter(pk=event.pk).first()
        if persisted_event is None:
            return None

        if persisted_event.status == SOSEvent.STATUS_ACTIVE:
            if status == SOSEvent.STATUS_CANCELLED:
                persisted_event.mark_cancelled()
            else:
                persisted_event.mark_resolved()

        stop_reason = (
            SOSEvent.STATUS_CANCELLED
            if persisted_event.status == SOSEvent.STATUS_CANCELLED
            else SOSEvent.STATUS_RESOLVED
        )
        stop_sos_escalation(persisted_event, stop_reason)
        try:
            persisted_event.live_location_share.revoke()
        except SOSLiveLocationShare.DoesNotExist:
            pass

    event.status = persisted_event.status
    return persisted_event


def build_sos_live_location_url(token):
    public_base_url = get_valid_sos_public_base_url()
    if not public_base_url:
        return ''

    page_url = public_base_url + reverse('SOSLiveLocationPage')
    return '{}#{}'.format(page_url, token) if page_url else ''


def get_sos_public_base_url_from_environment():
    load_local_env()
    if 'SOS_PUBLIC_BASE_URL' in os.environ:
        return (os.environ.get('SOS_PUBLIC_BASE_URL') or '').strip().rstrip('/')
    return (getattr(settings, 'SOS_PUBLIC_BASE_URL', '') or '').strip().rstrip('/')


def get_sos_public_base_url_diagnostic():
    public_base_url = get_sos_public_base_url_from_environment()
    if not public_base_url:
        return 'SOS_PUBLIC_BASE_URL is missing. Email alerts can still include the last saved coordinates.'
    if not get_valid_sos_public_base_url():
        return 'SOS_PUBLIC_BASE_URL is not a valid, publicly resolvable HTTPS origin. Email alerts can still include the last saved coordinates.'
    return ''


def get_valid_sos_public_base_url():
    public_base_url = get_sos_public_base_url_from_environment()
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


def build_sos_email_body(
    sos_event,
    user_name,
    live_location_url='',
    emergency_instructions='',
):
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
        if sos_event.location_updated_at is not None:
            location_time = timezone.localtime(sos_event.location_updated_at)
            lines.append(
                'Location last updated: {}'.format(
                    location_time.strftime('%Y-%m-%d %H:%M:%S %Z')
                )
            )
            location_age = timezone.now() - sos_event.location_updated_at
            if timedelta(0) <= location_age <= SOS_LIVE_LOCATION_STALE_AFTER:
                lines.append(
                    'This location update is recent, but GPS accuracy and current availability are not guaranteed.'
                )
            else:
                lines.append(
                    'This is the last known location and may be stale; it does not confirm the user is still there.'
                )
        else:
            lines.append(
                'The location timestamp is unavailable; treat these coordinates as potentially stale.'
            )
    else:
        lines.append('Location is currently unavailable.')

    if emergency_instructions:
        lines.append('User-provided emergency instructions: {}'.format(
            emergency_instructions.strip()
        ))

    if live_location_url:
        lines.extend([
            'Live location sharing link (expires in 24 hours and is revoked when the SOS ends):',
            live_location_url,
            'The live map updates only while the user’s device and browser remain online with location access enabled.',
        ])
    else:
        lines.append(
            'No secure public live-location link is included in this alert. '
            'Use the saved coordinates above if available.'
        )

    return '\n\n'.join(lines)


def get_sos_delivery_summary(event):
    summary = {
        'sent': 0,
        'failed': 0,
        'pending': 0,
        'no_email': 0,
        'unattempted': 0,
        'can_retry': False,
        'failures': [],
        'email_failures': [],
        'eligible_contacts': 0,
        'ineligible_email_contacts': 0,
        'email_accepted': 0,
        'email_delivered': 0,
        'email_failed': 0,
        'email_pending': 0,
        'email_uncertain': 0,
        'email_unattempted': 0,
        'source': 'none',
        'legacy_only': False,
        'workflow_attempt_count': 0,
        'queued': 0,
        'sending': 0,
        'accepted': 0,
        'delivered': 0,
        'no_answer': 0,
        'busy': 0,
        'uncertain': 0,
        'uncertain_outcomes': [],
        'acknowledged': 0,
        'cancelled': 0,
        'legacy_records': 0,
        'legacy_accepted': 0,
        'legacy_failed': 0,
        'legacy_pending': 0,
        'legacy_uncertain': 0,
    }
    if event is None:
        return summary

    contact_queryset = EmergencyContact.objects.filter(user=event.user, is_active=True)
    contacts_with_email_ids = set(
        contact_queryset.exclude(email__isnull=True)
        .exclude(email='')
        .values_list('pk', flat=True)
    )
    eligible_contact_ids = {
        contact.pk
        for contact in get_eligible_contacts(
            event,
            SOSNotificationAttempt.CHANNEL_EMAIL,
        )
    }
    summary['eligible_contacts'] = len(eligible_contact_ids)
    summary['ineligible_email_contacts'] = len(
        contacts_with_email_ids - eligible_contact_ids
    )
    summary['no_email'] = contact_queryset.filter(
        Q(email__isnull=True) | Q(email='')
    ).count()

    attempts = SOSNotificationAttempt.objects.filter(sos_event=event).order_by(
        'emergency_contact_id',
        'channel',
        '-attempt_number',
        '-created_at',
        '-pk',
    )
    latest_attempts = {}
    for attempt in attempts:
        key = (attempt.emergency_contact_id, attempt.channel)
        if key not in latest_attempts:
            latest_attempts[key] = attempt

    deliveries = SOSEmailDelivery.objects.filter(sos_event=event)
    summary['legacy_records'] = deliveries.count()
    summary['legacy_accepted'] = deliveries.filter(
        status=SOSEmailDelivery.STATUS_SENT
    ).count()

    if latest_attempts:
        summary['source'] = 'workflow'
        summary['workflow_attempt_count'] = len(latest_attempts)
        attempted_email_contacts = set()
        email_attempts = []
        for attempt in latest_attempts.values():
            status_key = attempt.status.lower()
            if status_key in summary:
                summary[status_key] += 1
            if attempt.channel == SOSNotificationAttempt.CHANNEL_EMAIL:
                attempted_email_contacts.add(attempt.emergency_contact_id)
                email_attempts.append(attempt)
            if attempt.status == SOSNotificationAttempt.STATUS_UNCERTAIN:
                summary['uncertain_outcomes'].append({
                    'reason': (
                        'The notification outcome is uncertain; it was not '
                        'counted as failed or delivered.'
                    ),
                })
            elif attempt.status in (
                SOSNotificationAttempt.STATUS_FAILED,
                SOSNotificationAttempt.STATUS_NO_ANSWER,
                SOSNotificationAttempt.STATUS_BUSY,
            ):
                failure = {
                    'contact_name': 'Emergency contact',
                    'reason': {
                        SOSNotificationAttempt.STATUS_FAILED: 'The notification failed.',
                        SOSNotificationAttempt.STATUS_NO_ANSWER: 'The call was not answered.',
                        SOSNotificationAttempt.STATUS_BUSY: 'The call channel was busy.',
                    }[attempt.status],
                }
                summary['failures'].append(failure)
                if attempt.channel == SOSNotificationAttempt.CHANNEL_EMAIL:
                    summary['email_failures'].append({
                        'contact_name': failure['contact_name'],
                        'reason': safe_sos_email_failure_reason(
                            attempt.error_message or attempt.error_code
                        ),
                    })

        summary['sent'] = summary['delivered']
        summary['pending'] = summary['queued'] + summary['sending']
        summary['email_accepted'] = sum(
            attempt.status == SOSNotificationAttempt.STATUS_ACCEPTED
            for attempt in email_attempts
        )
        summary['email_delivered'] = sum(
            attempt.status == SOSNotificationAttempt.STATUS_DELIVERED
            for attempt in email_attempts
        )
        summary['email_failed'] = sum(
            attempt.status == SOSNotificationAttempt.STATUS_FAILED
            for attempt in email_attempts
        )
        summary['email_pending'] = sum(
            attempt.status in (
                SOSNotificationAttempt.STATUS_QUEUED,
                SOSNotificationAttempt.STATUS_SENDING,
            )
            for attempt in email_attempts
        )
        summary['email_uncertain'] = sum(
            attempt.status == SOSNotificationAttempt.STATUS_UNCERTAIN
            for attempt in email_attempts
        )
        summary['unattempted'] = len(
            eligible_contact_ids - attempted_email_contacts
        )
        summary['email_unattempted'] = summary['unattempted']
        return summary

    if not summary['legacy_records']:
        summary['unattempted'] = len(eligible_contact_ids)
        summary['email_unattempted'] = summary['unattempted']
        return summary

    summary['source'] = 'legacy'
    summary['legacy_only'] = True
    summary['legacy_failed'] = deliveries.filter(
        status=SOSEmailDelivery.STATUS_FAILED
    ).count()
    summary['legacy_pending'] = deliveries.filter(
        status__in=(SOSEmailDelivery.STATUS_PENDING, SOSEmailDelivery.STATUS_SENDING)
    ).count()
    summary['legacy_uncertain'] = deliveries.filter(
        status=SOSEmailDelivery.STATUS_UNCERTAIN
    ).count()
    summary['email_accepted'] = summary['legacy_accepted']
    summary['email_failed'] = summary['legacy_failed']
    summary['email_pending'] = summary['legacy_pending']
    summary['email_uncertain'] = summary['legacy_uncertain']
    attempted_legacy_contacts = set(
        deliveries.values_list('emergency_contact_id', flat=True)
    )
    summary['unattempted'] = len(eligible_contact_ids - attempted_legacy_contacts)
    summary['email_unattempted'] = summary['unattempted']
    for delivery in deliveries.filter(status=SOSEmailDelivery.STATUS_FAILED):
        failure = {
            'contact_name': 'Emergency contact',
            'reason': 'The legacy email submission failed.',
        }
        summary.setdefault('legacy_failures', []).append(failure)
        summary['email_failures'].append(failure)
    return summary


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
    if 'outcome could not be confirmed' in message or 'delivery is unknown' in message:
        return 'The email outcome could not be confirmed; retry was blocked to prevent duplicate alerts.'
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
        'email sending is not configured',
        'sender credentials',
        'could not connect to the smtp server',
        'smtp server refused this recipient',
        'invalid email address',
        'invalid recipient',
        'smtp server rejected the message (response code',
    ))


def send_sos_emails_for_event(sos_event, share_token=None, delivery_ids=None):
    logger.warning(
        'Legacy SOS email delivery is disabled; use the SOS escalation service instead.'
    )
    return False


def SOSDashboard(request):
    return Panic(request)


def activate_sos_event_for_user(user, latitude=None, longitude=None):
    for _ in range(20):
        try:
            with transaction.atomic():
                active_event = get_active_sos_for_user(user)
                if active_event is not None:
                    return active_event, None, False

                event = SOSEvent.objects.create(
                    user=user,
                    latitude=latitude,
                    longitude=longitude,
                    location_updated_at=timezone.now() if latitude is not None else None,
                    status=SOSEvent.STATUS_ACTIVE,
                    activated_at=timezone.now(),
                )
                share_token = create_sos_live_location_share(event)
                return event, share_token, True
        except (IntegrityError, OperationalError):
            time.sleep(0.02)

    try:
        active_event = get_active_sos_for_user(user)
        if active_event is not None:
            return active_event, None, False
        return None, None, False
    except OperationalError:
        return None, None, False


def ActivateSOS(request):
    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
        return redirect_response

    if request.method != 'POST':
        return redirect('SOSDashboard')

    latitude, longitude = parse_sos_coordinates(
        request.POST.get('latitude', ''),
        request.POST.get('longitude', ''),
    )

    event, _, _ = activate_sos_event_for_user(current_user, latitude, longitude)
    if event is None:
        messages.error(request, 'SOS could not be activated. Please retry.')
        return redirect('SOSDashboard')

    try:
        start_sos_escalation(event)
    except Exception as exc:
        logger.error(
            'SOS escalation could not be started for event %s (error type: %s).',
            event.pk,
            type(exc).__name__,
        )
        messages.error(
            request,
            'SOS is active, but automatic contact escalation could not start. Please seek help directly.',
        )
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
        request.session.pop(FOLLOWED_MODE_SESSION_KEY, None)

    return redirect('SOSDashboard')


@csrf_exempt
def SOSWebAcknowledgement(request):
    if request.method != 'POST':
        response = JsonResponse({'error': 'POST is required.'}, status=405)
    elif request.content_type != 'application/json':
        response = JsonResponse({'error': 'A JSON request body is required.'}, status=415)
    else:
        try:
            content_length = int(request.META.get('CONTENT_LENGTH') or 0)
        except ValueError:
            content_length = -1
        if content_length < 0:
            response = JsonResponse({'error': 'Invalid request size.'}, status=400)
        elif content_length > 512:
            response = JsonResponse({'error': 'Request is too large.'}, status=413)
        else:
            try:
                body = request.body
                if len(body) > 512:
                    response = JsonResponse({'error': 'Request is too large.'}, status=413)
                else:
                    payload = json.loads(body.decode('utf-8'))
                    token = payload.get('token') if isinstance(payload, dict) else None
                    if not isinstance(token, str) or not token or len(token) > 128:
                        raise ValueError('A valid acknowledgement token is required.')
                    record_web_acknowledgement(token)
                    response = JsonResponse({'status': 'acknowledged'})
            except (UnicodeDecodeError, ValueError):
                response = JsonResponse(
                    {'error': 'This acknowledgement link is invalid, expired, or already used.'},
                    status=400,
                )

    response['Cache-Control'] = 'no-store, private'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def SOSLocationUpdate(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
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

    current_user, redirect_response = require_logged_in_user(request)
    if redirect_response is not None:
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

    try:
        start_sos_escalation(event)
    except Exception as exc:
        logger.error(
            'SOS escalation processing could not be started from the retry action for event %s (error type: %s).',
            event.pk,
            type(exc).__name__,
        )
        messages.error(request, 'SOS notifications could not be processed. The scheduled escalation will retry safely.')
        return redirect('SOSDashboard')

    messages.info(request, 'SOS notification retries are handled by the escalation policy; ambiguous attempts are not resent.')
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
        alternative_phone_number = (request.POST.get('alternative_phone_number', '') or '').strip()
        email = (request.POST.get('email', '') or '').strip() or None
        relationship = (request.POST.get('relationship', '') or '').strip() or None
        is_primary = request.POST.get('is_primary') == 'on'
        is_active = request.POST.get('is_active') != 'off'
        return_to = (request.POST.get('return_to', '') or '').strip()

        contact = EmergencyContact(
            user=current_user,
            name=name,
            phone_number=phone_number,
            alternative_phone_number=alternative_phone_number,
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
                    'alternative_phone_number': alternative_phone_number,
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
        contact.alternative_phone_number = (request.POST.get('alternative_phone_number', '') or '').strip()
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
                email_consent=True,
                email_verified_at__isnull=False,
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
        heatmap_data = build_state_heatmap_data(dataset)
        context = {
            'crime_options': json.dumps(list(CRIME_CATEGORIES.keys())),
            'year_options': json.dumps(heatmap_data['years']),
            'map_data': json.dumps(heatmap_data['records']),
            'crime_labels': json.dumps(CRIME_CATEGORIES),
            'geographic_levels': json.dumps(heatmap_data['geographic_levels']),
            'unmatched_geo_records': heatmap_data['unmatched_records'],
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
        context = {'data': 'Neural Network Accuracy Graph', 'img': img_b64}
        return render(request, 'TrainML.html', context)


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
        areaname = (request.POST.get('t1') or '').strip()
        crime = dataset.loc[dataset['State'] == areaname, 'Rape']
        if not areaname or crime.empty:
            context = {'prediction_error': 'Choose a valid state from the available options.'}
            return render(request, 'UserScreen.html', context)

        context = {
            'prediction_state': areaname,
            'prediction_value': crime.iloc[0],
        }
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
            'public_location_link_diagnostic': get_sos_public_base_url_diagnostic(),
        })


def PanicAction(request):
    if request.method == 'POST':
        current_user, redirect_response = require_logged_in_user(request)
        if redirect_response is not None:
            return redirect_response

        return render(request, 'Panic.html', {
            'data': (
                'The legacy email alert action is disabled. Activate SOS to start '
                'the current mock-only notification workflow.'
            ),
        })


def UserLoginAction(request):
    if request.method == 'POST':
        status = "none"
        users = request.POST.get('t1', False)
        password = request.POST.get('t2', False)
        ensure_register_table()
        with get_register_db_connection() as con:
            row = con.execute(
                "SELECT username, password, email FROM register WHERE username = ?",
                (users,),
            ).fetchone()
            if row is not None and verify_and_upgrade_registered_password(
                row['username'], password, row['password'],
            ):
                request.session.flush()
                request.session['username'] = users
                request.session['email'] = row['email']
                request.session['is_logged_in'] = True
                status = "success"
        if status == 'success':
            return redirect('index')
        else:
            context= {'data':'Invalid username'}
            context['force_guest_navigation'] = True
            return add_private_no_store_headers(render(request, 'UserLogin.html', context))


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
                    make_password(account['password']),
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
        return add_private_no_store_headers(render(
            request,
            'UserLogin.html',
            {'force_guest_navigation': True},
        ))


def Dashboard(request):
    if request.method == 'GET':
        return redirect('index')


def Logout(request):
    if request.method in ('GET', 'POST'):
        request.session.flush()
        response = redirect('index')
        return add_private_no_store_headers(response)


def index(request):
    if request.method in ('GET', 'HEAD'):
        current_user, _ = require_logged_in_user(request)
        context = {'dashboard_user': current_user}

        if current_user is not None:
            context.update({
                'emergency_contact_count': EmergencyContact.objects.filter(user=current_user).count(),
                'journey_count': Journey.objects.filter(user=current_user).count(),
                'community_report_count': CommunityReport.objects.filter(reporter=current_user).count(),
                'has_active_sos': SOSEvent.objects.filter(
                    user=current_user,
                    status=SOSEvent.STATUS_ACTIVE,
                ).exists(),
                'active_safety_timer': SafetyTimerModel.objects.filter(
                    user=current_user,
                    status=SafetyTimerModel.STATUS_ACTIVE,
                    expires_at__gt=timezone.now(),
                ).order_by('-started_at').first(),
            })

            recent_activities = []
            for activity in JourneyActivity.objects.filter(
                journey__user=current_user,
            ).select_related('journey').order_by('-occurred_at')[:5]:
                recent_activities.append({
                    'title': 'Journey: {}'.format(activity.get_action_display()),
                    'occurred_at': activity.occurred_at,
                    'status': '',
                })
            for event in SOSEvent.objects.filter(user=current_user).order_by('-activated_at')[:5]:
                recent_activities.append({
                    'title': 'SOS alert',
                    'occurred_at': event.activated_at,
                    'status': event.get_status_display(),
                })
            for timer in SafetyTimerModel.objects.filter(user=current_user).order_by('-started_at')[:5]:
                recent_activities.append({
                    'title': 'Safety timer',
                    'occurred_at': timer.started_at,
                    'status': timer.get_status_display(),
                })
            for contact in EmergencyContact.objects.filter(user=current_user).order_by('-created_at')[:5]:
                recent_activities.append({
                    'title': 'Emergency contact added',
                    'occurred_at': contact.created_at,
                    'status': '',
                })
            for report in CommunityReport.objects.filter(reporter=current_user).order_by('-created_at')[:5]:
                recent_activities.append({
                    'title': 'Community report submitted',
                    'occurred_at': report.created_at,
                    'status': report.get_status_display(),
                })

            context['recent_activities'] = sorted(
                recent_activities,
                key=lambda activity: activity['occurred_at'],
                reverse=True,
            )[:5]

        return render(request, 'index.html', context)


def Register(request):
    if request.method == 'GET':
        return add_private_no_store_headers(render(
            request,
            'Register.html',
            {'force_guest_navigation': True},
        ))
