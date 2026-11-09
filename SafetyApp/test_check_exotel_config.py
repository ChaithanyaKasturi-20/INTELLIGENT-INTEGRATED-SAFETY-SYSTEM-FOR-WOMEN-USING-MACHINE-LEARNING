import importlib.util
import os
import sys
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase


class CheckExotelConfigTests(SimpleTestCase):
    def run_check(self, environment):
        output = StringIO()
        errors = StringIO()
        with patch.dict(os.environ, environment, clear=True), patch(
            'urllib.request.urlopen',
            side_effect=AssertionError('configuration check attempted network access'),
        ) as urlopen:
            try:
                call_command(
                    'check_exotel_config',
                    stdout=output,
                    stderr=errors,
                )
            except CommandError as exc:
                return output.getvalue(), errors.getvalue(), str(exc), urlopen
        return output.getvalue(), errors.getvalue(), None, urlopen

    def test_missing_configuration_reports_mock_only_without_failing(self):
        output, errors, command_error, urlopen = self.run_check({})

        self.assertIsNone(command_error)
        self.assertEqual(errors, '')
        self.assertIn('Credential presence: API key=missing, API token=missing', output)
        self.assertIn('SMS readiness: MOCK MODE (real configuration NOT READY)', output)
        self.assertIn('VOICE readiness: MOCK MODE (real configuration NOT READY)', output)
        self.assertIn('SOS_EXOTEL_SMS_ENABLED: disabled', output)
        self.assertIn('SOS_EXOTEL_VOICE_ENABLED: disabled', output)
        self.assertIn('Endpoint validity: invalid/not configured', output)
        self.assertIn('SOS_EXOTEL_API_KEY', output)
        urlopen.assert_not_called()

    def test_complete_real_configuration_reports_ready_without_revealing_values(self):
        secret_values = (
            'synthetic-api-key-must-not-print',
            'synthetic-api-token-must-not-print',
            'synthetic-account-sid-must-not-print',
            'synthetic-sms-body-must-not-print',
        )
        environment = {
            'SOS_EXOTEL_API_KEY': secret_values[0],
            'SOS_EXOTEL_API_TOKEN': secret_values[1],
            'SOS_EXOTEL_ACCOUNT_SID': secret_values[2],
            'SOS_EXOTEL_REGION': 'IN',
            'SOS_EXOTEL_SMS_SENDER_ID': 'SYNTHETIC',
            'SOS_EXOTEL_SMS_BODY': secret_values[3],
            'SOS_EXOTEL_DLT_ENTITY_ID': 'synthetic-entity',
            'SOS_EXOTEL_VOICE_CALLER_ID': '+910123456789',
            'SOS_EXOTEL_VOICE_APP_ID': 'synthetic-flow',
            'SOS_EXOTEL_SMS_ENABLED': 'true',
            'SOS_MOCK_SMS_ENABLED': 'false',
            'SOS_EXOTEL_VOICE_ENABLED': 'true',
            'SOS_MOCK_VOICE_ENABLED': 'false',
        }

        output, errors, command_error, urlopen = self.run_check(environment)

        self.assertIsNone(command_error)
        self.assertEqual(errors, '')
        self.assertIn('SMS readiness: REAL PROVIDER READY', output)
        self.assertIn('VOICE readiness: REAL PROVIDER READY', output)
        self.assertIn('Mock mode: disabled', output)
        self.assertIn('SOS_EXOTEL_SMS_ENABLED: enabled', output)
        self.assertIn('SOS_EXOTEL_VOICE_ENABLED: enabled', output)
        self.assertEqual(output.count('Endpoint validity: valid'), 2)
        self.assertIn('API key=present, API token=present, account SID=present', output)
        for secret_value in secret_values:
            self.assertNotIn(secret_value, output)
            self.assertNotIn(secret_value, errors)
        urlopen.assert_not_called()

    def test_invalid_configuration_and_conflicting_flags_are_reported_safely(self):
        environment = {
            'SOS_EXOTEL_API_KEY': 'do-not-print-key',
            'SOS_EXOTEL_API_TOKEN': 'do-not-print-token',
            'SOS_EXOTEL_ACCOUNT_SID': 'invalid/account',
            'SOS_EXOTEL_REGION': 'xx',
            'SOS_EXOTEL_SMS_SENDER_ID': 'SYNTHETIC',
            'SOS_EXOTEL_SMS_BODY': 'x' * 2001,
            'SOS_EXOTEL_VOICE_CALLER_ID': 'invalid-number',
            'SOS_EXOTEL_VOICE_APP_ID': 'invalid/app',
            'SOS_EXOTEL_SMS_ENABLED': 'sometimes',
            'SOS_MOCK_SMS_ENABLED': 'true',
            'SOS_EXOTEL_VOICE_ENABLED': 'true',
            'SOS_MOCK_VOICE_ENABLED': 'true',
        }

        output, errors, command_error, urlopen = self.run_check(environment)

        self.assertIsNotNone(command_error)
        self.assertIn('configuration is invalid or incomplete', command_error)
        self.assertIn('SOS_EXOTEL_ACCOUNT_SID', output)
        self.assertIn('SOS_EXOTEL_REGION', output)
        self.assertIn('SOS_EXOTEL_SMS_BODY', output)
        self.assertIn('SOS_EXOTEL_VOICE_CALLER_ID', output)
        self.assertIn('SOS_EXOTEL_VOICE_APP_ID', output)
        self.assertIn('Invalid provider flags: SOS_EXOTEL_SMS_ENABLED', output)
        self.assertIn('SOS_EXOTEL_VOICE_ENABLED, SOS_MOCK_VOICE_ENABLED', output)
        self.assertNotIn('do-not-print-key', output)
        self.assertNotIn('do-not-print-token', output)
        self.assertNotIn('invalid/account', output)
        self.assertNotIn('invalid-number', output)
        self.assertEqual(errors, '')
        urlopen.assert_not_called()

    def test_project_settings_skip_env_file_for_readiness_command(self):
        settings_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__),
            '..',
            'Safety',
            'settings.py',
        ))
        spec = importlib.util.spec_from_file_location(
            'phase_7c_test_settings',
            settings_path,
        )
        settings_module = importlib.util.module_from_spec(spec)
        original_isfile = os.path.isfile
        original_open = open

        def guarded_isfile(path):
            if os.fspath(path).lower().endswith('.env'):
                raise AssertionError('settings probed the .env file')
            return original_isfile(path)

        def guarded_open(path, *args, **kwargs):
            if os.fspath(path).lower().endswith('.env'):
                raise AssertionError('settings opened the .env file')
            return original_open(path, *args, **kwargs)

        with patch.object(sys, 'argv', ['manage.py', 'check_exotel_config']), patch(
            'os.path.isfile',
            side_effect=guarded_isfile,
        ), patch('builtins.open', side_effect=guarded_open):
            spec.loader.exec_module(settings_module)
