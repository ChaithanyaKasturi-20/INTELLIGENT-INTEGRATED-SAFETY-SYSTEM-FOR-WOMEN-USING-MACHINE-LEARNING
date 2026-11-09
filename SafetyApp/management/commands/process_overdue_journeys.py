from django.core.management.base import BaseCommand

from SafetyApp.journey_notifications import process_overdue_journeys


class Command(BaseCommand):
    help = 'Process scheduled Journey check-ins, grace periods, guardian alerts, and opted-in SOS escalation.'

    def handle(self, *args, **options):
        result = process_overdue_journeys()
        self.stdout.write(
            'Check-ins due: {overdue_journeys}; now unconfirmed: {unconfirmed_journeys}; '
            'guardian emails accepted by SMTP: {sent_notifications}; '
            'failed: {failed_notifications}; uncertain: {uncertain_notifications}; '
            'reminder retries: {reminder_retries}; opted-in SOS events started or reused: '
            '{automatic_sos_events_reused_or_started}.'.format(
                **result
            )
        )
