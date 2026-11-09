import os
import re
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError

from SafetyApp.sos_notifications import EXOTEL_API_HOSTS, _environment_flag


ACCOUNT_SID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
VOICE_CALLER_ID_PATTERN = re.compile(r'^\+?[0-9]{5,20}$')
VOICE_APP_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
SMS_ENDPOINT_PATH = '/v1/Accounts/{}/Sms/send'
VOICE_ENDPOINT_PATH = '/v1/Accounts/{}/Calls/connect'
VOICE_FLOW_PATH = '/{}/exoml/start_voice/{}'

SHARED_SETTINGS = (
    'SOS_EXOTEL_API_KEY',
    'SOS_EXOTEL_API_TOKEN',
    'SOS_EXOTEL_ACCOUNT_SID',
    'SOS_EXOTEL_REGION',
)
CHANNEL_SETTINGS = {
    'SMS': (
        'SOS_EXOTEL_SMS_SENDER_ID',
        'SOS_EXOTEL_SMS_BODY',
    ),
    'VOICE': (
        'SOS_EXOTEL_VOICE_CALLER_ID',
        'SOS_EXOTEL_VOICE_APP_ID',
    ),
}


def _configured(name, environ):
    return bool(environ.get(name, '').strip())


def _endpoint_status(channel, environ):
    account_sid = environ.get('SOS_EXOTEL_ACCOUNT_SID', '').strip()
    region = environ.get('SOS_EXOTEL_REGION', '').strip().lower()
    if not ACCOUNT_SID_PATTERN.fullmatch(account_sid):
        return False
    host = EXOTEL_API_HOSTS.get(region)
    if host is None:
        return False

    if channel == 'SMS':
        endpoint = 'https://{}{}'.format(
            host,
            SMS_ENDPOINT_PATH.format(account_sid),
        )
        parsed = urlsplit(endpoint)
        return (
            parsed.scheme == 'https'
            and parsed.hostname == host
            and parsed.path == SMS_ENDPOINT_PATH.format(account_sid)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )

    app_id = environ.get('SOS_EXOTEL_VOICE_APP_ID', '').strip()
    if not VOICE_APP_ID_PATTERN.fullmatch(app_id):
        return False
    endpoint = 'https://{}{}'.format(
        host,
        VOICE_ENDPOINT_PATH.format(account_sid),
    )
    flow_url = 'http://my.exotel.com{}'.format(
        VOICE_FLOW_PATH.format(account_sid, app_id),
    )
    parsed_endpoint = urlsplit(endpoint)
    parsed_flow = urlsplit(flow_url)
    return (
        parsed_endpoint.scheme == 'https'
        and parsed_endpoint.hostname == host
        and parsed_endpoint.path == VOICE_ENDPOINT_PATH.format(account_sid)
        and not parsed_endpoint.username
        and not parsed_endpoint.password
        and not parsed_endpoint.query
        and not parsed_endpoint.fragment
        and parsed_flow.scheme == 'http'
        and parsed_flow.hostname == 'my.exotel.com'
        and parsed_flow.path == VOICE_FLOW_PATH.format(account_sid, app_id)
        and not parsed_flow.username
        and not parsed_flow.password
        and not parsed_flow.query
        and not parsed_flow.fragment
    )


def _channel_configuration(channel, environ):
    missing = [
        name
        for name in SHARED_SETTINGS + CHANNEL_SETTINGS[channel]
        if not _configured(name, environ)
    ]
    invalid = []
    account_sid = environ.get('SOS_EXOTEL_ACCOUNT_SID', '').strip()
    region = environ.get('SOS_EXOTEL_REGION', '').strip().lower()
    if account_sid and not ACCOUNT_SID_PATTERN.fullmatch(account_sid):
        invalid.append('SOS_EXOTEL_ACCOUNT_SID')
    if region and region not in EXOTEL_API_HOSTS:
        invalid.append('SOS_EXOTEL_REGION')

    if channel == 'SMS':
        message_body = environ.get('SOS_EXOTEL_SMS_BODY', '').strip()
        if message_body and len(message_body) > 2000:
            invalid.append('SOS_EXOTEL_SMS_BODY')
        if region == 'in' and not _configured('SOS_EXOTEL_DLT_ENTITY_ID', environ):
            missing.append('SOS_EXOTEL_DLT_ENTITY_ID')
    else:
        caller_id = environ.get('SOS_EXOTEL_VOICE_CALLER_ID', '').strip()
        app_id = environ.get('SOS_EXOTEL_VOICE_APP_ID', '').strip()
        if caller_id and not VOICE_CALLER_ID_PATTERN.fullmatch(caller_id):
            invalid.append('SOS_EXOTEL_VOICE_CALLER_ID')
        if app_id and not VOICE_APP_ID_PATTERN.fullmatch(app_id):
            invalid.append('SOS_EXOTEL_VOICE_APP_ID')

    endpoint_valid = _endpoint_status(channel, environ)
    if not endpoint_valid and not missing and not any(
        name in invalid
        for name in ('SOS_EXOTEL_ACCOUNT_SID', 'SOS_EXOTEL_REGION',
                     'SOS_EXOTEL_VOICE_APP_ID')
    ):
        invalid.append('Exotel endpoint configuration')

    return {
        'missing': sorted(set(missing)),
        'invalid': sorted(set(invalid)),
        'endpoint_valid': endpoint_valid,
    }


