"""
Core data models for the evaluation framework.

Defines test cases, execution results, and evaluation metrics.

Ground Truth Philosophy:
    We use a "known minimum + relative comparison" approach rather than
    claiming to know the complete correct answer. Ground truth defines:
    - must_include: items we're SURE must be in any correct answer
    - must_exclude: items we're SURE must NOT be in a correct answer
    - minimum_count: minimum number of items a good answer should have
    
    Beyond that, the two methods are compared RELATIVE to each other:
    which found more, which found exclusive items, which was cheaper.
    A human reviewer judges exclusive findings for correctness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional


# ─── Enums ───────────────────────────────────────────────────────────────────


class Category(str, Enum):
    """Types of questions that can be asked against the codebase."""

    BLAST_RADIUS = "blast_radius"
    TRANSITIVE_DEPS = "transitive_deps"
    PRESENCE_CHECK = "presence_check"
    OWNERSHIP = "ownership"
    CROSS_LANGUAGE = "cross_language"
    FEATURE_TOGGLE = "feature_toggle"
    API_SURFACE = "api_surface"
    TEST_COVERAGE = "test_coverage"
    DEPENDENCY_CHAIN = "dependency_chain"


class ExpectedGraphAdvantage(str, Enum):
    """How much the graph is expected to help vs tools-only."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class Method(str, Enum):
    """The two analysis methods being compared."""

    GRAPH_ASSISTED = "graph_assisted"
    TOOLS_ONLY = "tools_only"


class FindingVerdict(str, Enum):
    """Human verdict on an exclusive finding."""

    CORRECT = "correct"  # Finding is valid and useful
    INCORRECT = "incorrect"  # Finding is wrong (false positive)
    UNVERIFIED = "unverified"  # Not yet reviewed by human


# ─── Test Case Definition ────────────────────────────────────────────────────


@dataclass
class GroundTruth:
    """
    Known-minimum ground truth.
    
    We don't claim to know the full answer. Instead:
    - must_include: items we're CONFIDENT must appear in any correct answer
    - must_exclude: items we're CONFIDENT are wrong/irrelevant
    - minimum_count: at minimum, a good answer should have this many items
    
    Everything beyond must_include/must_exclude is judged by relative comparison.
    """

    # Items that MUST be present (high confidence these are correct)
    must_include: list[str] = field(default_factory=list)

    # Items that MUST NOT be present (known false positives)
    must_exclude: list[str] = field(default_factory=list)

    # Minimum number of items a good answer should have
    # (even if we can't enumerate them all)
    minimum_count: int = 0

    # Free-text explanation of what a correct answer looks like
    description: str = ""

    # How the must_include items were verified
    verification_method: str = ""

    # Confidence in the must_include items (0.0 - 1.0)
    # 1.0 = manually verified; 0.7 = high confidence from graph/grep
    confidence: float = 1.0


@dataclass
class TestCase:
    """A single evaluation scenario."""

    id: str
    scenario: str  # Natural language question
    category: Category
    ground_truth: GroundTruth
    expected_graph_advantage: ExpectedGraphAdvantage

    # Optional: specific instructions/constraints for the agent
    instructions: str = ""

    # Optional: tags for filtering
    tags: list[str] = field(default_factory=list)


# ─── Execution Results ───────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """A single tool invocation during execution."""

    tool_name: str
    input_summary: str = ""
    output_summary: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    success: bool = True


@dataclass
class ExecutionResult:
    """Result of running one method against one test case."""

    method: Method
    test_case_id: str

    # The answer produced
    answer_items: list[str] = field(default_factory=list)
    answer_summary: str = ""

    # Cost metrics
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tool_calls: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    wall_time_ms: int = 0

    # Tool usage breakdown
    graph_queries: int = 0
    grep_calls: int = 0
    file_reads: int = 0

    # Self-reported confidence
    confidence: float = 0.0

    # Claims made without tool evidence
    unsupported_claims: list[str] = field(default_factory=list)


# ─── Evaluation Metrics ──────────────────────────────────────────────────────


@dataclass
class BaselineScores:
    """
    Scores against the known-minimum ground truth.
    These are PASS/FAIL checks, not percentages.
    """

    # Did it find all must_include items?
    must_include_found: list[str] = field(default_factory=list)
    must_include_missed: list[str] = field(default_factory=list)
    must_include_pass: bool = False  # True if ALL must_include items found

    # Did it avoid must_exclude items?
    must_exclude_violations: list[str] = field(default_factory=list)
    must_exclude_pass: bool = False  # True if NO must_exclude items returned

    # Did it meet minimum count?
    item_count: int = 0
    meets_minimum: bool = False

    # Hallucination count
    hallucination_count: int = 0


@dataclass
class ExclusiveFinding:
    """An item found by one method but not the other."""

    item: str
    found_by: Method
    verdict: FindingVerdict = FindingVerdict.UNVERIFIED


