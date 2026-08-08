from __future__ import annotations

import pytest

from ozon_v2.domain.models import WorkbenchAction, WorkbenchState
from ozon_v2.domain.state_machine import transition_workbench_state


def test_locked_ozon_products_open_supplier_review_without_template_browser_gate() -> None:
    assert transition_workbench_state(
        WorkbenchState.OZON_COLLECTED,
        WorkbenchAction.OPEN_SUPPLIER_REVIEW,
    ) is WorkbenchState.SUPPLIER_REVIEW


def test_new_pipeline_cannot_start_legacy_ozon_attribute_template_collection() -> None:
    with pytest.raises(ValueError):
        transition_workbench_state(
            WorkbenchState.OZON_COLLECTED,
            WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION,
        )
