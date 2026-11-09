import re
import os
import uuid
import warnings

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def validate_community_evidence(upload):
    max_bytes = 5 * 1024 * 1024
    if upload.size > max_bytes:
        raise ValidationError('Evidence images must be 5 MB or smaller.')

    try:
        from PIL import Image
        upload.seek(0)
        image = Image.open(upload)
        if image.format not in ('JPEG', 'PNG'):
            raise ValidationError('Evidence must be a JPEG or PNG image.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image.verify()
    except ValidationError:
        raise
    except Exception:
        raise ValidationError('Evidence must be a valid JPEG or PNG image.')
    finally:
        upload.seek(0)


def community_evidence_upload_path(instance, filename):
    extension = os.path.splitext(filename)[1].lower()
    if extension not in ('.jpg', '.jpeg', '.png'):
        extension = '.jpg'
    return 'community-reports/{}/{}'.format(uuid.uuid4().hex, uuid.uuid4().hex + extension)


def normalize_emergency_email(email_value):
    cleaned_email = (email_value or '').strip()
    if not cleaned_email:
        return ''

    lowered = cleaned_email.lower()
    if 'example.com' in lowered or lowered.endswith('@example.com'):
        return ''
    return cleaned_email


class EmergencyContact(models.Model):
    user = models.CharField(max_length=150, db_index=True)
    name = models.CharField(max_length=120)
    phone_number = models.CharField(max_length=20)
    alternative_phone_number = models.CharField(max_length=20, blank=True, default='')
    email = models.EmailField(blank=True, null=True)
    relationship = models.CharField(max_length=80, blank=True, null=True)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    email_consent = models.BooleanField(default=False)
    sms_consent = models.BooleanField(default=False)
    voice_consent = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    consent_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-is_primary', 'name']

    def clean(self):
        super().clean()
        cleaned_name = (self.name or '').strip()
        if not cleaned_name:
            raise ValidationError({'name': 'Name is required.'})
        if len(cleaned_name) > 120:
            raise ValidationError({'name': 'Name must be shorter than 120 characters.'})

        cleaned_phone = (self.phone_number or '').strip()
        if not cleaned_phone:
            raise ValidationError({'phone_number': 'Phone number is required.'})
        if not re.fullmatch(r'^\+?[0-9\s()-]{8,20}$', cleaned_phone):
            raise ValidationError({'phone_number': 'Enter a valid phone number.'})

        cleaned_alternative_phone = (self.alternative_phone_number or '').strip()
        if cleaned_alternative_phone and not re.fullmatch(r'^\+?[0-9\s()-]{8,20}$', cleaned_alternative_phone):
            raise ValidationError({'alternative_phone_number': 'Enter a valid alternative phone number.'})

        cleaned_email = (self.email or '').strip()
        if cleaned_email:
            normalized_email = normalize_emergency_email(cleaned_email)
            if not normalized_email:
                raise ValidationError({'email': 'Use a real email address for an emergency contact.'})
            if not re.fullmatch(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', normalized_email):
                raise ValidationError({'email': 'Enter a valid email address.'})

        cleaned_relationship = (self.relationship or '').strip()
        if cleaned_relationship and len(cleaned_relationship) > 80:
            raise ValidationError({'relationship': 'Relationship must be shorter than 80 characters.'})

    def save(self, *args, **kwargs):
        self.name = (self.name or '').strip()
        self.phone_number = (self.phone_number or '').strip()
        self.alternative_phone_number = (self.alternative_phone_number or '').strip()
        self.email = (self.email or '').strip() or None
        self.relationship = (self.relationship or '').strip() or None
        self.full_clean()

        update_fields = kwargs.get('update_fields')
        fields_to_update = set(update_fields) if update_fields is not None else None
        previous_values = {}
        if self.pk is not None:
            fields_to_check = []
            if fields_to_update is None or 'email' in fields_to_update:
                fields_to_check.append('email')
            if fields_to_update is None or 'phone_number' in fields_to_update:
                fields_to_check.append('phone_number')
            if fields_to_check:
                previous_values = type(self).objects.filter(pk=self.pk).values(
                    *fields_to_check
                ).first() or {}

        if 'email' in previous_values and previous_values['email'] != self.email:
            self.email_consent = False
            self.email_verified_at = None
            self.consent_updated_at = timezone.now()
            if fields_to_update is not None:
                fields_to_update.update((
                    'email_consent',
                    'email_verified_at',
                    'consent_updated_at',
                ))

        if (
            'phone_number' in previous_values
            and previous_values['phone_number'] != self.phone_number
        ):
            self.sms_consent = False
            self.voice_consent = False
            self.phone_verified_at = None
            self.consent_updated_at = timezone.now()
            if fields_to_update is not None:
                fields_to_update.update((
                    'sms_consent',
                    'voice_consent',
                    'phone_verified_at',
                    'consent_updated_at',
                ))

        if fields_to_update is not None:
            kwargs['update_fields'] = tuple(fields_to_update)

        if self.is_primary:
            self.is_active = True
            EmergencyContact.objects.filter(user=self.user, is_primary=True).exclude(pk=self.pk).update(is_primary=False)

        super(EmergencyContact, self).save(*args, **kwargs)

    def __str__(self):
        return '{} ({})'.format(self.name, self.phone_number)


class SOSEvent(models.Model):
    STATUS_ACTIVE = 'ACTIVE'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_RESOLVED = 'RESOLVED'
    STATUS_CHOICES = (
        (STATUS_ACTIVE, 'ACTIVE'),
        (STATUS_CANCELLED, 'CANCELLED'),
        (STATUS_RESOLVED, 'RESOLVED'),
    )

    user = models.CharField(max_length=150, db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-activated_at']

    def mark_cancelled(self):
        self.status = self.STATUS_CANCELLED
        self.cancelled_at = timezone.now()
        self.resolved_at = None
        self.save(update_fields=['status', 'cancelled_at', 'resolved_at'])

    def mark_resolved(self):
        self.status = self.STATUS_RESOLVED
        self.resolved_at = timezone.now()
        self.cancelled_at = None
        self.save(update_fields=['status', 'cancelled_at', 'resolved_at'])

    def __str__(self):
        return '{} - {}'.format(self.user, self.status)


class SOSLiveLocationShare(models.Model):
    sos_event = models.OneToOneField(
        SOSEvent,
        on_delete=models.CASCADE,
        related_name='live_location_share',
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_valid(self):
        return (
            self.revoked_at is None
            and self.expires_at > timezone.now()
            and self.sos_event.status == SOSEvent.STATUS_ACTIVE
        )

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=['revoked_at', 'updated_at'])

    def __str__(self):
        return 'Live location share for SOS {}'.format(self.sos_event_id)


class SOSEmailDelivery(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SENDING = 'sending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_UNCERTAIN = 'uncertain'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'pending'),
        (STATUS_SENDING, 'sending'),
        (STATUS_SENT, 'sent'),
        (STATUS_FAILED, 'failed'),
        (STATUS_UNCERTAIN, 'uncertain'),
    )

    sos_event = models.ForeignKey('SOSEvent', on_delete=models.CASCADE, related_name='email_deliveries')
    emergency_contact = models.ForeignKey('EmergencyContact', on_delete=models.CASCADE, related_name='sos_email_deliveries')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('sos_event', 'emergency_contact')

    def mark_sent(self):
        self.status = self.STATUS_SENT
        self.sent_at = timezone.now()
        self.failed_at = None
        self.error_message = ''
        self.save(update_fields=['status', 'sent_at', 'failed_at', 'error_message'])

    def mark_failed(self, error_message=''):
        self.status = self.STATUS_FAILED
        self.failed_at = timezone.now()
        self.sent_at = None
        self.error_message = (error_message or '')[:500]
        self.save(update_fields=['status', 'failed_at', 'sent_at', 'error_message'])

    def mark_uncertain(self, error_message=''):
        self.status = self.STATUS_UNCERTAIN
        self.failed_at = timezone.now()
        self.sent_at = None
        self.error_message = (error_message or '')[:500]
        self.save(update_fields=['status', 'failed_at', 'sent_at', 'error_message', 'updated_at'])

    def __str__(self):
        return '{} -> {} ({})'.format(self.emergency_contact.name, self.status, self.sos_event_id)


class SafetyTimer(models.Model):
    STATUS_ACTIVE = 'ACTIVE'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_EXPIRED = 'EXPIRED'
    STATUS_UNCONFIRMED = 'UNCONFIRMED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (STATUS_ACTIVE, 'ACTIVE'),
        (STATUS_COMPLETED, 'COMPLETED'),
        (STATUS_EXPIRED, 'EXPIRED'),
        (STATUS_UNCONFIRMED, 'UNCONFIRMED'),
        (STATUS_CANCELLED, 'CANCELLED'),
    )

    user = models.CharField(max_length=150, db_index=True)
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    duration = models.PositiveIntegerField(default=15)
    checkin_interval_minutes = models.PositiveSmallIntegerField(default=15)
    grace_period_minutes = models.PositiveSmallIntegerField(default=15)
    reminder_retry_minutes = models.PositiveSmallIntegerField(default=5)
    max_reminder_retries = models.PositiveSmallIntegerField(default=1)
    guardian_enabled = models.BooleanField(default=False)
    auto_sos_on_unconfirmed = models.BooleanField(default=False)
    auto_sos_delay_minutes = models.PositiveSmallIntegerField(default=15)
    share_location_with_contacts = models.BooleanField(default=False)
    guardian_contacts = models.ManyToManyField(
        EmergencyContact,
        blank=True,
        related_name='guarded_safety_timers',
    )
    location_status = models.CharField(
        max_length=20,
        choices=(
            ('PENDING', 'Awaiting foreground location update'),
            ('NOT_SHARED', 'Not shared'),
            ('AVAILABLE', 'Available'),
            ('UNAVAILABLE', 'Unavailable'),
        ),
        default='NOT_SHARED',
    )
    last_device_seen_at = models.DateTimeField(null=True, blank=True)
    last_known_latitude = models.FloatField(null=True, blank=True)
    last_known_longitude = models.FloatField(null=True, blank=True)
    last_known_accuracy_meters = models.FloatField(null=True, blank=True)
    last_location_updated_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return '{} ({})'.format(self.user, self.status)

    def mark_completed(self):
        if self.status in (self.STATUS_ACTIVE, self.STATUS_EXPIRED):
            self.status = self.STATUS_COMPLETED
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_cancelled(self):
        if self.status == self.STATUS_ACTIVE:
            self.status = self.STATUS_CANCELLED
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_expired(self):
        if self.status == self.STATUS_ACTIVE:
            self.status = self.STATUS_EXPIRED
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at', 'updated_at'])


class Journey(models.Model):
    STATUS_PLANNED = 'PLANNED'
    STATUS_ACTIVE = 'ACTIVE'
    STATUS_OVERDUE = 'OVERDUE'
    STATUS_UNCONFIRMED = 'UNCONFIRMED'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (STATUS_PLANNED, 'Planned'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_OVERDUE, 'Overdue'),
        (STATUS_UNCONFIRMED, 'Unconfirmed'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_CANCELLED, 'Cancelled'),
    )

    user = models.CharField(max_length=150, db_index=True)
    origin_label = models.CharField(max_length=255)
    origin_latitude = models.FloatField()
    origin_longitude = models.FloatField()
    destination_label = models.CharField(max_length=255)
    destination_latitude = models.FloatField()
    destination_longitude = models.FloatField()
    distance_meters = models.PositiveIntegerField()
    estimated_duration_seconds = models.PositiveIntegerField()
    planned_departure = models.DateTimeField()
    expected_arrival = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PLANNED, db_index=True)
    guardian_enabled = models.BooleanField(default=False)
    guardian_contacts = models.ManyToManyField(EmergencyContact, blank=True, related_name='guarded_journeys')
    checkin_interval_minutes = models.PositiveSmallIntegerField(default=15)
    grace_period_minutes = models.PositiveSmallIntegerField(default=15)
    reminder_retry_minutes = models.PositiveSmallIntegerField(default=5)
    max_reminder_retries = models.PositiveSmallIntegerField(default=1)
    auto_sos_on_unconfirmed = models.BooleanField(default=False)
    auto_sos_delay_minutes = models.PositiveSmallIntegerField(default=15)
    share_location_with_contacts = models.BooleanField(default=False)
    location_status = models.CharField(
        max_length=20,
        choices=(
            ('PENDING', 'Awaiting foreground location update'),
            ('NOT_SHARED', 'Not shared'),
            ('AVAILABLE', 'Available'),
            ('UNAVAILABLE', 'Unavailable'),
        ),
        default='NOT_SHARED',
    )
    last_device_seen_at = models.DateTimeField(null=True, blank=True)
    last_known_latitude = models.FloatField(null=True, blank=True)
    last_known_longitude = models.FloatField(null=True, blank=True)
    last_known_accuracy_meters = models.FloatField(null=True, blank=True)
    last_location_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return '{}: {} to {} ({})'.format(self.user, self.origin_label, self.destination_label, self.status)


