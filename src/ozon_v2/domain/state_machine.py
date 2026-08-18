from __future__ import annotations

from ozon_v2.domain.models import RunStatus, WorkbenchAction, WorkbenchState


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.NEEDS_QUERY_GENERATION, RunStatus.READY_FOR_OZON_COLLECTION, RunStatus.FAILED},
    RunStatus.NEEDS_QUERY_GENERATION: {RunStatus.READY_FOR_OZON_COLLECTION, RunStatus.FAILED},
    RunStatus.READY_FOR_OZON_COLLECTION: {RunStatus.INGESTED, RunStatus.FAILED},
    RunStatus.INGESTED: {RunStatus.FINALIZED, RunStatus.FAILED},
    RunStatus.FINALIZED: set(),
    RunStatus.FAILED: set(),
}


def can_transition(current: RunStatus, next_status: RunStatus) -> bool:
    return next_status in ALLOWED_TRANSITIONS[current]


def assert_transition(current: RunStatus, next_status: RunStatus) -> None:
    if not can_transition(current, next_status):
        raise ValueError(f"invalid run status transition: {current.value} -> {next_status.value}")


WORKBENCH_TRANSITIONS: dict[WorkbenchState, dict[WorkbenchAction, WorkbenchState]] = {
    WorkbenchState.CREATED: {
        WorkbenchAction.CHECK_CREDENTIALS: WorkbenchState.NEEDS_CREDENTIALS,
        WorkbenchAction.START_DEDUPE: WorkbenchState.DEDUPING_STORE,
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.NEEDS_CREDENTIALS: {
        WorkbenchAction.SAVE_CREDENTIALS: WorkbenchState.CREATED,
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.DEDUPING_STORE: {
        WorkbenchAction.START_DEDUPE: WorkbenchState.DEDUPING_STORE,
        WorkbenchAction.MARK_STORE_DEDUPED: WorkbenchState.STORE_DEDUPED,
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.STORE_DEDUPED: {
        WorkbenchAction.SELECT_SEEDS: WorkbenchState.SEED_SELECTED,
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.SEED_SELECTED: {
        WorkbenchAction.GENERATE_OZON_QUERIES: WorkbenchState.SEED_SELECTED,
        WorkbenchAction.START_OZON_COLLECTION: WorkbenchState.OZON_COLLECTING,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING: {
        WorkbenchAction.MARK_ATTRIBUTE_TEMPLATE_COLLECTED: WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
    },
    WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED: {
        WorkbenchAction.OPEN_SUPPLIER_REVIEW: WorkbenchState.SUPPLIER_REVIEW,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.OZON_COLLECTING: {
        WorkbenchAction.MARK_OZON_COLLECTED: WorkbenchState.OZON_COLLECTED,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
    },
    WorkbenchState.OZON_COLLECTED: {
        WorkbenchAction.OPEN_SUPPLIER_REVIEW: WorkbenchState.SUPPLIER_REVIEW,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.SUPPLIER_REVIEW: {
        WorkbenchAction.START_SUPPLIER_COLLECTION: WorkbenchState.SUPPLIER_COLLECTING,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.SUPPLIER_COLLECTING: {
        WorkbenchAction.MARK_SUPPLIER_COLLECTED: WorkbenchState.SUPPLIER_COLLECTED,
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.SUPPLIER_SEARCHING: {
        WorkbenchAction.MARK_SUPPLIER_COLLECTED: WorkbenchState.SUPPLIER_COLLECTED,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
    },
    WorkbenchState.SUPPLIER_COLLECTED: {
        WorkbenchAction.START_IMAGE_PROCESSING: WorkbenchState.IMAGE_PROCESSING,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.SAME_PRODUCT_REVIEW: {
        WorkbenchAction.APPROVE_SAME_PRODUCT: WorkbenchState.AI_FILLING,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.AI_FILLING: {
        WorkbenchAction.START_IMAGE_PROCESSING: WorkbenchState.IMAGE_PROCESSING,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.IMAGE_PROCESSING: {
        WorkbenchAction.BUILD_DRAFT: WorkbenchState.DRAFT_BUILDING,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.DRAFT_BUILDING: {
        WorkbenchAction.MARK_DRAFT_READY: WorkbenchState.DRAFT_READY,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.DRAFT_READY: {
        WorkbenchAction.REQUEST_PUBLISH: WorkbenchState.PUBLISH_WAITING_CONFIRMATION,
        WorkbenchAction.MARK_NEEDS_MANUAL_REVIEW: WorkbenchState.NEEDS_MANUAL_REVIEW,
    },
    WorkbenchState.PUBLISH_WAITING_CONFIRMATION: {
        WorkbenchAction.MARK_PUBLISH_SUBMITTED: WorkbenchState.PUBLISH_SUBMITTED,
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.PUBLISH_SUBMITTED: {
        WorkbenchAction.MARK_DONE: WorkbenchState.DONE,
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
    },
    WorkbenchState.NEEDS_MANUAL_REVIEW: {
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.NEEDS_SLIDER: {
        WorkbenchAction.MARK_FAILED_RETRYABLE: WorkbenchState.FAILED_RETRYABLE,
        WorkbenchAction.MARK_FAILED_BLOCKED: WorkbenchState.FAILED_BLOCKED,
    },
    WorkbenchState.FAILED_RETRYABLE: {
        WorkbenchAction.RETRY_FAILED: WorkbenchState.CREATED,
    },
    WorkbenchState.FAILED_BLOCKED: {},
    WorkbenchState.DONE: {},
}


def allowed_workbench_actions(state: WorkbenchState, publish_locked: bool = True) -> list[WorkbenchAction]:
    transitions = WORKBENCH_TRANSITIONS[state]
    actions = list(transitions)
    if publish_locked and WorkbenchAction.REQUEST_PUBLISH in actions:
        actions.remove(WorkbenchAction.REQUEST_PUBLISH)
    return actions


def transition_workbench_state(current: WorkbenchState, action: WorkbenchAction) -> WorkbenchState:
    transitions = WORKBENCH_TRANSITIONS[current]
    if action not in transitions:
        raise ValueError(f"invalid workbench action: {current.value} + {action.value}")
    return transitions[action]
