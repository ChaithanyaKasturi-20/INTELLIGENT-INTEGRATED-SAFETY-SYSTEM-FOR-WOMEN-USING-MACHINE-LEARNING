from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.template import RequestContext
from django.contrib import messages
from django.http import HttpResponse
from django.conf import settings
from django.db.models import Q
import os
import io
import json
import base64
import sqlite3
import logging
import urllib.parse
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import pickle
import smtplib
from datetime import date, timedelta
import seaborn as sns
from django.utils import timezone

from .models import EmergencyContact, SafetyTimer as SafetyTimerModel, SOSEvent, SOSEmailDelivery, normalize_emergency_email

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
        send_sos_emails_for_event(active_event)

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
        event.mark_resolved()

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


def build_sos_email_body(sos_event, user_name):
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

    return '\n\n'.join(lines)


def get_sos_delivery_summary(event):
    if event is None:
        return {
            'sent': 0,
            'failed': 0,
            'pending': 0,
            'no_email': 0,
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

    return {
        'sent': deliveries.filter(status=SOSEmailDelivery.STATUS_SENT).count(),
        'failed': deliveries.filter(status=SOSEmailDelivery.STATUS_FAILED).count(),
        'pending': deliveries.filter(status=SOSEmailDelivery.STATUS_PENDING).count(),
        'no_email': no_email.count(),
        'eligible_contacts': eligible_contacts.count(),
    }


def send_sos_emails_for_event(sos_event):
    if sos_event is None:
        return False

    eligible_contacts = list(EmergencyContact.objects.filter(user=sos_event.user, is_active=True).exclude(email__isnull=True).exclude(email='').order_by('name'))
    if not eligible_contacts:
        fallback_contact = get_signup_emergency_contact_for_user(sos_event.user)
        if fallback_contact is not None:
            eligible_contacts = [fallback_contact]
        else:
            return True

    sender_email, sender_password = get_panic_email_config()

    if not sender_email or not sender_password:
        for contact in eligible_contacts:
            delivery, created = SOSEmailDelivery.objects.get_or_create(sos_event=sos_event, emergency_contact=contact)
            if delivery.status == SOSEmailDelivery.STATUS_SENT or delivery.status == SOSEmailDelivery.STATUS_FAILED:
                continue
            delivery.mark_failed('SMTP is not configured for this environment.')
        return True

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as connection:
            connection.login(sender_email, sender_password)
            for contact in eligible_contacts:
                delivery, created = SOSEmailDelivery.objects.get_or_create(sos_event=sos_event, emergency_contact=contact)
                if delivery.status == SOSEmailDelivery.STATUS_SENT or delivery.status == SOSEmailDelivery.STATUS_FAILED:
                    continue

                message = build_sos_email_body(sos_event, sos_event.user)
                msg = "From: {}\nTo: {}\nSubject: {}\n\n{}".format(
                    sender_email,
                    contact.email,
                    build_sos_email_subject(sos_event.user),
                    message,
                )
                try:
                    connection.sendmail(from_addr=sender_email, to_addrs=[contact.email], msg=msg)
                    delivery.mark_sent()
                except Exception as exc:
                    logger.exception('Failed to send SOS email to %s for event %s', contact.email, sos_event.pk)
                    delivery.mark_failed(str(exc))
    except Exception as exc:
        logger.exception('Unable to send SOS email batch for user %s', sos_event.user)
        for contact in eligible_contacts:
            delivery, created = SOSEmailDelivery.objects.get_or_create(sos_event=sos_event, emergency_contact=contact)
            if delivery.status == SOSEmailDelivery.STATUS_SENT or delivery.status == SOSEmailDelivery.STATUS_FAILED:
                continue
            delivery.mark_failed(str(exc))
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

    latitude_value = request.POST.get('latitude', '').strip()
    longitude_value = request.POST.get('longitude', '').strip()

    latitude = float(latitude_value) if latitude_value else None
    longitude = float(longitude_value) if longitude_value else None

    event = SOSEvent.objects.create(
        user=current_user,
        latitude=latitude,
        longitude=longitude,
        status=SOSEvent.STATUS_ACTIVE,
        activated_at=timezone.now(),
    )
    send_sos_emails_for_event(event)
    return redirect('SOSDashboard')


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
        event.mark_cancelled()
    elif status_value == SOSEvent.STATUS_RESOLVED:
        event.mark_resolved()

    return redirect('SOSDashboard')


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
        return render(request, 'Route.html', {
            'route_map_url': '',
            'route_destination': '',
            'route_error': '',
            'google_maps_embed_api_key': (os.environ.get('GOOGLE_MAPS_EMBED_API_KEY', '') or '').strip(),
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
                'route_error': 'Unable to access your current location. Please allow location access to use Safe Route.',
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
    if request.method == 'POST':
        global username
        username = request.POST.get('t1', False)
        password = request.POST.get('t2', False)
        contact = request.POST.get('t3', False)
        email = request.POST.get('t4', False)
        address = request.POST.get('t5', False)

        ensure_register_table()
        output = "none"
        with get_register_db_connection() as con:
            cur = con.cursor()
            cur.execute("SELECT username FROM register")
            rows = cur.fetchall()
            for row in rows:
                if row['username'] == username:
                    output = username+" Username already exists"
                    break
        if output == "none":
            with get_register_db_connection() as db_connection:
                db_cursor = db_connection.cursor()
                db_cursor.execute(
                    "INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                    (username, password, contact, email, address)
                )
                db_connection.commit()
                if db_cursor.rowcount == 1:
                    signup_email = (email or '').strip()
                    if signup_email:
                        EmergencyContact.objects.get_or_create(
                            user=username,
                            email=signup_email,
                            defaults={
                                'name': 'Emergency Contact',
                                'phone_number': (contact or '').strip() or '0000000000',
                                'relationship': 'Registered emergency contact',
                                'is_primary': True,
                                'is_active': True,
                            },
                        )
                    output = "Signup process completed. Login to perform Emergency activities"
        context= {'data':output}
        return render(request, 'Register.html', context)


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


