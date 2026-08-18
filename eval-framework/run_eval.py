#!/usr/bin/env python3
"""
Orchestrator — end-to-end automated evaluation pipeline.

Runs the full workflow:
  1. Load test cases
  2. Execute both methods via kiro-cli (auto_runner)
  3. Extract answers from session output (extractor)
  4. Evaluate and compare (evaluator)
  5. Generate reports (report)

Usage:
    # Run everything (all cases, both methods)
    python run_eval.py --cases test-cases/seed-cases.json

    # Run specific cases
    python run_eval.py --cases test-cases/seed-cases.json --ids transitive-deps-001 presence-check-001

    # Run only one category
    python run_eval.py --cases test-cases/test-cases-md-imported.json --categories blast_radius

    # Skip the kiro-cli execution step (just re-extract and re-evaluate existing sessions)
    python run_eval.py --cases test-cases/seed-cases.json --skip-run

    # Use a specific model
    python run_eval.py --cases test-cases/seed-cases.json --model sonnet
"""

import argparse
import json
import sys
import time
from pathlib import Path

FRAMEWORK_DIR = Path(__file__).parent
sys.path.insert(0, str(FRAMEWORK_DIR))

from models import ExecutionResult, Method, load_test_cases
from evaluator import compare_results, generate_report
from report import save_report
from auto_runner import KiroConfig, run_all_cases, SESSIONS_DIR, DEFAULT_REPO_ROOT
from extractor import extract_all_sessions, save_extracted_results, RECORDED_RESULTS_DIR


def load_recorded_results(cases_file: Path) -> list[tuple]:
    """
    Load recorded results and pair them with test cases.
    Returns list of (test_case, graph_result, tools_result) tuples.
    """

    cases = load_test_cases(cases_file)
    case_map = {c.id: c for c in cases}
    pairs = []

    if not RECORDED_RESULTS_DIR.exists():
        return []

    # Find cases with both methods recorded
    recorded_cases = set()
    for f in RECORDED_RESULTS_DIR.glob("*.json"):
        stem = f.stem
        for method in ["graph_assisted", "tools_only"]:
            if stem.endswith(f"_{method}"):
                case_id = stem[:-(len(method) + 1)]
                recorded_cases.add(case_id)

    for case_id in sorted(recorded_cases):
        graph_file = RECORDED_RESULTS_DIR / f"{case_id}_graph_assisted.json"
        tools_file = RECORDED_RESULTS_DIR / f"{case_id}_tools_only.json"

        if not graph_file.exists() or not tools_file.exists():
            continue
        if case_id not in case_map:
            continue

        graph_data = json.loads(graph_file.read_text())
        tools_data = json.loads(tools_file.read_text())

        graph_result = ExecutionResult(
            method=Method.GRAPH_ASSISTED,
            test_case_id=case_id,
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
            wall_time_ms=graph_data.get("wall_time_ms", 0),
        )

        tools_result = ExecutionResult(
            method=Method.TOOLS_ONLY,
            test_case_id=case_id,
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
            wall_time_ms=tools_data.get("wall_time_ms", 0),
        )

        pairs.append((case_map[case_id], graph_result, tools_result))

    return pairs


