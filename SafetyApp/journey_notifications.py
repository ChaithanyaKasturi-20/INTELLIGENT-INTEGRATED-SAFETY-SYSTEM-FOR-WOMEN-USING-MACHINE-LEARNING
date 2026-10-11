from datetime import timedelta
from email.message import EmailMessage

from django.db import transaction
from django.utils import timezone

from .models import (
    Journey,
    JourneyActivity,
    JourneyCheckInAlert,
    JourneyCheckInDelivery,
)
from .views import get_panic_email_config


def _record_overdue_journeys(now):
    due_ids = Journey.objects.filter(
        status=Journey.STATUS_ACTIVE,
        guardian_enabled=True,
        expected_arrival__lte=now,
    ).values_list('pk', flat=True)
    created = 0
    for journey_id in due_ids.iterator():
        with transaction.atomic():
            claimed = Journey.objects.filter(
                pk=journey_id,
                status=Journey.STATUS_ACTIVE,
                guardian_enabled=True,
                expected_arrival__lte=now,
            ).update(status=Journey.STATUS_OVERDUE, updated_at=now)
            if not claimed:
                continue
            journey = Journey.objects.get(pk=journey_id)
            JourneyActivity.objects.create(
                journey=journey,
                action=JourneyActivity.ACTION_OVERDUE,
                occurred_at=now,
                details='Expected-arrival deadline passed. No SOS was activated.',
            )
            JourneyCheckInAlert.objects.get_or_create(
                journey=journey,
                deadline_at=journey.expected_arrival,
            )
            created += 1
    return created


def _mark_interrupted_deliveries(now):
    stale_before = now - timedelta(minutes=10)
    return JourneyCheckInDelivery.objects.filter(
        status=JourneyCheckInDelivery.STATUS_SENDING,
        attempted_at__lt=stale_before,
    ).update(
        status=JourneyCheckInDelivery.STATUS_UNCERTAIN,
        completed_at=now,
        error_message='Previous send attempt was interrupted. Delivery is unknown and was not retried to avoid duplicate alerts.',
    )


def _deliver_alert(alert, sender_email, sender_password, now):
    journey = alert.journey
    contacts = journey.guardian_contacts.filter(
        user=journey.user,
        is_active=True,
    ).exclude(email__isnull=True).exclude(email='').order_by('pk')
    by_email = {}
    for contact in contacts:
        by_email.setdefault(contact.email.strip().lower(), contact)

    if not by_email:
        JourneyCheckInAlert.objects.filter(pk=alert.pk).update(
            status=JourneyCheckInAlert.STATUS_NO_RECIPIENTS,
            completed_at=now,
        )
        return 0, 0

    delivery_rows = []
    for recipient_email, contact in by_email.items():
        delivery, created = JourneyCheckInDelivery.objects.get_or_create(
            alert=alert,
            recipient_email=recipient_email,
            defaults={'contact': contact},
        )
        delivery_rows.append(delivery)

    if not sender_email or not sender_password:
        for delivery in delivery_rows:
            JourneyCheckInDelivery.objects.filter(
                pk=delivery.pk,
                status=JourneyCheckInDelivery.STATUS_PENDING,
            ).update(
                status=JourneyCheckInDelivery.STATUS_FAILED,
                attempted_at=now,
                completed_at=now,
                error_message='SMTP is not configured for this environment.',
            )
    else:
        pending_ids = []
        for delivery in delivery_rows:
            claimed = JourneyCheckInDelivery.objects.filter(
                pk=delivery.pk,
                status=JourneyCheckInDelivery.STATUS_PENDING,
            ).update(
                status=JourneyCheckInDelivery.STATUS_SENDING,
                attempted_at=now,
            )
            if claimed:
                pending_ids.append(delivery.pk)

        if pending_ids:
            try:
                import smtplib
                with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=20) as connection:
                    connection.login(sender_email, sender_password)
                    for delivery in JourneyCheckInDelivery.objects.filter(pk__in=pending_ids):
                        message = EmailMessage()
                        message['From'] = sender_email
                        message['To'] = delivery.recipient_email
                        message['Subject'] = 'Journey Guardian check-in overdue'
                        message.set_content(
                            'A journey check-in is overdue for a contact who selected you as a trusted '
                            'notification recipient.\n\n'
                            'Planned journey: {} to {}\n'
                            'Expected check-in: {}\n'
                            'The application did not automatically activate an SOS or contact emergency '
                            'services. Please use your own judgment to contact the person or local '
                            'emergency services if appropriate.'.format(
                                journey.origin_label,
                                journey.destination_label,
                                timezone.localtime(alert.deadline_at).strftime('%Y-%m-%d %H:%M %Z'),
                            )
                        )
                        try:
                            refused = connection.send_message(message)
                            if refused:
                                raise smtplib.SMTPRecipientsRefused(refused)
                        except Exception as exc:
                            JourneyCheckInDelivery.objects.filter(pk=delivery.pk).update(
                                status=JourneyCheckInDelivery.STATUS_FAILED,
                                completed_at=timezone.now(),
                                error_message=str(exc)[:500],
                            )
                        else:
                            JourneyCheckInDelivery.objects.filter(pk=delivery.pk).update(
                                status=JourneyCheckInDelivery.STATUS_SENT,
                                completed_at=timezone.now(),
                                error_message='',
                            )
            except Exception as exc:
                JourneyCheckInDelivery.objects.filter(
                    pk__in=pending_ids,
                    status=JourneyCheckInDelivery.STATUS_SENDING,
                ).update(
                    status=JourneyCheckInDelivery.STATUS_FAILED,
                    completed_at=timezone.now(),
                    error_message=str(exc)[:500],
                )

    statuses = list(
        JourneyCheckInDelivery.objects.filter(alert=alert).values_list('status', flat=True)
    )
    sent = statuses.count(JourneyCheckInDelivery.STATUS_SENT)
    failed = len(statuses) - sent
    alert_status = JourneyCheckInAlert.STATUS_SENT if sent == len(statuses) else JourneyCheckInAlert.STATUS_FAILED
    JourneyCheckInAlert.objects.filter(pk=alert.pk).update(
        status=alert_status,
        completed_at=timezone.now(),
    )
    return sent, failed


def process_overdue_journeys(now=None):
    now = now or timezone.now()
    created = _record_overdue_journeys(now)
    uncertain = _mark_interrupted_deliveries(now)
    sender_email, sender_password = get_panic_email_config()
    sent = 0
    failed = 0
    for alert in JourneyCheckInAlert.objects.filter(status=JourneyCheckInAlert.STATUS_PENDING).select_related('journey'):
        delivered, not_delivered = _deliver_alert(alert, sender_email, sender_password, now)
        sent += delivered
        failed += not_delivered
    return {
        'overdue_journeys': created,
        'sent_notifications': sent,
        'failed_notifications': failed,
        'uncertain_notifications': uncertain,
    }
