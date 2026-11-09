"""
Frozen implementation for the SOS workflow schema migrations.

Do not change this module to alter application behavior. Migration 0011 and
0013 import it directly so later edits to the general compatibility helper
cannot change historical schema adoption or repair behavior. Any intentional
schema-contract change belongs in a new migration support module/version.
The migration compatibility test pins this file's SHA-256; do not update that
pin for an in-place behavior change.
"""

import re

from django.db.migrations.operations.base import Operation
from django.db.utils import NotSupportedError


SOS_WORKFLOW_MODEL_NAMES = (
    'SOSNotificationAttempt',
    'SOSContactAcknowledgement',
    'SOSEscalationState',
)


def _normalize_type(value):
    return ' '.join(str(value).lower().split())


def _normalize_check(value):
    value = re.sub(r'^\s*CHECK\s*', '', str(value), flags=re.IGNORECASE)
    return ''.join(
        character.lower()
        for character in value
        if character not in '"`()[] \t\r\n'
    )


def _expected_schema(model, connection):
    columns = {}
    primary_key = set()
    unique = set()
    indexes = set()
    foreign_keys = set()
    check_constraints = set()

    for field in model._meta.local_fields:
        columns[field.column] = (
            _normalize_type(field.db_type(connection)),
            bool(field.null),
        )
        if field.primary_key:
            primary_key.add((field.column,))
        if field.unique and not field.primary_key:
            unique.add((field.column,))
        if field.db_index and not field.unique and not field.primary_key:
            indexes.add((field.column,))
        if field.remote_field:
            target_model = field.remote_field.model
            target_field = field.target_field
            foreign_keys.add((
                field.column,
                target_model._meta.db_table,
                target_field.column,
            ))
        check = field.db_check(connection)
        if check:
            check_constraints.add(_normalize_check(check))

    for field_names in model._meta.unique_together:
        unique.add(tuple(
            model._meta.get_field(field_name).column
            for field_name in field_names
        ))

    for index in model._meta.indexes:
        indexes.add(tuple(
            model._meta.get_field(field_name.lstrip('-')).column
            for field_name in index.fields
        ))

    return {
        'columns': columns,
        'primary_key': primary_key,
        'unique': unique,
        'indexes': indexes,
        'foreign_keys': foreign_keys,
        'check_constraints': check_constraints,
    }


def _actual_schema(connection, cursor, table_name):
    description = connection.introspection.get_table_description(cursor, table_name)
    constraints = connection.introspection.get_constraints(cursor, table_name)
    check_constraints = set()

    if connection.vendor == 'postgresql':
        cursor.execute(
            'SELECT pg_get_constraintdef(constraint_row.oid) '
            'FROM pg_constraint AS constraint_row '
            'JOIN pg_class AS table_row '
            'ON table_row.oid = constraint_row.conrelid '
            'JOIN pg_namespace AS namespace_row '
            'ON namespace_row.oid = table_row.relnamespace '
            'WHERE table_row.relname = %s '
            'AND namespace_row.nspname = current_schema() '
            "AND constraint_row.contype = 'c'",
            [table_name],
        )
        check_constraints.update(
            _normalize_check(row[0])
            for row in cursor.fetchall()
        )
    elif connection.vendor == 'sqlite':
        cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = %s",
            [table_name],
        )
        row = cursor.fetchone()
        if row and re.search(r'\bCHECK\s*\(', row[0], re.IGNORECASE):
            check_constraints.add('<unrecognized SQLite check constraint>')
    else:
        check_constraints.update(
            '<check constraint definition unavailable>'
            for constraint in constraints.values()
            if constraint.get('check')
        )

    return {
        'columns': {
            column.name: (
                _normalize_type(column.type_code),
                bool(column.null_ok),
            )
            for column in description
        },
        'primary_key': {
            tuple(constraint['columns'])
            for constraint in constraints.values()
            if constraint.get('primary_key')
        },
        'unique': {
            tuple(constraint['columns'])
            for constraint in constraints.values()
            if constraint.get('unique') and not constraint.get('primary_key')
        },
        'indexes': {
            tuple(constraint['columns'])
            for constraint in constraints.values()
            if constraint.get('index') and not constraint.get('unique')
            and not constraint.get('primary_key')
        },
        'foreign_keys': {
            (
                constraint['columns'][0],
                constraint['foreign_key'][0],
                constraint['foreign_key'][1],
            )
            for constraint in constraints.values()
            if constraint.get('foreign_key')
        },
        'check_constraints': check_constraints,
    }


