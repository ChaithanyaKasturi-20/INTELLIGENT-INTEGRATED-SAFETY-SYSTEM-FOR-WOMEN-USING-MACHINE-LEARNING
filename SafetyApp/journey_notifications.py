from .safety_checkins import process_safety_checkins


def process_overdue_journeys(now=None):
    result = process_safety_checkins(now=now, owner_kind='journey')
    return {
        'overdue_journeys': result['due_checkins'],
        'unconfirmed_journeys': result['unconfirmed_checkins'],
        'sent_notifications': result['guardian_emails_accepted'],
        'failed_notifications': result['guardian_emails_failed'],
        'uncertain_notifications': result['guardian_emails_uncertain'],
        'reminder_retries': result['reminder_retries'],
        'automatic_sos_events_reused_or_started': result['automatic_sos_events_reused_or_started'],
    }
