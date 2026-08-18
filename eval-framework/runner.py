"""
Test case runner — executes both graph-assisted and tools-only methods
against a test case and records execution results.

This module is designed to be used by a human or agent running the evaluation.
It provides a structured workflow for executing test cases and recording results.

Usage:
    The runner does NOT automatically call an LLM. Instead, it provides prompts
    and records manually-observed results. This is intentional because:
    1. We want to measure how a real agent (like Kiro) uses the tools
    2. Auto-LLM evaluation introduces self-grading bias
    3. Token costs can only be measured from real agent sessions

    For automated execution, extend this with your LLM API of choice.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from .models import (
        ComparisonResult, ExecutionResult, Method, TestCase, ToolCall, load_test_cases,
    )
    from .evaluator import compare_results
except ImportError:
    from models import (
        ComparisonResult, ExecutionResult, Method, TestCase, ToolCall, load_test_cases,
    )
    from evaluator import compare_results


# ─── Runner Configuration ────────────────────────────────────────────────────


@dataclass
class RunnerConfig:
    """Configuration for the test runner."""

    # Path to test cases JSON
    test_cases_path: Path

    # Output directory for results
    output_dir: Path

    # If set, only run test cases with these IDs
    filter_ids: Optional[list[str]] = None

    # If set, only run test cases with these categories
    filter_categories: Optional[list[str]] = None

    # If set, only run test cases with these tags
    filter_tags: Optional[list[str]] = None


# ─── Prompt Generation ───────────────────────────────────────────────────────


GRAPH_SYSTEM_PROMPT = """You are analyzing a codebase using a Neo4j knowledge graph.
You have access to the following tools:
- read_neo4j_cypher: Execute Cypher queries against the knowledge graph
- get_neo4j_schema: Get the graph schema
- grep: Search file contents
- glob: Find files by name pattern
- read: Read file contents
- shell: Run shell commands

PREFER the knowledge graph for finding relationships, dependencies, and impact analysis.
Use grep/read as supplements when you need actual file contents or the graph lacks data.

Answer the following question and provide a structured list of findings.
"""

TOOLS_SYSTEM_PROMPT = """You are analyzing a codebase using standard development tools.
You have access to the following tools:
- grep: Search file contents
- glob: Find files by name pattern
- read: Read file contents
- shell: Run shell commands (git grep, git ls-files, etc.)

You do NOT have access to any knowledge graph or pre-indexed dependency database.
You must find all information by searching the filesystem directly.

Answer the following question and provide a structured list of findings.
"""


def generate_prompt(test_case: TestCase, method: Method) -> str:
    """Generate the full prompt for a test case execution."""

    system = GRAPH_SYSTEM_PROMPT if method == Method.GRAPH_ASSISTED else TOOLS_SYSTEM_PROMPT

    user_prompt = f"""## Question

{test_case.scenario}

## Instructions

{test_case.instructions if test_case.instructions else "Provide a thorough answer."}

## Output Format

Provide your answer as:
1. A bullet-point list of specific findings (classes, modules, files, etc.)
2. A confidence score (0.0 - 1.0) for your answer
3. Note any claims you made without direct tool evidence