class JourneyActivity(models.Model):
    ACTION_PLANNED = 'PLANNED'
    ACTION_STARTED = 'STARTED'
    ACTION_CHECKED_IN = 'CHECKED_IN'
    ACTION_EXTENDED = 'EXTENDED'
    ACTION_OVERDUE = 'OVERDUE'
    ACTION_UNCONFIRMED = 'UNCONFIRMED'
    ACTION_ENDED = 'ENDED'
    ACTION_CANCELLED = 'CANCELLED'
    ACTION_CHOICES = (
        (ACTION_PLANNED, 'Planned'),
        (ACTION_STARTED, 'Started'),
        (ACTION_CHECKED_IN, 'Checked in safe'),
        (ACTION_EXTENDED, 'Deadline extended'),
        (ACTION_OVERDUE, 'Check-in overdue'),
        (ACTION_UNCONFIRMED, 'Check-in unconfirmed'),
        (ACTION_ENDED, 'Journey ended'),
        (ACTION_CANCELLED, 'Journey cancelled'),
    )

    journey = models.ForeignKey(Journey, on_delete=models.CASCADE, related_name='activities')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    occurred_at = models.DateTimeField(default=timezone.now)
    details = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['occurred_at', 'pk']

    def __str__(self):
        return '{}: {}'.format(self.journey_id, self.action)


