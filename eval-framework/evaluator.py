"""
Evaluation engine — combines:
  1. Baseline checks against known-minimum ground truth (Option 4)
  2. Relative head-to-head comparison between methods (Option 3)

The key insight: we DON'T need to know the full correct answer.
We need to know:
  - Did both methods find the things we're SURE about? (baseline)
  - Between the two, which found more? (relative quantity)
  - What did each find that the other didn't? (exclusive findings)
  - Which was cheaper? (efficiency)
"""

from __future__ import annotations

try:
    from .models import (
        BaselineScores, Category, CategorySummary, ComparisonResult,
        EvaluationReport, ExclusiveFinding, ExecutionResult, FindingVerdict,
        Method, RelativeComparison, TestCase,
    )
except ImportError:
    from models import (
        BaselineScores, Category, CategorySummary, ComparisonResult,
        EvaluationReport, ExclusiveFinding, ExecutionResult, FindingVerdict,
        Method, RelativeComparison, TestCase,
    )


def normalize(item: str) -> str:
    """Normalize for fuzzy matching."""
    return item.strip().lower()


# ─── Baseline Scoring (against known-minimum ground truth) ───────────────────


def score_baseline(result: ExecutionResult, test_case: TestCase) -> BaselineScores:
    """
    Check the result against known-minimum ground truth.
    
    This is NOT an F1 score. It's a set of PASS/FAIL checks:
    - Did it find all must_include items?
    - Did it avoid must_exclude items?
    - Did it meet minimum item count?
    """

    gt = test_case.ground_truth
    actual = {normalize(i) for i in result.answer_items}
    must_include = {normalize(i) for i in gt.must_include}
    must_exclude = {normalize(i) for i in gt.must_exclude}

    # Check must_include
    found = sorted(must_include & actual)
    missed = sorted(must_include - actual)

    # Check must_exclude violations
    violations = sorted(must_exclude & actual)

    # Check minimum count
    item_count = len(result.answer_items)
    meets_minimum = item_count >= gt.minimum_count

    return BaselineScores(
        must_include_found=found,
        must_include_missed=missed,
        must_include_pass=len(missed) == 0,
        must_exclude_violations=violations,
        must_exclude_pass=len(violations) == 0,
        item_count=item_count,
        meets_minimum=meets_minimum,
        hallucination_count=len(result.unsupported_claims),
    )


# ─── Relative Comparison (head-to-head, no ground truth needed) ──────────────


def compare_relative(
    graph_result: ExecutionResult,
    tools_result: ExecutionResult,
) -> RelativeComparison:
    """
    Compare the two methods directly against each other.
    
    This doesn't need ground truth at all — it just asks:
    - What did each find?
    - What's unique to each?
    - What do they agree on?
    - Which was cheaper?
    """

    graph_items = {normalize(i) for i in graph_result.answer_items}
    tools_items = {normalize(i) for i in tools_result.answer_items}

    # Set operations
    agreed = sorted(graph_items & tools_items)
    graph_only = sorted(graph_items - tools_items)
    tools_only = sorted(tools_items - graph_items)

    # Build exclusive finding objects (unverified until human reviews)
    graph_exclusive = [
        ExclusiveFinding(item=item, found_by=Method.GRAPH_ASSISTED)
        for item in graph_only
    ]
    tools_exclusive = [
        ExclusiveFinding(item=item, found_by=Method.TOOLS_ONLY)
        for item in tools_only
    ]

    # Token costs
    graph_tokens = graph_result.total_tokens_in + graph_result.total_tokens_out
    tools_tokens = tools_result.total_tokens_in + tools_result.total_tokens_out
    token_ratio = tools_tokens / graph_tokens if graph_tokens > 0 else 0.0

    return RelativeComparison(
        graph_exclusive=graph_exclusive,
        tools_exclusive=tools_exclusive,
        agreed_items=agreed,
        graph_item_count=len(graph_result.answer_items),
        tools_item_count=len(tools_result.answer_items),
        overlap_count=len(agreed),
        graph_total_tokens=graph_tokens,
        tools_total_tokens=tools_tokens,
        token_efficiency_ratio=round(token_ratio, 3),
        graph_queries_used=graph_result.graph_queries,
        graph_grep_used=graph_result.grep_calls,
        tools_grep_used=tools_result.grep_calls,
        tools_file_reads_used=tools_result.file_reads,
    )


# ─── Winner Determination ────────────────────────────────────────────────────


