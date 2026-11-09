"""
Compatibility accessors for the frozen SOS workflow migration implementation.

Migrations import `migration_support_0011` directly. Keep this wrapper
independent so application/test behavior can change without rewriting
historical migrations.
"""

from SafetyApp.migration_support_0011 import (
    EnsureSOSWorkflowSchema,
    SOS_WORKFLOW_MODEL_NAMES,
    _actual_schema,
    _expected_schema,
    _schema_mismatches,
    ensure_sos_workflow_schema,
)

__all__ = [
    'EnsureSOSWorkflowSchema',
    'SOS_WORKFLOW_MODEL_NAMES',
    'ensure_sos_workflow_schema',
]
