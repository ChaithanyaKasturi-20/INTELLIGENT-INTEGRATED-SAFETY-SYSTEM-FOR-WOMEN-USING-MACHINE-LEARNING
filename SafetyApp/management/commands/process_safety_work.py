import logging

from django.core.management.base import BaseCommand, CommandError

from SafetyApp.safety_checkins import process_safety_checkins
from SafetyApp.sos_escalation import process_due_escalations
from SafetyApp.sos_notifications import recover_interrupted_attempts


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process due safety check-ins and SOS work once; schedule this command externally.'

    def handle(self, *args, **options):
        escalation_failures = []

        def report_escalation_failure(event_id, error):
            escalation_failures.append((event_id, error))
            logger.error(
                'SOS escalation processing failed for event %s (error type: %s).',
                event_id,
                type(error).__name__,
            )
            self.stderr.write(
                'SOS event {} failed ({}).'.format(event_id, type(error).__name__)
            )

        try:
            checkins = process_safety_checkins()
            recovered_attempts = recover_interrupted_attempts()
            escalations = process_due_escalations(on_error=report_escalation_failure)
        except Exception as exc:
            logger.exception(
                'Scheduled safety work failed (error type: %s).',
                type(exc).__name__,
            )
            raise CommandError(
                'Scheduled safety work failed before the batch completed ({}).'.format(
                    type(exc).__name__
                )
            )

        self.stdout.write(
            'Check-ins due: {due_checkins}; reminder retries: {reminder_retries}; '
            'check-ins unconfirmed: {unconfirmed_checkins}; '
            'guardian emails accepted: {guardian_emails_accepted}; failed: {guardian_emails_failed}; '
            'uncertain: {guardian_emails_uncertain}; auto-SOS events started or reused: '
            '{automatic_sos_events_reused_or_started}; interrupted SOS attempts recovered: {}; '
            'SOS escalation events processed: {}; escalation failures: {}.'.format(
                recovered_attempts,
                escalations,
                len(escalation_failures),
                **checkins
            )
        )
        if escalation_failures:
            raise CommandError(
                'Scheduled safety work completed with {} SOS escalation failure(s).'.format(
                    len(escalation_failures)
                )
            )
