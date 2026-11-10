from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database.models import (
    Base,
    LearningModelActivePointerRecord,
    LearningModelRegistryEventRecord,
    LearningModelRegistryRecord,
)
from trading_bot.domain.learning.model_registry import (
    LearningModelRegistryAction,
    LearningModelRegistryState,
    LearningModelTransitionRejected,
    transition_learning_model_state,
)


def test_registry_requires_evaluation_and_human_approval_before_paper_activation() -> None:
    draft = transition_learning_model_state(None, LearningModelRegistryAction.REGISTER)
    evaluated = transition_learning_model_state(
        draft,
        LearningModelRegistryAction.EVALUATE,
        has_evaluation=True,
    )
    approved = transition_learning_model_state(
        evaluated,
        LearningModelRegistryAction.APPROVE,
        has_evaluation=True,
    )
    active = transition_learning_model_state(
        approved,
        LearningModelRegistryAction.ACTIVATE,
        has_evaluation=True,
        approved=True,
    )

    assert active is LearningModelRegistryState.PAPER_ACTIVE


@pytest.mark.parametrize(
    ("action", "has_evaluation", "approved"),
    [
        (LearningModelRegistryAction.EVALUATE, False, False),
        (LearningModelRegistryAction.APPROVE, False, False),
        (LearningModelRegistryAction.ACTIVATE, False, True),
        (LearningModelRegistryAction.ACTIVATE, True, False),
    ],
)
def test_registry_rejects_incomplete_or_unapproved_activation(
    action: LearningModelRegistryAction,
    has_evaluation: bool,
    approved: bool,
) -> None:
    with pytest.raises(LearningModelTransitionRejected):
        transition_learning_model_state(
            LearningModelRegistryState.DRAFT
            if action is LearningModelRegistryAction.EVALUATE
            else LearningModelRegistryState.EVALUATED,
            action,
            has_evaluation=has_evaluation,
            approved=approved,
        )


def test_rejected_model_cannot_be_reactivated_and_rollback_requires_prior_activation() -> None:
    rejected = transition_learning_model_state(
        LearningModelRegistryState.DRAFT,
        LearningModelRegistryAction.REJECT,
    )
    with pytest.raises(LearningModelTransitionRejected):
        transition_learning_model_state(
            rejected,
            LearningModelRegistryAction.ACTIVATE,
            has_evaluation=True,
            approved=True,
        )

    restored = transition_learning_model_state(
        LearningModelRegistryState.RETIRED,
        LearningModelRegistryAction.ROLLBACK,
        has_evaluation=True,
        approved=True,
        previously_active=True,
    )
    assert restored is LearningModelRegistryState.PAPER_ACTIVE


def test_registry_has_no_live_state_or_transition() -> None:
    assert "LIVE" not in {state.value for state in LearningModelRegistryState}
    with pytest.raises(ValueError, match="LIVE"):
        LearningModelRegistryState("LIVE")


def test_registry_tables_keep_workspace_scope_and_explicit_state_allowlist() -> None:
    assert {
        "learning_model_registry",
        "learning_model_registry_event",
        "learning_model_active_pointer",
    } <= set(Base.metadata.tables)

    registry = LearningModelRegistryRecord.__table__
    state_constraint = next(
        constraint
        for constraint in registry.constraints
        if isinstance(constraint, CheckConstraint)
        and "learning_model_registry_state_valid" in constraint.name
    )
    assert all(state.value in str(state_constraint.sqltext) for state in LearningModelRegistryState)
    assert "live" not in str(state_constraint.sqltext).lower()
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns) == ("workspace_id", "artifact_id")
        for constraint in registry.constraints
    )
    assert any(
        isinstance(constraint, ForeignKeyConstraint)
        and [element.parent.name for element in constraint.elements]
        == ["workspace_id", "registry_id"]
        for constraint in LearningModelRegistryEventRecord.__table__.foreign_key_constraints
    )
    assert LearningModelActivePointerRecord.__table__.c.version.nullable is False