Be exhaustive. List every relevant item you can find.
"""

    return f"{system}\n\n{user_prompt}"


# ─── Result Recording ────────────────────────────────────────────────────────


def record_execution(
    test_case_id: str,
    method: Method,
    answer_items: list[str],
    answer_summary: str = "",
    confidence: float = 0.5,
    tool_calls: Optional[list[dict]] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    wall_time_ms: int = 0,
    unsupported_claims: Optional[list[str]] = None,
) -> ExecutionResult:
    """
    Record the execution result for one method on one test case.

    This is typically called after observing an agent session.

    Args:
        test_case_id: The test case ID
        method: GRAPH_ASSISTED or TOOLS_ONLY
        answer_items: List of specific findings (the structured answer)
        answer_summary: Free-text summary of the answer
        confidence: Self-reported confidence (0.0 - 1.0)
        tool_calls: List of dicts with keys: tool_name, input_summary, output_summary,
                    tokens_in, tokens_out, duration_ms, success
        tokens_in: Total input tokens consumed
        tokens_out: Total output tokens consumed
        wall_time_ms: Total wall time in milliseconds
        unsupported_claims: Any claims made without tool evidence
    """

    parsed_calls = []
    graph_queries = 0
    grep_calls = 0
    file_reads = 0

    if tool_calls:
        for tc in tool_calls:
            call = ToolCall(
                tool_name=tc.get("tool_name", "unknown"),
                input_summary=tc.get("input_summary", ""),
                output_summary=tc.get("output_summary", ""),
                tokens_in=tc.get("tokens_in", 0),
                tokens_out=tc.get("tokens_out", 0),
                duration_ms=tc.get("duration_ms", 0),
                success=tc.get("success", True),
            )
            parsed_calls.append(call)

            # Classify tool calls
            if "neo4j" in call.tool_name or "cypher" in call.tool_name:
                graph_queries += 1
            elif "grep" in call.tool_name:
                grep_calls += 1
            elif "read" in call.tool_name or "glob" in call.tool_name:
                file_reads += 1

    return ExecutionResult(
        method=method,
        test_case_id=test_case_id,
        answer_items=answer_items,
        answer_summary=answer_summary,
        total_tokens_in=tokens_in,
        total_tokens_out=tokens_out,
        total_tool_calls=len(parsed_calls),
        tool_calls=parsed_calls,
        wall_time_ms=wall_time_ms,
        graph_queries=graph_queries,
        grep_calls=grep_calls,
        file_reads=file_reads,
        confidence=confidence,
        unsupported_claims=unsupported_claims or [],
    )


# ─── Runner Workflow ─────────────────────────────────────────────────────────


class EvalRunner:
    """
    Orchestrates the evaluation workflow.

    Typical usage:
        runner = EvalRunner(config)
        cases = runner.load_cases()

        for case in cases:
            # Run graph-assisted (observe agent session, record results)
            prompt_a = runner.get_prompt(case, Method.GRAPH_ASSISTED)
            result_a = runner.record(case, Method.GRAPH_ASSISTED, ...)

            # Run tools-only (observe agent session, record results)
            prompt_b = runner.get_prompt(case, Method.TOOLS_ONLY)
            result_b = runner.record(case, Method.TOOLS_ONLY, ...)

        # Compare and generate report
        report = runner.evaluate()
    """

    def __init__(self, config: RunnerConfig):
        self.config = config
        self.results: dict[str, dict[Method, ExecutionResult]] = {}
        self._cases: Optional[list[TestCase]] = None

    def load_cases(self) -> list[TestCase]:
        """Load and filter test cases."""
        all_cases = load_test_cases(self.config.test_cases_path)

        filtered = all_cases
        if self.config.filter_ids:
            filtered = [c for c in filtered if c.id in self.config.filter_ids]
        if self.config.filter_categories:
            filtered = [c for c in filtered if c.category.value in self.config.filter_categories]
        if self.config.filter_tags:
            filtered = [c for c in filtered
                       if any(t in c.tags for t in self.config.filter_tags)]

        self._cases = filtered
        return filtered

    def get_prompt(self, test_case: TestCase, method: Method) -> str:
        """Get the prompt for a specific test case and method."""
        return generate_prompt(test_case, method)

    def record(
        self,
        test_case: TestCase,
        method: Method,
        **kwargs,
    ) -> ExecutionResult:
        """Record execution result for a test case + method."""
        result = record_execution(test_case_id=test_case.id, method=method, **kwargs)

        if test_case.id not in self.results:
            self.results[test_case.id] = {}
        self.results[test_case.id][method] = result

        return result

    def evaluate(self) -> list[ComparisonResult]:
        """Compare all recorded results and return comparisons."""
        if self._cases is None:
            self._cases = self.load_cases()

        comparisons = []
        case_map = {c.id: c for c in self._cases}

        for case_id, methods in self.results.items():
            if Method.GRAPH_ASSISTED in methods and Method.TOOLS_ONLY in methods:
                case = case_map[case_id]
                comparison = compare_results(
                    test_case=case,
                    graph_result=methods[Method.GRAPH_ASSISTED],
                    tools_result=methods[Method.TOOLS_ONLY],
                )
                comparisons.append(comparison)

        return comparisons

    def save_results(self):
        """Save raw results to the output directory."""
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        for case_id, methods in self.results.items():
            for method, result in methods.items():
                filename = f"{case_id}_{method.value}.json"
                path = self.config.output_dir / filename
                # Convert to dict for JSON serialization
                from .models import _serialize
                path.write_text(json.dumps(_serialize(result), indent=2))