def _schema_mismatches(model, expected, actual):
    mismatches = []
    expected_columns = expected['columns']
    actual_columns = actual['columns']

    missing_columns = sorted(set(expected_columns) - set(actual_columns))
    extra_columns = sorted(set(actual_columns) - set(expected_columns))
    if missing_columns:
        mismatches.append('missing columns {}'.format(', '.join(missing_columns)))
    if extra_columns:
        mismatches.append('unexpected columns {}'.format(', '.join(extra_columns)))

    for column in sorted(set(expected_columns) & set(actual_columns)):
        if expected_columns[column] != actual_columns[column]:
            expected_type, expected_null = expected_columns[column]
            actual_type, actual_null = actual_columns[column]
            if expected_type != actual_type:
                mismatches.append(
                    '{} has type {}, expected {}'.format(
                        column, actual_type, expected_type,
                    )
                )
            if expected_null != actual_null:
                mismatches.append(
                    '{} nullability is {}, expected {}'.format(
                        column, actual_null, expected_null,
                    )
                )

    for key, label in (
        ('primary_key', 'primary key'),
        ('unique', 'unique constraints'),
        ('indexes', 'indexes'),
        ('foreign_keys', 'foreign keys'),
    ):
        missing = expected[key] - actual[key]
        extra = actual[key] - expected[key]
        if missing:
            mismatches.append('missing {} {}'.format(label, sorted(missing)))
        if extra:
            mismatches.append('unexpected {} {}'.format(label, sorted(extra)))

    missing_checks = (
        expected['check_constraints'] - actual['check_constraints']
    )
    extra_checks = (
        actual['check_constraints'] - expected['check_constraints']
    )
    if missing_checks:
        mismatches.append(
            'missing check constraints {}'.format(sorted(missing_checks))
        )
    if extra_checks:
        mismatches.append(
            'unexpected check constraints {}'.format(sorted(extra_checks))
        )

    return mismatches


def ensure_sos_workflow_schema(models, schema_editor):
    connection = schema_editor.connection
    expected = {}

    with connection.cursor() as cursor:
        existing_tables = set(connection.introspection.table_names(cursor))
        missing_models = []

        for model in models:
            table_name = model._meta.db_table
            if table_name in existing_tables:
                expected[model] = _expected_schema(model, connection)
                actual = _actual_schema(connection, cursor, table_name)
                mismatches = _schema_mismatches(
                    model, expected[model], actual,
                )
                if mismatches:
                    raise RuntimeError(
                        "Cannot safely adopt existing SOS table '{}': {}. "
                        "No SOS table was changed. Back up the database and "
                        "reconcile this schema before retrying.".format(
                            table_name, '; '.join(mismatches),
                        )
                    )
            else:
                missing_models.append(model)

    if missing_models and not connection.features.can_rollback_ddl:
        raise NotSupportedError(
            'Cannot create missing SOS workflow tables on backend {!r}: '
            'schema DDL cannot be rolled back atomically.'.format(
                connection.vendor,
            )
        )

    for model in missing_models:
        schema_editor.create_model(model)


class EnsureSOSWorkflowSchema(Operation):
    serialization_expand_args = ['state_operations']
    reduces_to_sql = False
    atomic = True

    def __init__(self, state_operations=None):
        self.state_operations = state_operations or []

    def deconstruct(self):
        return (
            self.__class__.__name__,
            [],
            {'state_operations': self.state_operations}
            if self.state_operations else {},
        )

    def state_forwards(self, app_label, state):
        for operation in self.state_operations:
            operation.state_forwards(app_label, state)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        models = [
            to_state.apps.get_model(app_label, model_name)
            for model_name in SOS_WORKFLOW_MODEL_NAMES
        ]
        models = [
            model for model in models
            if self.allow_migrate_model(schema_editor.connection.alias, model)
        ]
        ensure_sos_workflow_schema(models, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Adopted tables may contain user data, so reversing state never drops them.
        return None

    def describe(self):
        return 'Create missing SOS workflow tables and validate existing tables'
