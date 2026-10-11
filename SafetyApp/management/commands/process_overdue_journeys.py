from django.core.management.base import BaseCommand

from SafetyApp.journey_notifications import process_overdue_journeys


class Command(BaseCommand):
    help = 'Process due Journey Guardian check-ins and send one notification attempt per contact.'

    def handle(self, *args, **options):
        result = process_overdue_journeys()
        self.stdout.write(
            'Overdue journeys: {overdue_journeys}; notifications sent: {sent_notifications}; '
            'notifications failed: {failed_notifications}; uncertain attempts: {uncertain_notifications}.'.format(
                **result
            )
        )
