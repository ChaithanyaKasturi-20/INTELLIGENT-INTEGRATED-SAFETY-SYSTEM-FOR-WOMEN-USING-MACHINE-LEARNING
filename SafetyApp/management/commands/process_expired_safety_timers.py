from datetime import timedelta
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from SafetyApp.models import SOSEmailDelivery
from SafetyApp.safety_checkins import process_safety_checkins


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process due Safety Timer check-ins, grace periods, guardian alerts, and opted-in SOS escalation.'

    def handle(self, *args, **options):
        now = timezone.now()
        try:
            result = process_safety_checkins(now=now, owner_kind='timer')
            processing_errors = 0
        except Exception as exc:
            result = {
                'backfilled_checkins': 0,
                'due_checkins': 0,
                'reminder_retries': 0,
                'unconfirmed_checkins': 0,
                'guardian_emails_accepted': 0,
                'guardian_emails_failed': 0,
                'guardian_emails_uncertain': 0,
            }
            processing_errors = 1
            logger.error(
                'Safety Timer processing failed (error type: %s).',
                type(exc).__name__,
            )

        uncertain_cutoff = now - timedelta(minutes=10)
        interrupted_attempts = SOSEmailDelivery.objects.filter(
            status=SOSEmailDelivery.STATUS_SENDING,
            updated_at__lt=uncertain_cutoff,
        ).update(
            status=SOSEmailDelivery.STATUS_UNCERTAIN,
            failed_at=now,
            error_message=(
                'The previous email attempt was interrupted; delivery is unknown and was not retried '
                'to avoid sending a duplicate alert.'
            ),
            updated_at=now,
        )

        self.stdout.write(
            'Check-ins backfilled: {}; reminders due: {}; retries queued: {}; '
            'unconfirmed: {}; guardian emails accepted: {}; failed: {}; uncertain: {}; '
            'interrupted legacy attempts marked uncertain: {}; processing errors: {}.'.format(
                result['backfilled_checkins'],
                result['due_checkins'],
                result['reminder_retries'],
                result['unconfirmed_checkins'],
                result['guardian_emails_accepted'],
                result['guardian_emails_failed'],
                result['guardian_emails_uncertain'],
                interrupted_attempts,
                processing_errors,
            )
        )