def determine_winner(
    graph_baseline: BaselineScores,
    tools_baseline: BaselineScores,
    relative: RelativeComparison,
) -> tuple[str, str]:
    """
    Determine the winner using a priority-ordered rubric:
    
    1. If one method fails baseline and the other passes → the passer wins
    2. If both pass/fail baseline equally → compare exclusive findings
    3. If exclusive findings are similar → compare total items found
    4. If total items are similar → cheaper method wins
    5. If all else is equal → tie
    
    Returns (winner, reason).
    """

    # Priority 1: Baseline failures
    graph_baseline_ok = graph_baseline.must_include_pass and graph_baseline.must_exclude_pass
    tools_baseline_ok = tools_baseline.must_include_pass and tools_baseline.must_exclude_pass

    if graph_baseline_ok and not tools_baseline_ok:
        return "graph", "Tools failed baseline checks (missed must_include or returned must_exclude items)"
    if tools_baseline_ok and not graph_baseline_ok:
        return "tools", "Graph failed baseline checks (missed must_include or returned must_exclude items)"

    # Priority 2: Exclusive findings quantity
    # More exclusive findings = found things the other missed
    graph_exclusive_count = len(relative.graph_exclusive)
    tools_exclusive_count = len(relative.tools_exclusive)

    if graph_exclusive_count > tools_exclusive_count * 2 and graph_exclusive_count >= 3:
        return "graph", f"Graph found {graph_exclusive_count} exclusive items vs {tools_exclusive_count} for tools"
    if tools_exclusive_count > graph_exclusive_count * 2 and tools_exclusive_count >= 3:
        return "tools", f"Tools found {tools_exclusive_count} exclusive items vs {graph_exclusive_count} for graph"

    # Priority 3: Total item count (more = more thorough)
    count_diff = relative.graph_item_count - relative.tools_item_count
    if count_diff > 3:
        return "graph", f"Graph found {relative.graph_item_count} items vs {relative.tools_item_count} for tools"
    if count_diff < -3:
        return "tools", f"Tools found {relative.tools_item_count} items vs {relative.graph_item_count} for graph"

    # Priority 4: Token efficiency (cheaper wins when results are similar)
    if relative.token_efficiency_ratio > 1.5:
        return "graph", f"Similar results but graph was {relative.token_efficiency_ratio:.1f}x more token-efficient"
    if relative.token_efficiency_ratio < 0.67:
        return "tools", f"Similar results but tools was {1/relative.token_efficiency_ratio:.1f}x more token-efficient"

    # Priority 5: Tie
    return "tie", "Both methods produced similar results at similar cost"


# ─── Full Comparison ─────────────────────────────────────────────────────────


def compare_results(
    test_case: TestCase,
    graph_result: ExecutionResult,
    tools_result: ExecutionResult,
) -> ComparisonResult:
    """Run the full evaluation pipeline for one test case."""

    graph_baseline = score_baseline(graph_result, test_case)
    tools_baseline = score_baseline(tools_result, test_case)
    relative = compare_relative(graph_result, tools_result)
    winner, reason = determine_winner(graph_baseline, tools_baseline, relative)

    return ComparisonResult(
        test_case=test_case,
        graph_result=graph_result,
        tools_result=tools_result,
        graph_baseline=graph_baseline,
        tools_baseline=tools_baseline,
        relative=relative,
        winner=winner,
        winner_reason=reason,
    )


# ─── Report Generation ───────────────────────────────────────────────────────