class JourneyCheckInAlert(models.Model):
    STATUS_PENDING = 'PENDING'
    STATUS_SENT = 'SENT'
    STATUS_FAILED = 'FAILED'
    STATUS_NO_RECIPIENTS = 'NO_RECIPIENTS'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_NO_RECIPIENTS, 'No eligible contacts'),
    )

    journey = models.ForeignKey(Journey, on_delete=models.CASCADE, related_name='checkin_alerts')
    deadline_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-deadline_at']
        unique_together = ('journey', 'deadline_at')

    def __str__(self):
        return '{} overdue at {}'.format(self.journey_id, self.deadline_at)


class PersonalSafetyProfile(models.Model):
    BLOOD_GROUP_CHOICES = (
        ('', 'Not specified'),
        ('A+', 'A+'), ('A-', 'A-'), ('B+', 'B+'), ('B-', 'B-'),
        ('AB+', 'AB+'), ('AB-', 'AB-'), ('O+', 'O+'), ('O-', 'O-'),
    )
    GENDER_CHOICES = (
        ('', 'Prefer not to say'),
        ('female', 'Female'),
        ('male', 'Male'),
        ('non_binary', 'Non-binary'),
        ('other', 'Other'),
    )

    username = models.CharField(max_length=150, unique=True, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, blank=True)
    medical_consent = models.BooleanField(default=False)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUP_CHOICES, blank=True)
    medical_conditions = models.TextField(blank=True)
    allergies = models.TextField(blank=True)
    medications = models.TextField(blank=True)
    medical_notes = models.TextField(blank=True)
    disability_information = models.TextField(blank=True)
    emergency_instructions = models.TextField(blank=True)
    safety_notes = models.TextField(blank=True)
    additional_emergency_information = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def age(self):
        if self.date_of_birth is None:
            return None
        today = timezone.localdate()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    def __str__(self):
        return 'Personal Safety Profile for {}'.format(self.username)


