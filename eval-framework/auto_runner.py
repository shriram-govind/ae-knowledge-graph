#!/usr/bin/env python3
"""
Automated Kiro CLI runner for the evaluation framework.

Executes test cases by launching kiro-cli in non-interactive mode with
two different system prompts:
  1. Graph-assisted: has access to Neo4j MCP tools
  2. Tools-only: instructed to NOT use Neo4j, only grep/read/shell

Captures the full session output for parsing by the extractor.
"""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Paths
FRAMEWORK_DIR = Path(__file__).parent
TEST_CASES_DIR = FRAMEWORK_DIR / "test-cases"
SESSIONS_DIR = FRAMEWORK_DIR / "sessions"
DEFAULT_REPO_ROOT = Path(os.environ.get("AE_REPO_ROOT", Path.home() / "repo" / "ae"))


@dataclass
class KiroConfig:
    """Configuration for a kiro-cli invocation."""

    repo_root: Path = DEFAULT_REPO_ROOT
    model: str = ""  # Empty = use default (Auto)
    effort: str = "high"
    trust_all_tools: bool = True
    timeout_seconds: int = 300  # 5 minutes max per case


# ─── Prompt Templates ────────────────────────────────────────────────────────


GRAPH_ASSISTED_PREFIX = """IMPORTANT INSTRUCTIONS FOR THIS SESSION:
You are being evaluated on your ability to answer codebase questions using the Neo4j knowledge graph.

RULES:
1. PREFER the knowledge graph (read_neo4j_cypher, get_neo4j_schema) for finding relationships, dependencies, and impact analysis.
2. You MAY also use grep, read, glob, shell as supplements.
3. Provide your answer as a STRUCTURED LIST of specific findings.
4. At the end, output a JSON block in this exact format:

```json
{"answer_items": ["item1", "item2", ...], "confidence": 0.8, "unsupported_claims": []}
```

The answer_items should be specific names (class names, module names, rule names, etc.).
confidence is 0.0-1.0.
unsupported_claims lists any assertions you made without tool evidence.

NOW ANSWER THIS QUESTION:
"""

TOOLS_ONLY_PREFIX = """IMPORTANT INSTRUCTIONS FOR THIS SESSION:
You are being evaluated on your ability to answer codebase questions using ONLY standard tools.

RULES:
1. You MUST NOT use read_neo4j_cypher, write_neo4j_cypher, or get_neo4j_schema.
2. Use ONLY: grep, read, glob, shell (git grep, git ls-files, etc.)
3. Provide your answer as a STRUCTURED LIST of specific findings.
4. At the end, output a JSON block in this exact format:

```json
{"answer_items": ["item1", "item2", ...], "confidence": 0.8, "unsupported_claims": []}
```

The answer_items should be specific names (class names, module names, rule names, etc.).
confidence is 0.0-1.0.
unsupported_claims lists any assertions you made without tool evidence.

NOW ANSWER THIS QUESTION:
"""


def build_prompt(scenario: str, method: str) -> str:
    """Build the full prompt for a kiro-cli invocation."""
    prefix = GRAPH_ASSISTED_PREFIX if method == "graph_assisted" else TOOLS_ONLY_PREFIX
    return f"{prefix}\n{scenario}"


# ─── Kiro CLI Execution ──────────────────────────────────────────────────────


def run_kiro_session(
    prompt: str,
    config: KiroConfig,
    session_id: str,
) -> dict:
    """
    Run a kiro-cli chat session in non-interactive mode.
    
    Returns:
        {
            "stdout": str,  # Full session output
            "stderr": str,
            "exit_code": int,
            "duration_ms": int,
            "session_id": str,
        }
    """

    cmd = [
        "kiro-cli", "chat",
        "--no-interactive",
        "--trust-all-tools",
        "--effort", config.effort,
        prompt,
    ]

    if config.model:
        cmd.extend(["--model", config.model])

    env = os.environ.copy()
    # Ensure kiro can find the repo context
    start_time = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            cwd=str(config.repo_root),
            env=env,
        )
        duration_ms = int((time.time() - start_time) * 1000)

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "duration_ms": duration_ms,
            "session_id": session_id,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start_time) * 1000)
        return {
            "stdout": "",
            "stderr": f"TIMEOUT after {config.timeout_seconds}s",
            "exit_code": -1,
            "duration_ms": duration_ms,
            "session_id": session_id,
        }

    except FileNotFoundError:
        return {
            "stdout": "",
            "stderr": "kiro-cli not found in PATH",
            "exit_code": -2,
            "duration_ms": 0,
            "session_id": session_id,
        }


