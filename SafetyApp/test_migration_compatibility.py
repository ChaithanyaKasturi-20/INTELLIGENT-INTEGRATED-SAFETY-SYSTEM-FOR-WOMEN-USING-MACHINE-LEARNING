import hashlib
import builtins
import importlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEMP_ROOT = None
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

MIGRATION_SUPPORT_0011_SHA256 = (
    '6e67b664484277c79a5b67da637169939013a3b4136fec64c038a66b5bce4913'
)


class MigrationCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _TEMP_ROOT

        super().setUpClass()
        cls._temporary_root_owner = None
        if _TEMP_ROOT is None:
            cls._temporary_root_owner = tempfile.TemporaryDirectory(
                prefix='migration-compatibility-tests-',
            )
            _TEMP_ROOT = Path(cls._temporary_root_owner.name).resolve()

    @classmethod
    def tearDownClass(cls):
        global _TEMP_ROOT

        try:
            super().tearDownClass()
        finally:
            if cls._temporary_root_owner is not None:
                root = _TEMP_ROOT
                cls._temporary_root_owner.cleanup()
                if _TEMP_ROOT == root:
                    _TEMP_ROOT = None

    def setUp(self):
        from django.db import connections

        self.database_dir = tempfile.mkdtemp(
            prefix='migration-case-',
            dir=str(_TEMP_ROOT),
        )
        self.connection = connections['default']
        self.original_database_name = self.connection.settings_dict['NAME']
        self.connection.close()
        self.connection.settings_dict['NAME'] = os.path.join(
            self.database_dir,
            'database.sqlite3',
        )

    def tearDown(self):
        self.connection.close()
        self.connection.settings_dict['NAME'] = self.original_database_name
        shutil.rmtree(self.database_dir)

    def _migrate(self, migration_name):
        from django.db.migrations.executor import MigrationExecutor

        target = ('SafetyApp', migration_name)
        executor = MigrationExecutor(self.connection)
        executor.migrate([target])
        return MigrationExecutor(self.connection).loader.project_state(
            [target],
        ).apps

    def _state_apps(self, migration_name):
        from django.db.migrations.executor import MigrationExecutor

        target = ('SafetyApp', migration_name)
        return MigrationExecutor(self.connection).loader.project_state(
            [target],
        ).apps

    def _workflow_models(self, apps):
        return [
            apps.get_model('SafetyApp', model_name)
            for model_name in (
                'SOSNotificationAttempt',
                'SOSContactAcknowledgement',
                'SOSEscalationState',
            )
        ]

    def _create_workflow_tables(self, models):
        with self.connection.schema_editor() as schema_editor:
            for model in models:
                schema_editor.create_model(model)

    def _create_base_rows(self, apps):
        contact_model = apps.get_model('SafetyApp', 'EmergencyContact')
        event_model = apps.get_model('SafetyApp', 'SOSEvent')
        contact = contact_model.objects.create(
            user='synthetic-user',
            name='Synthetic contact',
            phone_number='00000000',
        )
        event = event_model.objects.create(user='synthetic-user')
        return contact, event

    def _create_workflow_rows(self, apps, contact, event, include_state=True):
        attempt_model, acknowledgement_model, state_model = (
            self._workflow_models(apps)
        )
        attempt = attempt_model.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel='EMAIL',
            attempt_number=1,
        )
        acknowledgement = acknowledgement_model.objects.create(
            sos_event=event,
            emergency_contact=contact,
            method='WEB_LINK',
        )
        state = (
            state_model.objects.create(sos_event=event)
            if include_state else None
        )
        return attempt, acknowledgement, state

    def _record_0011(self):
        from django.db.migrations.recorder import MigrationRecorder

        MigrationRecorder(self.connection).record_applied(
            'SafetyApp',
            '0011_sos_cross_process_leases',
        )

    def _assert_workflow_schema(self, apps):
        from SafetyApp.migration_compatibility import (
            _actual_schema,
            _expected_schema,
            _schema_mismatches,
        )

        models = self._workflow_models(apps)
        with self.connection.cursor() as cursor:
            tables = set(self.connection.introspection.table_names(cursor))
            for model in models:
                self.assertIn(model._meta.db_table, tables)
                expected = _expected_schema(model, self.connection)
                actual = _actual_schema(
                    self.connection,
                    cursor,
                    model._meta.db_table,
                )
                self.assertEqual(
                    [],
                    _schema_mismatches(model, expected, actual),
                    model._meta.db_table,
                )

    def test_fresh_migration_chain_schema_and_model_consistency(self):
        from django.core.management import call_command
        from django.db import IntegrityError
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.writer import MigrationWriter
        from SafetyApp.migration_compatibility import (
            ensure_sos_workflow_schema,
        )

        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)
        MigrationLoader(self.connection).check_consistent_history(
            self.connection,
        )
        call_command(
            'makemigrations',
            'SafetyApp',
            check=True,
            dry_run=True,
            verbosity=0,
        )
        loader = MigrationLoader(self.connection)
        MigrationWriter(
            loader.disk_migrations[
                ('SafetyApp', '0011_sos_cross_process_leases')
            ],
        ).as_string()
        MigrationWriter(
            loader.disk_migrations[
                ('SafetyApp', '0013_sos_schema_compatibility')
            ],
        ).as_string()

        contact_model = apps.get_model('SafetyApp', 'EmergencyContact')
        contact = contact_model.objects.create(
            user='synthetic-user',
            name='Synthetic contact',
            phone_number='00000000',
        )
        event_model = apps.get_model('SafetyApp', 'SOSEvent')
        event = event_model.objects.create(user='synthetic-user')
        attempt_model, acknowledgement_model, state_model = (
            self._workflow_models(apps)
        )
        attempt_model.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel='EMAIL',
            attempt_number=1,
        )
        acknowledgement_model.objects.create(
            sos_event=event,
            emergency_contact=contact,
            method='WEB_LINK',
        )
        state_model.objects.create(sos_event=event)

        for duplicate in (
            lambda: attempt_model.objects.create(
                sos_event=event,
                emergency_contact=contact,
                channel='EMAIL',
                attempt_number=1,
            ),
            lambda: acknowledgement_model.objects.create(
                sos_event=event,
                emergency_contact=contact,
                method='WEB_LINK',
            ),
            lambda: state_model.objects.create(sos_event=event),
        ):
            with self.assertRaises(IntegrityError):
                duplicate()

        boolean_fields = ('email_consent', 'sms_consent', 'voice_consent')
        for field_name in boolean_fields:
            field = contact_model._meta.get_field(field_name)
            self.assertIs(field.default, False)
            self.assertFalse(getattr(contact, field_name))
        for field_name in (
            'consent_updated_at',
            'email_verified_at',
            'phone_verified_at',
        ):
            field = contact_model._meta.get_field(field_name)
            self.assertTrue(field.null)
            self.assertTrue(field.blank)
            self.assertIsNone(getattr(contact, field_name))

        with self.connection.schema_editor() as schema_editor:
            models = self._workflow_models(apps)
            ensure_sos_workflow_schema(models, schema_editor)
            ensure_sos_workflow_schema(models, schema_editor)
        self.assertEqual(1, attempt_model.objects.count())
        self.assertEqual(1, acknowledgement_model.objects.count())
        self.assertEqual(1, state_model.objects.count())

    def test_migrations_do_not_call_mutable_compatibility_helper(self):
        with patch(
            'SafetyApp.migration_compatibility.ensure_sos_workflow_schema',
            side_effect=AssertionError('mutable helper must not be used'),
        ) as mutable_helper:
            apps = self._migrate('0013_sos_schema_compatibility')

        mutable_helper.assert_not_called()
        self._assert_workflow_schema(apps)

    def test_versioned_migration_support_source_is_immutable(self):
        support_path = (
            Path(__file__).resolve().with_name('migration_support_0011.py')
        )
        source = support_path.read_bytes().replace(b'\r\n', b'\n').replace(
            b'\r', b'\n',
        )
        digest = hashlib.sha256(source).hexdigest()
        self.assertEqual(MIGRATION_SUPPORT_0011_SHA256, digest)

    def test_at_0010_no_sos_tables_creates_complete_chain(self):
        self._migrate('0010_personal_safety_profile')
        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)

    def test_at_0010_matching_tables_are_adopted_and_rows_preserved(self):
        self._migrate('0010_personal_safety_profile')
        apps11 = self._state_apps('0011_sos_cross_process_leases')
        contact, event = self._create_base_rows(apps11)
        models = self._workflow_models(apps11)
        self._create_workflow_tables(models)
        rows = self._create_workflow_rows(apps11, contact, event)

        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)
        for model_name, row in zip(
            (
                'SOSNotificationAttempt',
                'SOSContactAcknowledgement',
                'SOSEscalationState',
            ),
            rows,
        ):
            self.assertTrue(
                apps.get_model('SafetyApp', model_name)
                .objects.filter(pk=row.pk).exists()
            )
        preserved_contact = apps.get_model(
            'SafetyApp',
            'EmergencyContact',
        ).objects.get(pk=contact.pk)
        self.assertFalse(preserved_contact.email_consent)
        self.assertIsNone(preserved_contact.consent_updated_at)
        self.assertTrue(
            apps.get_model('SafetyApp', 'SOSEvent')
            .objects.filter(pk=event.pk).exists()
        )

    def test_at_0010_one_matching_table_is_adopted_and_others_created(self):
        self._migrate('0010_personal_safety_profile')
        apps11 = self._state_apps('0011_sos_cross_process_leases')
        contact, event = self._create_base_rows(apps11)
        attempt_model = apps11.get_model(
            'SafetyApp',
            'SOSNotificationAttempt',
        )
        self._create_workflow_tables([attempt_model])
        attempt = attempt_model.objects.create(
            sos_event=event,
            emergency_contact=contact,
            channel='EMAIL',
            attempt_number=1,
        )

        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)
        self.assertTrue(
            apps.get_model('SafetyApp', 'SOSNotificationAttempt')
            .objects.filter(pk=attempt.pk).exists()
        )

    def test_partial_table_at_0010_fails_before_creating_other_tables(self):
        from django.db.migrations.executor import MigrationExecutor

        self._migrate('0010_personal_safety_profile')
        with self.connection.cursor() as cursor:
            cursor.execute(
                'CREATE TABLE "SafetyApp_sosnotificationattempt" '
                '("id" integer PRIMARY KEY)'
            )
            cursor.execute(
                'INSERT INTO "SafetyApp_sosnotificationattempt" ("id") '
                'VALUES (77)'
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot safely adopt existing SOS table",
        ):
            MigrationExecutor(self.connection).migrate([
                ('SafetyApp', '0011_sos_cross_process_leases'),
            ])

        with self.connection.cursor() as cursor:
            tables = set(self.connection.introspection.table_names(cursor))
            description = self.connection.introspection.get_table_description(
                cursor,
                'SafetyApp_sosnotificationattempt',
            )
            cursor.execute(
                'SELECT "id" FROM "SafetyApp_sosnotificationattempt"'
            )
            rows = cursor.fetchall()
        self.assertEqual(
            {'id'},
            {column.name for column in description},
        )
        self.assertEqual([(77,)], rows)
        self.assertNotIn('SafetyApp_soscontactacknowledgement', tables)
        self.assertNotIn('SafetyApp_sosescalationstate', tables)

    def test_partial_later_table_is_detected_before_creating_earlier_missing_table(self):
        from django.db.migrations.executor import MigrationExecutor

        self._migrate('0010_personal_safety_profile')
        with self.connection.cursor() as cursor:
            cursor.execute(
                'CREATE TABLE "SafetyApp_soscontactacknowledgement" '
                '("id" integer PRIMARY KEY)'
            )
            cursor.execute(
                'INSERT INTO "SafetyApp_soscontactacknowledgement" ("id") '
                'VALUES (88)'
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot safely adopt existing SOS table",
        ):
            MigrationExecutor(self.connection).migrate([
                ('SafetyApp', '0011_sos_cross_process_leases'),
            ])

        with self.connection.cursor() as cursor:
            tables = set(self.connection.introspection.table_names(cursor))
            cursor.execute(
                'SELECT "id" FROM "SafetyApp_soscontactacknowledgement"'
            )
            rows = cursor.fetchall()
        self.assertNotIn('SafetyApp_sosnotificationattempt', tables)
        self.assertNotIn('SafetyApp_sosescalationstate', tables)
        self.assertIn('SafetyApp_soscontactacknowledgement', tables)
        self.assertEqual([(88,)], rows)

    def test_missing_tables_are_rejected_without_transactional_ddl(self):
        from django.db import NotSupportedError
        from SafetyApp.migration_support_0011 import (
            ensure_sos_workflow_schema,
        )

        self._migrate('0010_personal_safety_profile')
        apps11 = self._state_apps('0011_sos_cross_process_leases')
        features = self.connection.features
        original_can_rollback_ddl = features.can_rollback_ddl
        features.can_rollback_ddl = False
        try:
            with self.connection.schema_editor() as schema_editor:
                with self.assertRaisesRegex(
                    NotSupportedError,
                    'cannot be rolled back atomically',
                ):
                    ensure_sos_workflow_schema(
                        self._workflow_models(apps11),
                        schema_editor,
                    )
        finally:
            features.can_rollback_ddl = original_can_rollback_ddl

        with self.connection.cursor() as cursor:
            tables = set(self.connection.introspection.table_names(cursor))
        self.assertNotIn('SafetyApp_sosnotificationattempt', tables)
        self.assertNotIn('SafetyApp_soscontactacknowledgement', tables)
        self.assertNotIn('SafetyApp_sosescalationstate', tables)

    def test_failure_creating_second_missing_table_rolls_back_0011(self):
        from django.db.backends.base.schema import BaseDatabaseSchemaEditor
        from django.db.migrations.executor import MigrationExecutor
        from django.db.migrations.recorder import MigrationRecorder

        self._migrate('0010_personal_safety_profile')
        original_create_model = BaseDatabaseSchemaEditor.create_model
        create_count = [0]

        def fail_on_second_create(schema_editor, model):
            create_count[0] += 1
            if create_count[0] == 2:
                raise RuntimeError('injected table creation failure')
            return original_create_model(schema_editor, model)

        with patch.object(
            BaseDatabaseSchemaEditor,
            'create_model',
            new=fail_on_second_create,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                'injected table creation failure',
            ):
                MigrationExecutor(self.connection).migrate([
                    ('SafetyApp', '0011_sos_cross_process_leases'),
                ])

        self.assertEqual(2, create_count[0])
        with self.connection.cursor() as cursor:
            tables = set(self.connection.introspection.table_names(cursor))
        for table_name in (
            'SafetyApp_sosnotificationattempt',
            'SafetyApp_soscontactacknowledgement',
            'SafetyApp_sosescalationstate',
        ):
            self.assertNotIn(table_name, tables)
        self.assertNotIn(
            ('SafetyApp', '0011_sos_cross_process_leases'),
            MigrationRecorder(self.connection).applied_migrations(),
        )

    def test_failure_creating_second_missing_table_rolls_back_0013(self):
        from django.db.backends.base.schema import BaseDatabaseSchemaEditor
        from django.db.migrations.executor import MigrationExecutor
        from django.db.migrations.recorder import MigrationRecorder

        self._migrate('0010_personal_safety_profile')
        self._record_0011()
        self._migrate('0012_emergency_contact_consent_verification')
        original_create_model = BaseDatabaseSchemaEditor.create_model
        create_count = [0]

        def fail_on_second_create(schema_editor, model):
            create_count[0] += 1
            if create_count[0] == 2:
                raise RuntimeError('injected table creation failure')
            return original_create_model(schema_editor, model)

        with patch.object(
            BaseDatabaseSchemaEditor,
            'create_model',
            new=fail_on_second_create,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                'injected table creation failure',
            ):
                MigrationExecutor(self.connection).migrate([
                    ('SafetyApp', '0013_sos_schema_compatibility'),
                ])

        self.assertEqual(2, create_count[0])
        with self.connection.cursor() as cursor:
            tables = set(self.connection.introspection.table_names(cursor))
        for table_name in (
            'SafetyApp_sosnotificationattempt',
            'SafetyApp_soscontactacknowledgement',
            'SafetyApp_sosescalationstate',
        ):
            self.assertNotIn(table_name, tables)
        self.assertNotIn(
            ('SafetyApp', '0013_sos_schema_compatibility'),
            MigrationRecorder(self.connection).applied_migrations(),
        )

    def test_recorded_0011_with_no_tables_is_repaired_by_0013(self):
        self._migrate('0010_personal_safety_profile')
        self._record_0011()
        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)

    def test_recorded_0011_with_matching_tables_preserves_rows(self):
        self._migrate('0010_personal_safety_profile')
        apps11 = self._state_apps('0011_sos_cross_process_leases')
        contact, event = self._create_base_rows(apps11)
        models = self._workflow_models(apps11)
        self._create_workflow_tables(models)
        rows = self._create_workflow_rows(apps11, contact, event)
        self._record_0011()

        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)
        for model_name, row in zip(
            (
                'SOSNotificationAttempt',
                'SOSContactAcknowledgement',
                'SOSEscalationState',
            ),
            rows,
        ):
            self.assertTrue(
                apps.get_model('SafetyApp', model_name)
                .objects.filter(pk=row.pk).exists()
            )

    def test_recorded_0011_with_one_missing_table_repairs_only_missing(self):
        self._migrate('0010_personal_safety_profile')
        apps11 = self._state_apps('0011_sos_cross_process_leases')
        contact, event = self._create_base_rows(apps11)
        models = self._workflow_models(apps11)
        self._create_workflow_tables(models[:2])
        rows = self._create_workflow_rows(
            apps11,
            contact,
            event,
            include_state=False,
        )[:2]
        self._record_0011()

        apps = self._migrate('0013_sos_schema_compatibility')
        self._assert_workflow_schema(apps)
        for model_name, row in zip(
            ('SOSNotificationAttempt', 'SOSContactAcknowledgement'),
            rows,
        ):
            self.assertTrue(
                apps.get_model('SafetyApp', model_name)
                .objects.filter(pk=row.pk).exists()
            )

    def test_recorded_0011_partial_table_fails_without_alteration(self):
        self._migrate('0010_personal_safety_profile')
        with self.connection.cursor() as cursor:
            cursor.execute(
                'CREATE TABLE "SafetyApp_sosnotificationattempt" '
                '("id" integer PRIMARY KEY)'
            )
            cursor.execute(
                'INSERT INTO "SafetyApp_sosnotificationattempt" ("id") '
                'VALUES (77)'
            )
        self._record_0011()

        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot safely adopt existing SOS table",
        ):
            self._migrate('0013_sos_schema_compatibility')

        with self.connection.cursor() as cursor:
            description = self.connection.introspection.get_table_description(
                cursor,
                'SafetyApp_sosnotificationattempt',
            )
            cursor.execute(
                'SELECT "id" FROM "SafetyApp_sosnotificationattempt"'
            )
            rows = cursor.fetchall()
        self.assertEqual({'id'}, {column.name for column in description})
        self.assertEqual([(77,)], rows)

    def test_active_event_index_is_portable_and_enforced(self):
        from django.db import IntegrityError, NotSupportedError
        migration_0008 = importlib.import_module(
            'SafetyApp.migrations.0008_sos_unique_active_event',
        )

        self.assertTrue(migration_0008._supports_partial_indexes(
            self.connection,
        ))
        self.assertFalse(migration_0008._supports_partial_indexes(
            SimpleNamespace(vendor='mysql'),
        ))
        self.assertFalse(migration_0008._supports_partial_indexes(
            SimpleNamespace(
                vendor='sqlite',
                Database=SimpleNamespace(sqlite_version_info=(3, 7, 17)),
            ),
        ))
        self.assertTrue(migration_0008._supports_partial_indexes(
            SimpleNamespace(vendor='postgresql'),
        ))
        with self.assertRaises(NotSupportedError):
            migration_0008.create_active_event_index(
                None,
                SimpleNamespace(connection=SimpleNamespace(vendor='mysql')),
            )

        apps = self._migrate('0013_sos_schema_compatibility')
        event_model = apps.get_model('SafetyApp', 'SOSEvent')
        event_model.objects.create(user='synthetic-active-user')
        with self.assertRaises(IntegrityError):
            event_model.objects.create(user='synthetic-active-user')
        event_model.objects.create(
            user='synthetic-active-user',
            status='CANCELLED',
        )
        event_model.objects.create(
            user='synthetic-active-user',
            status='RESOLVED',
        )
        history_user = 'synthetic-history-user'
        event_model.objects.create(
            user=history_user,
            status='CANCELLED',
        )
        event_model.objects.create(
            user=history_user,
            status='RESOLVED',
        )
        event_model.objects.create(user=history_user, status='ACTIVE')
        self.assertEqual(
            3,
            event_model.objects.filter(user=history_user).count(),
        )
        self.assertEqual(
            1,
            event_model.objects.filter(
                user=history_user,
                status='ACTIVE',
            ).count(),
        )

        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = %s",
                ['safetyapp_sos_active_user_unique'],
            )
            index_sql = cursor.fetchone()[0]
            constraints = self.connection.introspection.get_constraints(
                cursor,
                event_model._meta.db_table,
            )
        self.assertIn('WHERE "status" = \'ACTIVE\'', index_sql)
        self.assertTrue(
            constraints['safetyapp_sos_active_user_unique']['unique']
        )

    def test_active_event_index_refuses_duplicates_without_deleting_rows(self):
        import importlib

        from django.db import IntegrityError
        from django.db.migrations.executor import MigrationExecutor
        migration_0008 = importlib.import_module(
            'SafetyApp.migrations.0008_sos_unique_active_event',
        )

        self._migrate('0007_sos_email_delivery_outcomes')
        apps = self._state_apps('0008_sos_unique_active_event')
        event_model = apps.get_model('SafetyApp', 'SOSEvent')
        event_model.objects.bulk_create([
            event_model(user='duplicate-active-user', status='ACTIVE'),
            event_model(user='duplicate-active-user', status='ACTIVE'),
        ])
        self.assertTrue(callable(migration_0008.create_active_event_index))
        with self.assertRaisesRegex(
            IntegrityError,
            'existing duplicate ACTIVE events',
        ):
            MigrationExecutor(self.connection).migrate([
                ('SafetyApp', '0008_sos_unique_active_event'),
            ])
        self.assertEqual(
            2,
            event_model.objects.filter(
                user='duplicate-active-user',
                status='ACTIVE',
            ).count(),
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                'SELECT COUNT(*) FROM sqlite_master '
                'WHERE type = %s AND name = %s',
                ['index', 'safetyapp_sos_active_user_unique'],
            )
            self.assertEqual(0, cursor.fetchone()[0])


