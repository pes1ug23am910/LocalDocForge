"""Pipeline lifecycle shared by every operation."""

from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)

__all__ = ["CandidateOutput", "ExecuteResult", "PipelineError", "run_pipeline"]