def run_pipeline(
    cases_file: Path,
    config: KiroConfig,
    filter_ids: list[str] = None,
    filter_categories: list[str] = None,
    skip_run: bool = False,
    output_dir: Path = None,
):
    """Run the full evaluation pipeline."""

    output_dir = output_dir or (FRAMEWORK_DIR / "results")
    start_time = time.time()

    print("\n" + "="*70)
    print("  🧪 KNOWLEDGE GRAPH EVALUATION — AUTOMATED PIPELINE")
    print("="*70)

    # ─── Step 1: Run sessions (unless skipped) ────────────────────────────
    if not skip_run:
        print("\n📋 Step 1/4: Running Kiro CLI sessions...")
        run_all_cases(
            cases_file=cases_file,
            config=config,
            filter_ids=filter_ids,
            filter_categories=filter_categories,
        )
    else:
        print("\n📋 Step 1/4: Skipped (--skip-run). Using existing sessions.")

    # ─── Step 2: Extract answers from sessions ────────────────────────────
    print("\n📋 Step 2/4: Extracting answers from session output...")
    results = extract_all_sessions()
    if results:
        save_extracted_results(results)
    else:
        print("  ⚠️  No sessions to extract. Checking recorded-results/...")

    # ─── Step 3: Evaluate and compare ─────────────────────────────────────
    print("\n📋 Step 3/4: Evaluating results...")
    pairs = load_recorded_results(cases_file)

    if not pairs:
        print("  ❌ No complete result pairs found (need both graph + tools for at least one case).")
        print("     Ensure sessions ran successfully and produced output.")
        return None

    comparisons = []
    for test_case, graph_result, tools_result in pairs:
        comp = compare_results(test_case, graph_result, tools_result)
        comparisons.append(comp)
        icon = {"graph": "🟢", "tools": "🔵", "tie": "⚪"}.get(comp.winner, "❓")
        print(f"  {icon} {test_case.id}: {comp.winner} — {comp.winner_reason}")

    report = generate_report(comparisons)

    # ─── Step 4: Generate reports ─────────────────────────────────────────
    print("\n📋 Step 4/4: Generating reports...")
    paths = save_report(report, output_dir)

    elapsed = time.time() - start_time

    # ─── Summary ──────────────────────────────────────────────────────────
    print("\n" + "="*70)
    print("  📊 RESULTS")
    print("="*70)
    print(f"\n  Cases evaluated: {report.total_cases}")
    print(f"  Graph wins:      {report.graph_wins}")
    print(f"  Tools wins:      {report.tools_wins}")
    print(f"  Ties:            {report.ties}")
    print(f"  Inconclusive:    {report.inconclusive}")
    print(f"\n  Token efficiency: {report.overall_token_efficiency_ratio:.2f}x (>1 = graph cheaper)")
    print(f"  Graph-exclusive:  {report.total_graph_exclusive_findings} findings")
    print(f"  Tools-exclusive:  {report.total_tools_exclusive_findings} findings")
    print(f"  Agreed:           {report.total_agreed_findings} findings")
    print(f"\n  Elapsed time: {elapsed:.1f}s")
    print(f"\n  Reports saved:")
    for fmt, path in paths.items():
        print(f"    {fmt}: {path}")

    if report.recommendations:
        print(f"\n  💡 Recommendations:")
        for rec in report.recommendations:
            print(f"    • {rec}")

    print("\n" + "="*70 + "\n")

    return report


# ─── CLI Entry Point ─────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end automated knowledge graph evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full automated run (all cases):
  python run_eval.py --cases test-cases/seed-cases.json

  # Run specific cases only:
  python run_eval.py --cases test-cases/seed-cases.json --ids transitive-deps-001 blast-radius-001

  # Re-evaluate existing sessions (skip kiro-cli execution):
  python run_eval.py --cases test-cases/seed-cases.json --skip-run

  # Run with specific model and timeout:
  python run_eval.py --cases test-cases/seed-cases.json --model sonnet --timeout 600
        """,
    )

    parser.add_argument("--cases", type=Path, required=True,
                       help="Path to test cases JSON file")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT,
                       help="Path to ae repo root")
    parser.add_argument("--model", default="",
                       help="LLM model to use (default: Auto)")
    parser.add_argument("--effort", default="high",
                       help="Agent effort level")
    parser.add_argument("--timeout", type=int, default=300,
                       help="Timeout per case in seconds (default: 300)")
    parser.add_argument("--ids", nargs="*",
                       help="Only run specific test case IDs")
    parser.add_argument("--categories", nargs="*",
                       help="Only run specific categories")
    parser.add_argument("--skip-run", action="store_true",
                       help="Skip kiro-cli execution, just extract and evaluate existing sessions")
    parser.add_argument("--output", type=Path, default=None,
                       help="Output directory for reports (default: results/)")
    parser.add_argument("--clean", action="store_true",
                       help="Clean previous sessions and results before running")

    args = parser.parse_args()

    if args.clean:
        import shutil
        for d in [SESSIONS_DIR, RECORDED_RESULTS_DIR]:
            if d.exists():
                shutil.rmtree(d)
                print(f"  🗑️  Cleaned: {d}")

    config = KiroConfig(
        repo_root=args.repo_root,
        model=args.model,
        effort=args.effort,
        timeout_seconds=args.timeout,
    )

    run_pipeline(
        cases_file=args.cases,
        config=config,
        filter_ids=args.ids,
        filter_categories=args.categories,
        skip_run=args.skip_run,
        output_dir=args.output,
    )


if __name__ == "__main__":
    main()
