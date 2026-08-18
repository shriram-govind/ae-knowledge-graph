"""
Report generator — produces human-readable evaluation reports.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from datetime import datetime

try:
    from .models import CategorySummary, ComparisonResult, EvaluationReport, FindingVerdict
    from .evaluator import generate_report
except ImportError:
    from models import CategorySummary, ComparisonResult, EvaluationReport, FindingVerdict
    from evaluator import generate_report


def render_markdown_report(report: EvaluationReport) -> str:
    """Render the evaluation report as Markdown."""

    lines = []
    lines.append("# Knowledge Graph Evaluation Report")
    lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ─── Executive Summary ────────────────────────────────────────────────
    lines.append("## Executive Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total test cases | {report.total_cases} |")
    lines.append(f"| Graph wins | **{report.graph_wins}** |")
    lines.append(f"| Tools wins | **{report.tools_wins}** |")
    lines.append(f"| Ties | {report.ties} |")
    lines.append(f"| Inconclusive | {report.inconclusive} |")
    lines.append(f"| Total Graph Tokens | {report.total_graph_tokens:,} |")
    lines.append(f"| Total Tools Tokens | {report.total_tools_tokens:,} |")
    lines.append(f"| Token Efficiency (tools/graph) | {report.overall_token_efficiency_ratio:.2f}x |")
    lines.append(f"| Graph-exclusive findings | {report.total_graph_exclusive_findings} |")
    lines.append(f"| Tools-exclusive findings | {report.total_tools_exclusive_findings} |")
    lines.append(f"| Agreed findings (both found) | {report.total_agreed_findings} |")
    lines.append("")

    # ─── Per-Category Breakdown ──────────────────────────────────────────
    lines.append("## Per-Category Results")
    lines.append("")
    lines.append("| Category | Cases | Graph | Tools | Tie | Graph Items | Tools Items | Overlap | Token Ratio | Graph-Only | Tools-Only |")
    lines.append("|----------|-------|-------|-------|-----|-------------|-------------|---------|-------------|------------|------------|")

    for s in report.category_summaries:
        lines.append(
            f"| {s.category.value} | {s.num_cases} | {s.graph_wins} | {s.tools_wins} | "
            f"{s.ties} | {s.avg_graph_item_count:.1f} | {s.avg_tools_item_count:.1f} | "
            f"{s.avg_overlap:.1f} | {s.avg_token_efficiency_ratio:.2f}x | "
            f"{s.total_graph_exclusive} | {s.total_tools_exclusive} |"
        )
    lines.append("")

    # ─── Recommendations ─────────────────────────────────────────────────
    lines.append("## Recommendations")
    lines.append("")
    for rec in report.recommendations:
        lines.append(f"- {rec}")
    lines.append("")

    # ─── Individual Cases ─────────────────────────────────────────────────
    lines.append("## Individual Cases")
    lines.append("")

    for comp in report.comparisons:
        tc = comp.test_case
        icon = {"graph": "🟢", "tools": "🔵", "tie": "⚪", "inconclusive": "❓"}.get(comp.winner, "⚪")

        lines.append(f"### {tc.id} {icon} → **{comp.winner}**")
        lines.append(f"**Category:** {tc.category.value} | "
                     f"**Expected:** {tc.expected_graph_advantage.value}")
        lines.append(f"**Scenario:** {tc.scenario}")
        lines.append(f"**Reason:** {comp.winner_reason}")
        lines.append("")

        # Baseline checks
        lines.append("**Baseline checks:**")
        g_pass = "✅" if comp.graph_baseline.must_include_pass else "❌"
        t_pass = "✅" if comp.tools_baseline.must_include_pass else "❌"
        lines.append(f"- Must-include: Graph {g_pass} | Tools {t_pass}")
        if comp.graph_baseline.must_include_missed:
            lines.append(f"  - Graph missed: {', '.join(comp.graph_baseline.must_include_missed)}")
        if comp.tools_baseline.must_include_missed:
            lines.append(f"  - Tools missed: {', '.join(comp.tools_baseline.must_include_missed)}")
        lines.append("")

        # Relative comparison
        lines.append("**Head-to-head:**")
        lines.append(f"| | Graph | Tools |")
        lines.append(f"|---|---|---|")
        lines.append(f"| Items found | {comp.relative.graph_item_count} | {comp.relative.tools_item_count} |")
        lines.append(f"| Tokens | {comp.relative.graph_total_tokens:,} | {comp.relative.tools_total_tokens:,} |")
        lines.append(f"| Exclusive findings | {len(comp.relative.graph_exclusive)} | {len(comp.relative.tools_exclusive)} |")
        lines.append(f"| Agreed items | {comp.relative.overlap_count} | {comp.relative.overlap_count} |")
        lines.append("")

        if comp.relative.graph_exclusive:
            lines.append(f"**Graph found (tools didn't):** {', '.join(f.item for f in comp.relative.graph_exclusive[:8])}")
            if len(comp.relative.graph_exclusive) > 8:
                lines.append(f"  ... +{len(comp.relative.graph_exclusive) - 8} more")
        if comp.relative.tools_exclusive:
            lines.append(f"**Tools found (graph didn't):** {', '.join(f.item for f in comp.relative.tools_exclusive[:8])}")
            if len(comp.relative.tools_exclusive) > 8:
                lines.append(f"  ... +{len(comp.relative.tools_exclusive) - 8} more")
        if comp.relative.agreed_items:
            lines.append(f"**Both found:** {', '.join(comp.relative.agreed_items[:8])}")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def render_csv(report: EvaluationReport) -> str:
    """Render as CSV for spreadsheet analysis."""

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "test_case_id", "category", "expected_advantage", "winner", "winner_reason",
        "graph_must_include_pass", "tools_must_include_pass",
        "graph_items", "tools_items", "overlap", "graph_exclusive", "tools_exclusive",
        "graph_tokens", "tools_tokens", "token_ratio",
        "graph_tool_calls", "tools_tool_calls",
        "graph_queries", "graph_hallucinations", "tools_hallucinations",
    ])

    for comp in report.comparisons:
        writer.writerow([
            comp.test_case.id,
            comp.test_case.category.value,
            comp.test_case.expected_graph_advantage.value,
            comp.winner,
            comp.winner_reason,
            comp.graph_baseline.must_include_pass,
            comp.tools_baseline.must_include_pass,
            comp.relative.graph_item_count,
            comp.relative.tools_item_count,
            comp.relative.overlap_count,
            len(comp.relative.graph_exclusive),
            len(comp.relative.tools_exclusive),
            comp.relative.graph_total_tokens,
            comp.relative.tools_total_tokens,
            comp.relative.token_efficiency_ratio,
            comp.graph_result.total_tool_calls,
            comp.tools_result.total_tool_calls,
            comp.graph_result.graph_queries,
            comp.graph_baseline.hallucination_count,
            comp.tools_baseline.hallucination_count,
        ])

    return output.getvalue()


def save_report(report: EvaluationReport, output_dir: Path):
    """Save report in multiple formats."""
    output_dir.mkdir(parents=True, exist_ok=True)

    md_path = output_dir / "evaluation-report.md"
    md_path.write_text(render_markdown_report(report))

    csv_path = output_dir / "evaluation-data.csv"
    csv_path.write_text(render_csv(report))

    try:
        from .models import save_report as save_json_report
    except ImportError:
        from models import save_report as save_json_report
    json_path = output_dir / "evaluation-report.json"
    save_json_report(report, json_path)

    return {
        "markdown": md_path,
        "csv": csv_path,
        "json": json_path,
    }