class JourneyCheckInDelivery(models.Model):
    STATUS_PENDING = 'PENDING'
    STATUS_SENDING = 'SENDING'
    STATUS_SENT = 'SENT'
    STATUS_FAILED = 'FAILED'
    STATUS_UNCERTAIN = 'UNCERTAIN'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENDING, 'Sending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_UNCERTAIN, 'Uncertain'),
    )

    alert = models.ForeignKey(JourneyCheckInAlert, on_delete=models.CASCADE, related_name='deliveries')
    contact = models.ForeignKey(EmergencyContact, on_delete=models.SET_NULL, null=True, blank=True)
    recipient_email = models.EmailField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['pk']
        unique_together = ('alert', 'recipient_email')

    def __str__(self):
        return '{} -> {} ({})'.format(self.alert_id, self.recipient_email, self.status)


class SafetyCheckIn(models.Model):
    STATUS_SCHEDULED = 'SCHEDULED'
    STATUS_DUE = 'DUE'
    STATUS_UNCONFIRMED = 'UNCONFIRMED'
    STATUS_CONFIRMED = 'CONFIRMED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_DUE, 'Check-in due'),
        (STATUS_UNCONFIRMED, 'Unconfirmed'),
        (STATUS_CONFIRMED, 'Confirmed safe'),
        (STATUS_CANCELLED, 'Cancelled'),
    )

    journey = models.ForeignKey(
        Journey,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='safety_checkins',
    )
    safety_timer = models.ForeignKey(
        SafetyTimer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='safety_checkins',
    )
    sequence = models.PositiveIntegerField()
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    scheduled_at = models.DateTimeField()
    grace_deadline = models.DateTimeField()
    next_reminder_at = models.DateTimeField(null=True, blank=True)
    reminder_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_SCHEDULED,
        db_index=True,
    )
    auto_sos_event = models.ForeignKey(
        SOSEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='safety_checkins',
    )
    unconfirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheduled_at', 'pk']

    def __str__(self):
        return 'Safety check-in {} ({})'.format(self.pk, self.status)


