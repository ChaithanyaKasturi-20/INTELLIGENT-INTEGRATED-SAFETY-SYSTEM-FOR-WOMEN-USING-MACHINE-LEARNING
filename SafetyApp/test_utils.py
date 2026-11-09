import tempfile

from django.test import override_settings


def isolate_register_test_database(test_case):
    temporary_base_dir = tempfile.TemporaryDirectory(
        prefix='safety-register-test-',
    )
    test_case.addCleanup(temporary_base_dir.cleanup)
    settings_override = override_settings(
        REGISTER_TEST_DB_DIR=temporary_base_dir.name,
    )
    settings_override.enable()
    test_case.addCleanup(settings_override.disable)
    test_case.register_test_directory = temporary_base_dir
    test_case.register_test_base_dir = temporary_base_dir.name
