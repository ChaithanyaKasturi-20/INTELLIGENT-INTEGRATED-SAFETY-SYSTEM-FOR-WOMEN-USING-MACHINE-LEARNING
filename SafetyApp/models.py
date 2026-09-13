import re

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


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
            if not re.fullmatch(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', cleaned_email):
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