class SafetyCheckInAttempt(models.Model):
    KIND_USER_REMINDER = 'USER_REMINDER'
    KIND_GUARDIAN_EMAIL = 'GUARDIAN_EMAIL'
    KIND_CHOICES = (
        (KIND_USER_REMINDER, 'User reminder'),
        (KIND_GUARDIAN_EMAIL, 'Guardian email'),
    )
    STATUS_QUEUED = 'QUEUED'
    STATUS_SENDING = 'SENDING'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_FAILED = 'FAILED'
    STATUS_UNCERTAIN = 'UNCERTAIN'
    STATUS_SKIPPED = 'SKIPPED'
    DELIVERY_CHANNEL_IN_APP = 'IN_APP'
    DELIVERY_CHANNEL_BROWSER = 'BROWSER'
    DELIVERY_CHANNEL_CHOICES = (
        (DELIVERY_CHANNEL_IN_APP, 'In-app'),
        (DELIVERY_CHANNEL_BROWSER, 'Browser notification'),
    )
    STATUS_CHOICES = (
        (STATUS_QUEUED, 'Queued'),
        (STATUS_SENDING, 'Sending'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_UNCERTAIN, 'Uncertain'),
        (STATUS_SKIPPED, 'Skipped'),
    )

    checkin = models.ForeignKey(
        SafetyCheckIn,
        on_delete=models.CASCADE,
        related_name='attempts',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    contact = models.ForeignKey(
        EmergencyContact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='safety_checkin_attempts',
    )
    recipient_email = models.EmailField(blank=True, default='')
    attempt_number = models.PositiveSmallIntegerField(default=1)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_QUEUED,
        db_index=True,
    )
    delivery_channel = models.CharField(
        max_length=20,
        choices=DELIVERY_CHANNEL_CHOICES,
        blank=True,
        default='',
    )
    claim_token = models.UUIDField(null=True, blank=True)
    claim_generation = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    retry_after = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['checkin_id', 'kind', 'recipient_email', 'attempt_number']
        unique_together = (
            'checkin',
            'kind',
            'recipient_email',
            'attempt_number',
        )

    def __str__(self):
        return '{} attempt {} ({})'.format(self.kind, self.attempt_number, self.status)