def _provider_mode(channel, environ):
    real_flag = 'SOS_EXOTEL_{}_ENABLED'.format(channel)
    mock_flag = 'SOS_MOCK_{}_ENABLED'.format(channel)
    flag_errors = []
    try:
        real_enabled = _environment_flag(real_flag, False)
    except ValueError:
        real_enabled = False
        flag_errors.append(real_flag)
    try:
        mock_enabled = _environment_flag(mock_flag, not real_enabled)
    except ValueError:
        mock_enabled = False
        flag_errors.append(mock_flag)

    conflict = real_enabled and mock_enabled
    if conflict:
        flag_errors.extend((real_flag, mock_flag))
    if flag_errors:
        mode = 'INVALID'
    elif real_enabled:
        mode = 'REAL'
    elif mock_enabled:
        mode = 'MOCK'
    else:
        mode = 'DISABLED'
    return {
        'real_flag': real_flag,
        'mock_flag': mock_flag,
        'real_enabled': real_enabled,
        'mock_enabled': mock_enabled,
        'mode': mode,
        'errors': sorted(set(flag_errors)),
    }


class Command(BaseCommand):
    help = 'Check Exotel configuration readiness without displaying secrets or contacting Exotel.'

    def handle(self, *args, **options):
        environ = os.environ
        self.stdout.write(
            'Exotel configuration check (environment values only; no network requests).'
        )
        self.stdout.write(
            'Credential presence: '
            'API key={}, API token={}, account SID={}.'.format(
                self._presence('SOS_EXOTEL_API_KEY', environ),
                self._presence('SOS_EXOTEL_API_TOKEN', environ),
                self._presence('SOS_EXOTEL_ACCOUNT_SID', environ),
            )
        )

        invalid_configuration = False
        for channel in ('SMS', 'VOICE'):
            configuration = _channel_configuration(channel, environ)
            mode = _provider_mode(channel, environ)
            invalid_configuration = invalid_configuration or bool(
                configuration['invalid'] or mode['errors']
            )
            config_ready = not configuration['missing'] and not configuration['invalid']

            if mode['mode'] == 'REAL':
                if config_ready:
                    readiness = 'REAL PROVIDER READY'
                else:
                    readiness = 'REAL PROVIDER NOT READY'
            elif mode['mode'] == 'INVALID':
                readiness = 'INVALID PROVIDER FLAGS'
            elif mode['mode'] == 'DISABLED':
                readiness = 'DISABLED'
            else:
                readiness = (
                    'MOCK MODE (real configuration READY)'
                    if config_ready
                    else 'MOCK MODE (real configuration NOT READY)'
                )

            self.stdout.write('{} readiness: {}.'.format(channel, readiness))
            self.stdout.write(
                '  Mock mode: {}; {}: {}; {}: {}.'.format(
                    'enabled' if mode['mock_enabled'] else 'disabled',
                    mode['mock_flag'],
                    'invalid' if mode['mock_flag'] in mode['errors']
                    else ('enabled' if mode['mock_enabled'] else 'disabled'),
                    mode['real_flag'],
                    'invalid' if mode['real_flag'] in mode['errors']
                    else ('enabled' if mode['real_enabled'] else 'disabled'),
                )
            )
            self.stdout.write(
                '  Endpoint validity: {} (configuration syntax only; reachability not tested).'.format(
                    'valid' if configuration['endpoint_valid'] else 'invalid/not configured'
                )
            )
            if configuration['missing']:
                self.stdout.write(
                    '  Missing settings: {}.'.format(', '.join(configuration['missing']))
                )
            if configuration['invalid']:
                self.stdout.write(
                    '  Invalid settings: {}.'.format(', '.join(configuration['invalid']))
                )
            if mode['errors']:
                self.stdout.write(
                    '  Invalid provider flags: {}.'.format(', '.join(mode['errors']))
                )

            if mode['mode'] == 'REAL' and not config_ready:
                invalid_configuration = True
            if mode['mode'] == 'INVALID':
                invalid_configuration = True

        if invalid_configuration:
            raise CommandError(
                'Exotel configuration is invalid or incomplete for an enabled real provider.'
            )

    @staticmethod
    def _presence(name, environ):
        return 'present' if _configured(name, environ) else 'missing'