@dataclass
class RelativeComparison:
    """
    Head-to-head comparison between the two methods.
    This is where the real insight lives — no ground truth needed.
    """

    # Items found by graph but NOT by tools
    graph_exclusive: list[ExclusiveFinding] = field(default_factory=list)

    # Items found by tools but NOT by graph
    tools_exclusive: list[ExclusiveFinding] = field(default_factory=list)

    # Items found by BOTH (agreement = likely correct)
    agreed_items: list[str] = field(default_factory=list)

    # Quantity comparison
    graph_item_count: int = 0
    tools_item_count: int = 0
    overlap_count: int = 0

    # Cost comparison
    graph_total_tokens: int = 0
    tools_total_tokens: int = 0
    token_efficiency_ratio: float = 0.0  # tools/graph, >1 = graph cheaper

    # Tool usage comparison
    graph_queries_used: int = 0
    graph_grep_used: int = 0
    tools_grep_used: int = 0
    tools_file_reads_used: int = 0


@dataclass
class ComparisonResult:
    """Full comparison of graph-assisted vs tools-only for one test case."""

    test_case: TestCase

    # Raw results
    graph_result: ExecutionResult
    tools_result: ExecutionResult

    # Baseline checks (against known-minimum ground truth)
    graph_baseline: BaselineScores
    tools_baseline: BaselineScores

    # Head-to-head relative comparison (the main signal)
    relative: RelativeComparison

    # Overall verdict
    winner: str = ""  # "graph" | "tools" | "tie" | "inconclusive"
    winner_reason: str = ""  # Human-readable explanation of why


# ─── Aggregate Report ────────────────────────────────────────────────────────


@dataclass
class CategorySummary:
    """Aggregate metrics for a single category."""

    category: Category
    num_cases: int = 0
    graph_wins: int = 0
    tools_wins: int = 0
    ties: int = 0
    inconclusive: int = 0

    # Baseline pass rates
    graph_must_include_pass_rate: float = 0.0
    tools_must_include_pass_rate: float = 0.0

    # Relative metrics (averaged)
    avg_graph_item_count: float = 0.0
    avg_tools_item_count: float = 0.0
    avg_overlap: float = 0.0
    avg_token_efficiency_ratio: float = 0.0

    # Exclusive findings
    total_graph_exclusive: int = 0
    total_tools_exclusive: int = 0
    graph_exclusive_verified_correct: int = 0
    tools_exclusive_verified_correct: int = 0


@dataclass
class EvaluationReport:
    """Final aggregate report."""

    total_cases: int = 0
    graph_wins: int = 0
    tools_wins: int = 0
    ties: int = 0
    inconclusive: int = 0

    category_summaries: list[CategorySummary] = field(default_factory=list)
    comparisons: list[ComparisonResult] = field(default_factory=list)

    # Aggregate cost
    total_graph_tokens: int = 0
    total_tools_tokens: int = 0
    overall_token_efficiency_ratio: float = 0.0

    # Exclusive finding stats
    total_graph_exclusive_findings: int = 0
    total_tools_exclusive_findings: int = 0
    total_agreed_findings: int = 0

    # Key takeaways
    recommendations: list[str] = field(default_factory=list)


# ─── Serialization ───────────────────────────────────────────────────────────


def _serialize(obj) -> dict:
    """Custom serializer for dataclasses with enums."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    return obj


def save_test_cases(cases: list[TestCase], path: Path):
    """Save test cases to JSON."""
    data = [_serialize(c) for c in cases]
    path.write_text(json.dumps(data, indent=2))


def load_test_cases(path: Path) -> list[TestCase]:
    """Load test cases from JSON."""
    data = json.loads(path.read_text())
    cases = []
    for item in data:
        gt_data = item.pop("ground_truth")
        # Handle both old format (expected_items) and new format (must_include)
        if "expected_items" in gt_data and "must_include" not in gt_data:
            gt_data["must_include"] = gt_data.pop("expected_items")
        if "excluded_items" in gt_data and "must_exclude" not in gt_data:
            gt_data["must_exclude"] = gt_data.pop("excluded_items")
        # Drop fields not in the new GroundTruth model
        valid_fields = {f.name for f in GroundTruth.__dataclass_fields__.values()}
        gt_data = {k: v for k, v in gt_data.items() if k in valid_fields}
        gt = GroundTruth(**gt_data)
        item["ground_truth"] = gt
        item["category"] = Category(item["category"])
        item["expected_graph_advantage"] = ExpectedGraphAdvantage(item["expected_graph_advantage"])
        cases.append(TestCase(**item))
    return cases


def save_report(report: EvaluationReport, path: Path):
    """Save evaluation report to JSON."""
    path.write_text(json.dumps(_serialize(report), indent=2))
