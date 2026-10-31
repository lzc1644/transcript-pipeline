from dataclasses import replace
import logging
from types import SimpleNamespace

import pytest

from src.pipeline import STAGE_EXIT_FAILED, STAGE_EXIT_PARTIAL, STAGE_EXIT_SUCCESS, run_stage
from src.pipeline.stages import default_stage_operations


@pytest.mark.parametrize("partial,failed,expected", [(0, 0, STAGE_EXIT_SUCCESS), (1, 0, STAGE_EXIT_PARTIAL), (0, 1, STAGE_EXIT_FAILED)])
def test_src_stage_core_preserves_reference_exit_codes_and_explicit_connection(partial, failed, expected):
    connection = object()
    progress = object()
    seen = []

    def prepare(loaded, *, logger, progress_callback, gateway_connection):
        seen.append((loaded, progress_callback, gateway_connection))
        return SimpleNamespace(partial=partial, failed=failed)

    loaded = object()
    operations = replace(default_stage_operations(), prepare_reference_batch=prepare,
                         summarize_reference_results=lambda result: "fixture")
    code = run_stage("prepare-reference", loaded, logging.getLogger("stage"),
                     prepare_reference_progress_callback=progress,
                     gateway_connection=connection, operations=operations)
    assert code == expected
    assert seen == [(loaded, progress, connection)]


def test_src_stage_refine_uses_explicit_connection_without_loading_project():
    connection = object()
    seen = []
    loaded = SimpleNamespace(settings=SimpleNamespace(llm=SimpleNamespace(backends=["codex_api"])))

    def refine(settings, *, requested_backends, logger, gateway_connection):
        seen.append((settings, requested_backends, gateway_connection))
        return object()

    operations = replace(default_stage_operations(), refine_batch=refine,
                         summarize_refinement_results=lambda result: "fixture")
    assert run_stage("refine", loaded, logging.getLogger("stage"), gateway_connection=connection,
                     operations=operations) == 0
    assert seen == [(loaded, ["codex_api"], connection)]