class CommunityReport(models.Model):
    CATEGORY_HARASSMENT = 'HARASSMENT'
    CATEGORY_INFRASTRUCTURE = 'INFRASTRUCTURE'
    CATEGORY_LIGHTING = 'LIGHTING'
    CATEGORY_SUSPICIOUS_ACTIVITY = 'SUSPICIOUS_ACTIVITY'
    CATEGORY_OTHER = 'OTHER'
    CATEGORY_CHOICES = (
        (CATEGORY_HARASSMENT, 'Harassment or intimidation'),
        (CATEGORY_INFRASTRUCTURE, 'Unsafe infrastructure'),
        (CATEGORY_LIGHTING, 'Poor lighting'),
        (CATEGORY_SUSPICIOUS_ACTIVITY, 'Suspicious activity'),
        (CATEGORY_OTHER, 'Other safety concern'),
    )

    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_UNDER_REVIEW = 'UNDER_REVIEW'
    STATUS_APPROVED = 'APPROVED'
    STATUS_REJECTED = 'REJECTED'
    STATUS_CHOICES = (
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_UNDER_REVIEW, 'Under review'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    )

    reporter = models.CharField(max_length=150, db_index=True)
    submission_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    category = models.CharField(max_length=24, choices=CATEGORY_CHOICES)
    description = models.TextField(max_length=2000)
    approximate_latitude = models.FloatField()
    approximate_longitude = models.FloatField()
    incident_at = models.DateTimeField()
    evidence = models.FileField(
        upload_to=community_evidence_upload_path,
        validators=[validate_community_evidence],
        blank=True,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SUBMITTED, db_index=True)
    moderation_note = models.CharField(max_length=500, blank=True)
    moderated_by = models.CharField(max_length=150, blank=True)
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-incident_at', '-created_at']

    def save(self, *args, **kwargs):
        self.approximate_latitude = round(self.approximate_latitude, 2)
        self.approximate_longitude = round(self.approximate_longitude, 2)
        super(CommunityReport, self).save(*args, **kwargs)

    def __str__(self):
        return '{} report ({})'.format(self.get_category_display(), self.status)


class CommunityReportFlag(models.Model):
    STATUS_PENDING = 'PENDING'
    STATUS_REVIEWED = 'REVIEWED'
    STATUS_DISMISSED = 'DISMISSED'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_REVIEWED, 'Reviewed'),
        (STATUS_DISMISSED, 'Dismissed'),
    )

    report = models.ForeignKey(CommunityReport, on_delete=models.CASCADE, related_name='flags')
    reporter = models.CharField(max_length=150, db_index=True)
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('report', 'reporter')

    def __str__(self):
        return 'Flag for report {} ({})'.format(self.report_id, self.status)


