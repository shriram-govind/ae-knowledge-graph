#!/usr/bin/env python3
"""
Knowledge Graph Evaluation CLI

Usage:
    # List available test cases
    python -m eval-framework.cli list

    # Show prompt for a specific test case and method
    python -m eval-framework.cli prompt --case transitive-deps-001 --method graph_assisted

    # Record a result (after observing an agent session)
    python -m eval-framework.cli record --case transitive-deps-001 --method graph_assisted \\
        --answers "epex,actor-executor,observability,ae" \\
        --tokens-in 5000 --tokens-out 3000 --tool-calls 8

    # Evaluate all recorded results and generate report
    python -m eval-framework.cli report --output ./results/

    # Run interactive evaluation mode (guides you through each case)
    python -m eval-framework.cli interactive
"""

import argparse
import json
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (
    Category,
    EvaluationReport,
    ExecutionResult,
    Method,
    TestCase,
    load_test_cases,
)
from evaluator import compare_results, generate_report
from runner import EvalRunner, RunnerConfig, generate_prompt, record_execution
from report import render_markdown_report, save_report


DEFAULT_CASES_PATH = Path(__file__).parent / "test-cases" / "seed-cases.json"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "results"
RECORDED_RESULTS_DIR = Path(__file__).parent / "recorded-results"


def cmd_list(args):
    """List available test cases."""
    cases = load_test_cases(Path(args.cases))
    print(f"\n{'ID':<25} {'Category':<20} {'Expected Advantage':<12} Scenario")
    print(f"{'─'*25} {'─'*20} {'─'*12} {'─'*50}")
    for c in cases:
        scenario_short = c.scenario[:50] + "..." if len(c.scenario) > 50 else c.scenario
        print(f"{c.id:<25} {c.category.value:<20} {c.expected_graph_advantage.value:<12} {scenario_short}")
    print(f"\nTotal: {len(cases)} test cases")


def cmd_prompt(args):
    """Show the prompt for a test case."""
    cases = load_test_cases(Path(args.cases))
    case = next((c for c in cases if c.id == args.case), None)
    if not case:
        print(f"Error: Test case '{args.case}' not found.")
        sys.exit(1)

    method = Method(args.method)
    prompt = generate_prompt(case, method)

    print(f"\n{'='*70}")
    print(f"Test Case: {case.id}")
    print(f"Method: {method.value}")
    print(f"{'='*70}")
    print(prompt)
    print(f"{'='*70}\n")

    # Also show ground truth for reference
    print("Ground Truth (for evaluator reference):")
    print(f"  Expected items: {case.ground_truth.expected_items}")
    if case.ground_truth.excluded_items:
        print(f"  Excluded items: {case.ground_truth.excluded_items}")
    print(f"  Summary: {case.ground_truth.summary}")
    print()


def cmd_record(args):
    """Record execution results."""
    RECORDED_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    answer_items = [a.strip() for a in args.answers.split(",") if a.strip()]

    result = record_execution(
        test_case_id=args.case,
        method=Method(args.method),
        answer_items=answer_items,
        answer_summary=args.summary or "",
        confidence=args.confidence,
        tokens_in=args.tokens_in,
        tokens_out=args.tokens_out,
        wall_time_ms=args.wall_time or 0,
        unsupported_claims=[c.strip() for c in (args.hallucinations or "").split(",") if c.strip()],
        tool_calls=[{"tool_name": t, "input_summary": "", "output_summary": ""} for t in
                    (args.tools_used or "").split(",") if t.strip()],
    )

    # Save to file
    filename = f"{args.case}_{args.method}.json"
    output_path = RECORDED_RESULTS_DIR / filename

    from models import _serialize
    output_path.write_text(json.dumps(_serialize(result), indent=2))
    print(f"✓ Recorded result: {output_path}")


def cmd_report(args):
    """Generate evaluation report from recorded results."""
    cases = load_test_cases(Path(args.cases))
    case_map = {c.id: c for c in cases}

    results_dir = RECORDED_RESULTS_DIR
    if not results_dir.exists():
        print("Error: No recorded results found. Run 'record' first.")
        sys.exit(1)

    # Load all recorded results
    comparisons = []
    for case in cases:
        graph_file = results_dir / f"{case.id}_graph_assisted.json"
        tools_file = results_dir / f"{case.id}_tools_only.json"

        if graph_file.exists() and tools_file.exists():
            graph_data = json.loads(graph_file.read_text())
            tools_data = json.loads(tools_file.read_text())

            graph_result = ExecutionResult(
                method=Method.GRAPH_ASSISTED,
                test_case_id=case.id,
                answer_items=graph_data.get("answer_items", []),
                answer_summary=graph_data.get("answer_summary", ""),
                total_tokens_in=graph_data.get("total_tokens_in", 0),
                total_tokens_out=graph_data.get("total_tokens_out", 0),
                total_tool_calls=graph_data.get("total_tool_calls", 0),
                graph_queries=graph_data.get("graph_queries", 0),
                grep_calls=graph_data.get("grep_calls", 0),
                file_reads=graph_data.get("file_reads", 0),
                confidence=graph_data.get("confidence", 0.5),
                unsupported_claims=graph_data.get("unsupported_claims", []),
            )

            tools_result = ExecutionResult(
                method=Method.TOOLS_ONLY,
                test_case_id=case.id,
                answer_items=tools_data.get("answer_items", []),
                answer_summary=tools_data.get("answer_summary", ""),
                total_tokens_in=tools_data.get("total_tokens_in", 0),
                total_tokens_out=tools_data.get("total_tokens_out", 0),
                total_tool_calls=tools_data.get("total_tool_calls", 0),
                graph_queries=tools_data.get("graph_queries", 0),
                grep_calls=tools_data.get("grep_calls", 0),
                file_reads=tools_data.get("file_reads", 0),
                confidence=tools_data.get("confidence", 0.5),
                unsupported_claims=tools_data.get("unsupported_claims", []),
            )

            comparison = compare_results(case, graph_result, tools_result)
            comparisons.append(comparison)

    if not comparisons:
        print("Error: No complete comparisons found (need both graph + tools results for a case).")
        sys.exit(1)

    report = generate_report(comparisons)

    output_dir = Path(args.output)
    paths = save_report(report, output_dir)

    print(f"\n✓ Report generated:")
    for fmt, path in paths.items():
        print(f"  {fmt}: {path}")

    # Also print summary to console
    print(f"\n{'='*60}")
    print(f"SUMMARY: {report.total_cases} cases evaluated")
    print(f"  Graph wins: {report.graph_wins}")
    print(f"  Tools wins: {report.tools_wins}")
    print(f"  Ties: {report.ties}")
    print(f"  Token ratio (tools/graph): {report.overall_token_efficiency_ratio:.2f}x")
    print(f"  Graph-exclusive findings: {report.total_graph_exclusive_findings}")
    print(f"  Tools-exclusive findings: {report.total_tools_exclusive_findings}")
    print(f"  Agreed findings: {report.total_agreed_findings}")
    print(f"{'='*60}\n")


