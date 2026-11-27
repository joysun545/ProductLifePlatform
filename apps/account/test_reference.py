"""Restore account reference rows needed after a TransactionTestCase flush."""

from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection


def restore_account_test_reference():
    schema = SimpleNamespace(connection=connection)
    for name, operation in (
        ("0002_seed_account_reference_data", "seed_account_reference_data"),
        ("0003_add_factory_production_role", "add_factory_production_role"),
        ("0004_add_factory_aftersales_role", "add_role"),
        ("0005_add_factory_chief_engineer_role", "add_role"),
        ("0006_factory_warehouse_roles", "add_warehouse_roles"),
        ("0012_senior_manager_delegation", "seed_senior_manager_roles_and_invitation_rules"),
    ):
        migration = import_module(f"account.migrations.{name}")
        getattr(migration, operation)(apps, schema)