class SOSNotificationAttempt(models.Model):
    CHANNEL_EMAIL = 'EMAIL'
    CHANNEL_SMS = 'SMS'
    CHANNEL_VOICE = 'VOICE'
    CHANNEL_CHOICES = (
        (CHANNEL_EMAIL, 'Email'),
        (CHANNEL_SMS, 'SMS'),
        (CHANNEL_VOICE, 'Voice'),
    )

    STATUS_QUEUED = 'QUEUED'
    STATUS_SENDING = 'SENDING'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_DELIVERED = 'DELIVERED'
    STATUS_FAILED = 'FAILED'
    STATUS_NO_ANSWER = 'NO_ANSWER'
    STATUS_BUSY = 'BUSY'
    STATUS_UNCERTAIN = 'UNCERTAIN'
    STATUS_ACKNOWLEDGED = 'ACKNOWLEDGED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (STATUS_QUEUED, 'Queued'),
        (STATUS_SENDING, 'Sending'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_NO_ANSWER, 'No answer'),
        (STATUS_BUSY, 'Busy'),
        (STATUS_UNCERTAIN, 'Uncertain'),
        (STATUS_ACKNOWLEDGED, 'Acknowledged'),
        (STATUS_CANCELLED, 'Cancelled'),
    )

    sos_event = models.ForeignKey(
        SOSEvent,
        on_delete=models.CASCADE,
        related_name='notification_attempts',
    )
    emergency_contact = models.ForeignKey(
        EmergencyContact,
        on_delete=models.CASCADE,
        related_name='sos_notification_attempts',
    )
    channel = models.CharField(max_length=5, choices=CHANNEL_CHOICES)
    status = models.CharField(max_length=14, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    attempt_number = models.PositiveIntegerField()
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    claim_token = models.UUIDField(null=True, blank=True)
    claim_generation = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True, default='')
    scheduled_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (
            'sos_event',
            'emergency_contact',
            'channel',
            'attempt_number',
        )
        indexes = [
            models.Index(
                fields=['sos_event', 'status', 'scheduled_at'],
                name='sos_attempt_due_idx',
            ),
            models.Index(
                fields=['emergency_contact', 'channel', 'status'],
                name='sos_attempt_contact_idx',
            ),
            models.Index(
                fields=['status', 'lease_expires_at'],
                name='sos_attempt_lease_idx',
            ),
        ]


class SOSContactAcknowledgement(models.Model):
    METHOD_VOICE_DTMF = 'VOICE_DTMF'
    METHOD_WEB_LINK = 'WEB_LINK'
    METHOD_CHOICES = (
        (METHOD_VOICE_DTMF, 'Voice keypad'),
        (METHOD_WEB_LINK, 'Web link'),
    )

    sos_event = models.ForeignKey(
        SOSEvent,
        on_delete=models.CASCADE,
        related_name='contact_acknowledgements',
    )
    emergency_contact = models.ForeignKey(
        EmergencyContact,
        on_delete=models.CASCADE,
        related_name='sos_acknowledgements',
    )
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('sos_event', 'emergency_contact')
        indexes = [
            models.Index(
                fields=['sos_event', 'acknowledged_at'],
                name='sos_ack_event_time_idx',
            ),
        ]


class SOSEscalationState(models.Model):
    STATUS_RUNNING = 'RUNNING'
    STATUS_ACKNOWLEDGED = 'ACKNOWLEDGED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_RESOLVED = 'RESOLVED'
    STATUS_EXHAUSTED = 'EXHAUSTED'
    STATUS_CHOICES = (
        (STATUS_RUNNING, 'Running'),
        (STATUS_ACKNOWLEDGED, 'Acknowledged'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_RESOLVED, 'Resolved'),
        (STATUS_EXHAUSTED, 'Exhausted'),
    )

    sos_event = models.OneToOneField(
        SOSEvent,
        on_delete=models.CASCADE,
        related_name='escalation_state',
    )
    status = models.CharField(max_length=14, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    current_contact = models.ForeignKey(
        EmergencyContact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='current_sos_escalations',
    )
    current_contact_order = models.PositiveIntegerField(null=True, blank=True)
    contact_sequence = models.TextField(default='[]')
    acknowledgement_timeout_seconds = models.PositiveIntegerField(default=60)
    max_retries_per_channel = models.PositiveIntegerField(default=1)
    acknowledgement_deadline = models.DateTimeField(null=True, blank=True)
    next_action_at = models.DateTimeField(null=True, blank=True)
    lease_owner = models.CharField(max_length=100, blank=True, default='')
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_generation = models.PositiveIntegerField(default=0)
    policy_version = models.CharField(max_length=32, default='v1')
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['status', 'next_action_at'],
                name='sos_escalation_next_idx',
            ),
            models.Index(
                fields=['status', 'acknowledgement_deadline'],
                name='sos_escalation_deadline_idx',
            ),
            models.Index(
                fields=['status', 'lease_expires_at'],
                name='sos_escalation_lease_idx',
            ),
        ]