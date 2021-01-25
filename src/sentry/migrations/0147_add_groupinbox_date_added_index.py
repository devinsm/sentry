# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2021-01-05 22:14

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    # This flag is used to mark that a migration shouldn't be automatically run in
    # production. We set this to True for operations that we think are risky and want
    # someone from ops to run manually and monitor.
    # General advice is that if in doubt, mark your migration as `is_dangerous`.
    # Some things you should always mark as dangerous:
    # - Large data migrations. Typically we want these to be run manually by ops so that
    #   they can be monitored. Since data migrations will now hold a transaction open
    #   this is even more important.
    # - Adding columns to highly active tables, even ones that are NULL.
    is_dangerous = True

    # This flag is used to decide whether to run this migration in a transaction or not.
    # By default we prefer to run in a transaction, but for migrations where you want
    # to `CREATE INDEX CONCURRENTLY` this needs to be set to False. Typically you'll
    # want to create an index concurrently when adding one to an existing table.
    atomic = False

    dependencies = [
        ("sentry", "0146_backfill_members_alert_write"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS sentry_groupinbox_date_added_f113c11b
                    ON sentry_groupinbox (date_added);
                    """,
                    reverse_sql="""
                    DROP INDEX CONCURRENTLY IF EXISTS sentry_groupinbox_date_added_f113c11b;
                    """,
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="groupinbox",
                    name="date_added",
                    field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
            ],
        ),
    ]
