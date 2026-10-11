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
    email = models.EmailField(blank=True, null=True)
    relationship = models.CharField(max_length=80, blank=True, null=True)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
        self.email = (self.email or '').strip() or None
        self.relationship = (self.relationship or '').strip() or None
        self.full_clean()

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
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'pending'),
        (STATUS_SENT, 'sent'),
        (STATUS_FAILED, 'failed'),
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

    def __str__(self):
        return '{} -> {} ({})'.format(self.emergency_contact.name, self.status, self.sos_event_id)


class SafetyTimer(models.Model):
    STATUS_ACTIVE = 'ACTIVE'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_EXPIRED = 'EXPIRED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (STATUS_ACTIVE, 'ACTIVE'),
        (STATUS_COMPLETED, 'COMPLETED'),
        (STATUS_EXPIRED, 'EXPIRED'),
        (STATUS_CANCELLED, 'CANCELLED'),
    )

    user = models.CharField(max_length=150, db_index=True)
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    duration = models.PositiveIntegerField(default=15)
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
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (STATUS_PLANNED, 'Planned'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_OVERDUE, 'Overdue'),
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
    ACTION_ENDED = 'ENDED'
    ACTION_CANCELLED = 'CANCELLED'
    ACTION_CHOICES = (
        (ACTION_PLANNED, 'Planned'),
        (ACTION_STARTED, 'Started'),
        (ACTION_CHECKED_IN, 'Checked in safe'),
        (ACTION_EXTENDED, 'Deadline extended'),
        (ACTION_OVERDUE, 'Check-in overdue'),
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