def cmd_interactive(args):
    """Interactive mode - guides through each test case."""
    cases = load_test_cases(Path(args.cases))

    if args.category:
        cases = [c for c in cases if c.category.value == args.category]

    print(f"\n🧪 Knowledge Graph Evaluation — Interactive Mode")
    print(f"   {len(cases)} test cases to evaluate\n")
    print("For each case, you'll run the scenario twice (graph-assisted, then tools-only)")
    print("and record the results.\n")

    for i, case in enumerate(cases, 1):
        print(f"\n{'='*70}")
        print(f"Case {i}/{len(cases)}: {case.id}")
        print(f"Category: {case.category.value}")
        print(f"Expected graph advantage: {case.expected_graph_advantage.value}")
        print(f"{'='*70}")
        print(f"\nScenario: {case.scenario}\n")
        print("Ground truth items:", case.ground_truth.expected_items)
        print()

        # Show prompts
        print("── STEP 1: Run with GRAPH ASSISTED ──")
        print("Use this prompt in a Kiro session with graph access:")
        print(f"  {case.scenario}")
        print()

        input("Press Enter when you've recorded the graph-assisted result...")

        print("\n── STEP 2: Run with TOOLS ONLY ──")
        print("Use this prompt in a Kiro session WITHOUT graph access:")
        print(f"  {case.scenario}")
        print()

        input("Press Enter when you've recorded the tools-only result...")

        print(f"\n✓ Case {case.id} complete. Record results with:")
        print(f'  python -m eval-framework.cli record --case {case.id} --method graph_assisted --answers "item1,item2" --tokens-in N --tokens-out N')
        print(f'  python -m eval-framework.cli record --case {case.id} --method tools_only --answers "item1,item2" --tokens-in N --tokens-out N')
        print()

    print("\n✓ All cases complete! Generate report with:")
    print("  python -m eval-framework.cli report")


def main():
    parser = argparse.ArgumentParser(
        description="Knowledge Graph Evaluation Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # list
    p_list = subparsers.add_parser("list", help="List available test cases")
    p_list.add_argument("--cases", default=str(DEFAULT_CASES_PATH))

    # prompt
    p_prompt = subparsers.add_parser("prompt", help="Show prompt for a test case")
    p_prompt.add_argument("--case", required=True, help="Test case ID")
    p_prompt.add_argument("--method", required=True, choices=["graph_assisted", "tools_only"])
    p_prompt.add_argument("--cases", default=str(DEFAULT_CASES_PATH))

    # record
    p_record = subparsers.add_parser("record", help="Record execution results")
    p_record.add_argument("--case", required=True, help="Test case ID")
    p_record.add_argument("--method", required=True, choices=["graph_assisted", "tools_only"])
    p_record.add_argument("--answers", required=True, help="Comma-separated answer items")
    p_record.add_argument("--summary", help="Free-text answer summary")
    p_record.add_argument("--confidence", type=float, default=0.5)
    p_record.add_argument("--tokens-in", type=int, default=0)
    p_record.add_argument("--tokens-out", type=int, default=0)
    p_record.add_argument("--wall-time", type=int, default=0, help="Wall time in ms")
    p_record.add_argument("--tools-used", help="Comma-separated tool names used")
    p_record.add_argument("--hallucinations", help="Comma-separated unsupported claims")
    p_record.add_argument("--cases", default=str(DEFAULT_CASES_PATH))

    # report
    p_report = subparsers.add_parser("report", help="Generate evaluation report")
    p_report.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    p_report.add_argument("--cases", default=str(DEFAULT_CASES_PATH))

    # interactive
    p_interactive = subparsers.add_parser("interactive", help="Interactive evaluation mode")
    p_interactive.add_argument("--category", help="Filter by category")
    p_interactive.add_argument("--cases", default=str(DEFAULT_CASES_PATH))

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "list": cmd_list,
        "prompt": cmd_prompt,
        "record": cmd_record,
        "report": cmd_report,
        "interactive": cmd_interactive,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