def _run_isolated_tests():
    global _TEMP_ROOT

    temp_root_obj = tempfile.TemporaryDirectory(
        prefix='phase6cc-tests-',
    )
    _TEMP_ROOT = Path(temp_root_obj.name).resolve()
    project_env = (Path(__file__).resolve().parents[1] / '.env').resolve()
    original_connect = sqlite3.connect
    original_open = builtins.open
    original_io_open = io.open

    def path_for(value):
        try:
            return Path(os.fspath(value)).resolve()
        except (TypeError, ValueError):
            return None

    def guarded_open(file, *args, **kwargs):
        if path_for(file) == project_env:
            raise RuntimeError('.env access is blocked')
        return original_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):
        if path_for(file) == project_env:
            raise RuntimeError('.env access is blocked')
        return original_io_open(file, *args, **kwargs)

    def guarded_connect(database, *args, **kwargs):
        path = path_for(database)
        if (
            path is None
            or os.path.commonpath([str(_TEMP_ROOT), str(path)])
            != str(_TEMP_ROOT)
        ):
            raise RuntimeError(
                'SQLite path outside disposable test root: {}'.format(path)
            )
        return original_connect(database, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_io_open
    sqlite3.connect = guarded_connect
    try:
        from django.conf import settings

        settings.configure(
            SECRET_KEY='phase-6c-c-isolated-tests',
            INSTALLED_APPS=['SafetyApp'],
            DATABASES={
                'default': {
                    'ENGINE': 'django.db.backends.sqlite3',
                    'NAME': str(_TEMP_ROOT / 'bootstrap.sqlite3'),
                },
            },
            USE_TZ=True,
            DEFAULT_AUTO_FIELD='django.db.models.AutoField',
        )
        import django
        django.setup()

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            MigrationCompatibilityTests,
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        from django.db import connections
        connections.close_all()
        return 0 if result.wasSuccessful() else 1
    finally:
        builtins.open = original_open
        io.open = original_io_open
        sqlite3.connect = original_connect
        temp_root_obj.cleanup()
        print('TEMP_CLEANED {}'.format(not _TEMP_ROOT.exists()))


if __name__ == '__main__':
    raise SystemExit(_run_isolated_tests())