def generate_report(comparisons: list[ComparisonResult]) -> EvaluationReport:
    """Generate aggregate report from all comparisons."""

    if not comparisons:
        return EvaluationReport()

    total = len(comparisons)
    graph_wins = sum(1 for c in comparisons if c.winner == "graph")
    tools_wins = sum(1 for c in comparisons if c.winner == "tools")
    ties = sum(1 for c in comparisons if c.winner == "tie")
    inconclusive = sum(1 for c in comparisons if c.winner == "inconclusive")

    total_graph_tokens = sum(c.relative.graph_total_tokens for c in comparisons)
    total_tools_tokens = sum(c.relative.tools_total_tokens for c in comparisons)
    overall_ratio = total_tools_tokens / total_graph_tokens if total_graph_tokens > 0 else 0.0

    total_graph_exclusive = sum(len(c.relative.graph_exclusive) for c in comparisons)
    total_tools_exclusive = sum(len(c.relative.tools_exclusive) for c in comparisons)
    total_agreed = sum(c.relative.overlap_count for c in comparisons)

    # Per-category summaries
    categories: dict[Category, list[ComparisonResult]] = {}
    for c in comparisons:
        categories.setdefault(c.test_case.category, []).append(c)

    category_summaries = []
    for cat, cases in sorted(categories.items(), key=lambda x: x[0].value):
        n = len(cases)
        summary = CategorySummary(
            category=cat,
            num_cases=n,
            graph_wins=sum(1 for c in cases if c.winner == "graph"),
            tools_wins=sum(1 for c in cases if c.winner == "tools"),
            ties=sum(1 for c in cases if c.winner == "tie"),
            inconclusive=sum(1 for c in cases if c.winner == "inconclusive"),
            graph_must_include_pass_rate=sum(
                1 for c in cases if c.graph_baseline.must_include_pass
            ) / n,
            tools_must_include_pass_rate=sum(
                1 for c in cases if c.tools_baseline.must_include_pass
            ) / n,
            avg_graph_item_count=sum(c.relative.graph_item_count for c in cases) / n,
            avg_tools_item_count=sum(c.relative.tools_item_count for c in cases) / n,
            avg_overlap=sum(c.relative.overlap_count for c in cases) / n,
            avg_token_efficiency_ratio=sum(
                c.relative.token_efficiency_ratio for c in cases
            ) / n,
            total_graph_exclusive=sum(len(c.relative.graph_exclusive) for c in cases),
            total_tools_exclusive=sum(len(c.relative.tools_exclusive) for c in cases),
            graph_exclusive_verified_correct=sum(
                sum(1 for f in c.relative.graph_exclusive if f.verdict == FindingVerdict.CORRECT)
                for c in cases
            ),
            tools_exclusive_verified_correct=sum(
                sum(1 for f in c.relative.tools_exclusive if f.verdict == FindingVerdict.CORRECT)
                for c in cases
            ),
        )
        category_summaries.append(summary)

    recommendations = _generate_recommendations(category_summaries, comparisons)

    return EvaluationReport(
        total_cases=total,
        graph_wins=graph_wins,
        tools_wins=tools_wins,
        ties=ties,
        inconclusive=inconclusive,
        category_summaries=category_summaries,
        comparisons=comparisons,
        total_graph_tokens=total_graph_tokens,
        total_tools_tokens=total_tools_tokens,
        overall_token_efficiency_ratio=round(overall_ratio, 3),
        total_graph_exclusive_findings=total_graph_exclusive,
        total_tools_exclusive_findings=total_tools_exclusive,
        total_agreed_findings=total_agreed,
        recommendations=recommendations,
    )


def _generate_recommendations(
    summaries: list[CategorySummary],
    comparisons: list[ComparisonResult],
) -> list[str]:
    """Generate actionable recommendations."""

    recs = []

    # Categories where graph dominates
    strong_graph = [s for s in summaries if s.graph_wins > s.tools_wins and s.num_cases >= 2]
    if strong_graph:
        cats = ", ".join(s.category.value for s in strong_graph)
        recs.append(f"GRAPH RECOMMENDED for: {cats}")

    # Categories where graph adds nothing
    no_value = [s for s in summaries if s.graph_wins == 0 and s.tools_wins >= s.num_cases / 2]
    if no_value:
        cats = ", ".join(s.category.value for s in no_value)
        recs.append(f"SKIP GRAPH for: {cats} — tools-only is sufficient")

    # High agreement categories (both methods find same things)
    high_agreement = [s for s in summaries
                      if s.avg_overlap > max(s.avg_graph_item_count, s.avg_tools_item_count) * 0.8
                      and s.num_cases >= 2]
    if high_agreement:
        cats = ", ".join(s.category.value for s in high_agreement)
        recs.append(f"REDUNDANT for: {cats} — both methods find the same things. Use whichever is cheaper.")

    # Exclusive findings worth investigating
    graph_exclusive_total = sum(s.total_graph_exclusive for s in summaries)
    tools_exclusive_total = sum(s.total_tools_exclusive for s in summaries)
    if graph_exclusive_total > 0:
        recs.append(f"REVIEW NEEDED: Graph found {graph_exclusive_total} exclusive items across all cases. "
                    "Verify these with 'review-exclusive' command to confirm correctness.")

    # Token savings
    cheap_graph = [s for s in summaries if s.avg_token_efficiency_ratio > 1.5 and s.num_cases >= 2]
    if cheap_graph:
        cats = ", ".join(s.category.value for s in cheap_graph)
        recs.append(f"COST SAVINGS: Graph is significantly cheaper for: {cats}")

    if not recs:
        recs.append("Insufficient data for recommendations. Run more test cases.")

    return recs