def run_test_case(
    test_case_id: str,
    scenario: str,
    method: str,
    config: KiroConfig,
) -> dict:
    """
    Run a single test case with one method.
    Saves the raw session output and returns metadata.
    """

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    session_id = f"{test_case_id}_{method}_{int(time.time())}"
    prompt = build_prompt(scenario, method)

    print(f"  🚀 Running: {test_case_id} [{method}]...")
    result = run_kiro_session(prompt, config, session_id)

    # Save raw output
    output_file = SESSIONS_DIR / f"{test_case_id}_{method}.txt"
    output_file.write_text(result["stdout"])

    # Save metadata
    meta_file = SESSIONS_DIR / f"{test_case_id}_{method}.meta.json"
    meta = {
        "test_case_id": test_case_id,
        "method": method,
        "session_id": session_id,
        "exit_code": result["exit_code"],
        "duration_ms": result["duration_ms"],
        "output_file": str(output_file),
        "stderr": result["stderr"][:500] if result["stderr"] else "",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    meta_file.write_text(json.dumps(meta, indent=2))

    if result["exit_code"] == 0:
        print(f"  ✅ Completed in {result['duration_ms']}ms")
    elif result["exit_code"] == -1:
        print(f"  ⏰ Timed out after {config.timeout_seconds}s")
    else:
        print(f"  ❌ Failed (exit code {result['exit_code']})")

    return meta


# ─── Batch Execution ─────────────────────────────────────────────────────────


def run_all_cases(
    cases_file: Path,
    config: KiroConfig,
    filter_ids: Optional[list[str]] = None,
    filter_categories: Optional[list[str]] = None,
    methods: Optional[list[str]] = None,
):
    """
    Run all (or filtered) test cases through both methods.
    
    Args:
        cases_file: Path to test cases JSON
        config: Kiro CLI configuration
        filter_ids: Only run these case IDs
        filter_categories: Only run cases in these categories
        methods: Which methods to run (default: both)
    """

    import sys
    sys.path.insert(0, str(FRAMEWORK_DIR))
    from models import load_test_cases

    cases = load_test_cases(cases_file)

    if filter_ids:
        cases = [c for c in cases if c.id in filter_ids]
    if filter_categories:
        cases = [c for c in cases if c.category.value in filter_categories]

    run_methods = methods or ["graph_assisted", "tools_only"]

    print(f"\n{'='*60}")
    print(f"🧪 Automated Evaluation Run")
    print(f"   Cases: {len(cases)}")
    print(f"   Methods: {run_methods}")
    print(f"   Repo: {config.repo_root}")
    print(f"   Timeout: {config.timeout_seconds}s per case")
    print(f"{'='*60}\n")

    results = []
    for i, case in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] {case.id} ({case.category.value})")
        print(f"  Q: {case.scenario[:80]}...")

        for method in run_methods:
            meta = run_test_case(
                test_case_id=case.id,
                scenario=case.scenario,
                method=method,
                config=config,
            )
            results.append(meta)

    # Save run summary
    summary_file = SESSIONS_DIR / "run-summary.json"
    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_cases": len(cases),
        "methods": run_methods,
        "results": results,
        "config": {
            "repo_root": str(config.repo_root),
            "model": config.model,
            "effort": config.effort,
            "timeout_seconds": config.timeout_seconds,
        },
    }
    summary_file.write_text(json.dumps(summary, indent=2))

    print(f"\n{'='*60}")
    print(f"✓ Run complete. Sessions saved to: {SESSIONS_DIR}")
    print(f"  Summary: {summary_file}")
    print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Automated Kiro CLI evaluation runner")
    parser.add_argument("--cases", type=Path, default=TEST_CASES_DIR / "seed-cases.json")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--ids", nargs="*", help="Only run specific case IDs")
    parser.add_argument("--categories", nargs="*", help="Only run specific categories")
    parser.add_argument("--methods", nargs="*", choices=["graph_assisted", "tools_only"],
                       help="Only run specific methods")

    args = parser.parse_args()

    config = KiroConfig(
        repo_root=args.repo_root,
        model=args.model,
        effort=args.effort,
        timeout_seconds=args.timeout,
    )

    run_all_cases(
        cases_file=args.cases,
        config=config,
        filter_ids=args.ids,
        filter_categories=args.categories,
        methods=args.methods,
    )
