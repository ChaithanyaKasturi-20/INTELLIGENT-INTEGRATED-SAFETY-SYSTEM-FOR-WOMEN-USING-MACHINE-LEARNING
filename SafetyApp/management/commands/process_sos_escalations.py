import logging

from django.core.management.base import BaseCommand, CommandError

from SafetyApp.sos_escalation import process_due_escalations
from SafetyApp.sos_notifications import recover_interrupted_attempts


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process due SOS escalation events once; schedule this command externally.'

    def handle(self, *args, **options):
        failures = []

        def report_failure(event_id, error):
            failures.append((event_id, error))
            logger.error(
                'SOS escalation processing failed for event %s (error type: %s).',
                event_id,
                type(error).__name__,
            )
            self.stderr.write(
                'SOS event {} failed ({}).'.format(event_id, type(error).__name__)
            )

        try:
            recovered_attempts = recover_interrupted_attempts()
            processed = process_due_escalations(on_error=report_failure)
        except Exception as exc:
            logger.exception(
                'SOS escalation batch processing failed (error type: %s).',
                type(exc).__name__,
            )
            raise CommandError(
                'SOS escalation processing failed before the batch completed ({}).'.format(
                    type(exc).__name__
                )
            )

        self.stdout.write(
            'Stale SENDING attempts marked UNCERTAIN: {}; '
            'SOS escalation events processed: {}; event failures: {}.'.format(
                recovered_attempts,
                processed,
                len(failures),
            )
        )
        if failures:
            raise CommandError(
                'SOS escalation processing completed with {} event failure(s).'.format(
                    len(failures)
                )
            )
