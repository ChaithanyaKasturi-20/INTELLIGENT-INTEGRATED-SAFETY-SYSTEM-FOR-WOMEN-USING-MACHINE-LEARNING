from django.db import migrations
from django.db.utils import IntegrityError, NotSupportedError


INDEX_NAME = 'safetyapp_sos_active_user_unique'
ACTIVE_STATUS = 'ACTIVE'


def _supports_partial_indexes(connection):
    if connection.vendor == 'sqlite':
        return connection.Database.sqlite_version_info >= (3, 8, 0)
    if connection.vendor == 'postgresql':
        return True
    return False


def create_active_event_index(apps, schema_editor):
    connection = schema_editor.connection
    if not _supports_partial_indexes(connection):
        raise NotSupportedError(
            'SafetyApp requires partial unique indexes to enforce one active '
            'SOS event per user; backend {!r} is not supported.'.format(
                connection.vendor,
            )
        )

    event_model = apps.get_model('SafetyApp', 'SOSEvent')
    quote_name = schema_editor.quote_name
    table_name = quote_name(event_model._meta.db_table)
    user_column = quote_name(event_model._meta.get_field('user').column)
    status_column = quote_name(event_model._meta.get_field('status').column)

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT COUNT(*) FROM ('
            'SELECT {user_column} FROM {table_name} '
            'WHERE {status_column} = %s '
            'GROUP BY {user_column} HAVING COUNT(*) > 1'
            ') duplicate_active_users'.format(
                user_column=user_column,
                table_name=table_name,
                status_column=status_column,
            ),
            [ACTIVE_STATUS],
        )
        duplicate_user_count = cursor.fetchone()[0]
    if duplicate_user_count:
        raise IntegrityError(
            'Cannot create the active SOS event unique index: existing '
            'duplicate ACTIVE events must be reviewed and resolved without '
            'deleting data.'
        )

    schema_editor.execute(
        'CREATE UNIQUE INDEX {} ON {} ({}) WHERE {} = {}'.format(
            quote_name(INDEX_NAME),
            table_name,
            user_column,
            status_column,
            "'{}'".format(ACTIVE_STATUS),
        )
    )


def drop_active_event_index(apps, schema_editor):
    quote_name = schema_editor.quote_name
    schema_editor.execute(
        'DROP INDEX {}'.format(quote_name(INDEX_NAME))
    )


class Migration(migrations.Migration):
    dependencies = [
        ('SafetyApp', '0007_sos_email_delivery_outcomes'),
    ]

    operations = [
        migrations.RunPython(
            create_active_event_index,
            reverse_code=drop_active_event_index,
        ),
    ]
