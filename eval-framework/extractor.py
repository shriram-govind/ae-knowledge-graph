#!/usr/bin/env python3
"""
Answer extractor — parses raw Kiro CLI session output to extract:
- Structured answer items (from the JSON block the agent was asked to produce)
- Token counts (estimated from output length if not available)
- Tool calls (detected from output patterns)
- Confidence and unsupported claims

The agent is prompted to output a JSON block like:
```json
{"answer_items": ["item1", "item2"], "confidence": 0.8, "unsupported_claims": []}
```

This extractor finds that block and also heuristically counts tool usage.
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

FRAMEWORK_DIR = Path(__file__).parent
SESSIONS_DIR = FRAMEWORK_DIR / "sessions"
RECORDED_RESULTS_DIR = FRAMEWORK_DIR / "recorded-results"

sys.path.insert(0, str(FRAMEWORK_DIR))
from models import ExecutionResult, Method, ToolCall


# ─── Token Estimation ────────────────────────────────────────────────────────

# Rough approximation: 1 token ≈ 4 characters for English text
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> tuple[int, int]:
    """
    Estimate input and output tokens from session text.
    
    Heuristic: The prompt (input) is ~20% of total text, the response (output) is ~80%.
    This is rough but serviceable when we don't have provider-level token counts.
    """
    total_chars = len(text)
    total_tokens = total_chars // CHARS_PER_TOKEN

    # Assume input is the prompt portion (~20%) and output is the agent response (~80%)
    tokens_in = int(total_tokens * 0.2)
    tokens_out = int(total_tokens * 0.8)

    return tokens_in, tokens_out


# ─── Tool Call Detection ─────────────────────────────────────────────────────

# Patterns that indicate tool usage in Kiro CLI output
TOOL_PATTERNS = {
    "read_neo4j_cypher": [
        re.compile(r'read_neo4j_cypher', re.IGNORECASE),
        re.compile(r'MATCH\s*\(', re.IGNORECASE),  # Cypher query
        re.compile(r'neo4j', re.IGNORECASE),
    ],
    "get_neo4j_schema": [
        re.compile(r'get_neo4j_schema', re.IGNORECASE),
    ],
    "grep": [
        re.compile(r'"pattern"', re.IGNORECASE),
        re.compile(r'grep', re.IGNORECASE),
    ],
    "read": [
        re.compile(r'"mode":\s*"Line"', re.IGNORECASE),
        re.compile(r'"mode":\s*"Directory"', re.IGNORECASE),
    ],
    "shell": [
        re.compile(r'git\s+(grep|ls-files|log)', re.IGNORECASE),
        re.compile(r'"command":', re.IGNORECASE),
    ],
    "glob": [
        re.compile(r'"pattern".*\*', re.IGNORECASE),
    ],
    "code": [
        re.compile(r'"operation":\s*"(search_symbols|find_references|goto_definition)"', re.IGNORECASE),
    ],
}


def count_tool_calls(text: str) -> dict[str, int]:
    """
    Heuristically count tool calls from session output.
    
    Looks for tool invocation patterns in the text.
    Returns {tool_name: count}.
    """
    counts = {}

    # Split into chunks around tool call boundaries
    # Kiro typically outputs tool calls in antml:function_calls blocks
    tool_blocks = re.findall(
        r'<invoke name="([^"]+)"', text
    )

    for tool_name in tool_blocks:
        counts[tool_name] = counts.get(tool_name, 0) + 1

    # If we couldn't find structured tool calls, fall back to pattern matching
    if not counts:
        for tool_name, patterns in TOOL_PATTERNS.items():
            for pattern in patterns:
                matches = pattern.findall(text)
                if matches:
                    counts[tool_name] = counts.get(tool_name, 0) + len(matches)
                    break  # Only count once per tool type per pattern group

    return counts


# ─── Answer Extraction ───────────────────────────────────────────────────────


def extract_json_answer(text: str) -> Optional[dict]:
    """
    Extract the structured JSON answer block from session output.
    
    The agent was prompted to output:
    ```json
    {"answer_items": [...], "confidence": 0.8, "unsupported_claims": [...]}
    ```
    
    We look for this pattern and parse it.
    """

    # Try to find JSON in a code block first
    json_blocks = re.findall(
        r'```(?:json)?\s*\n(\{[^`]*"answer_items"[^`]*\})\s*\n```',
        text,
        re.DOTALL,
    )

    if json_blocks:
        # Take the last one (the final answer)
        for block in reversed(json_blocks):
            try:
                data = json.loads(block)
                if "answer_items" in data:
                    return data
            except json.JSONDecodeError:
                continue

    # Fallback: look for inline JSON with answer_items
    inline_matches = re.findall(
        r'\{[^{}]*"answer_items"\s*:\s*\[[^\]]*\][^{}]*\}',
        text,
        re.DOTALL,
    )

    if inline_matches:
        for match in reversed(inline_matches):
            try:
                data = json.loads(match)
                if "answer_items" in data:
                    return data
            except json.JSONDecodeError:
                continue

    return None


def extract_items_heuristically(text: str) -> list[str]:
    """
    If the agent didn't produce a clean JSON block, try to extract
    answer items from bullet points or numbered lists in the output.
    
    Looks for patterns like:
    - ClassName
    - `ModuleName`
    - 1. SomeThing
    """

    items = []

    # Bullet points with backticks (common for code items)
    backtick_items = re.findall(r'[-*]\s+`([^`]+)`', text)
    items.extend(backtick_items)

    # Bullet points without backticks (class/module names are typically PascalCase)
    bullet_items = re.findall(r'[-*]\s+([A-Z][a-zA-Z0-9_]+(?:Impl|Service|Config|Function|Rule)?)\b', text)
    items.extend(bullet_items)

    # Table rows with pipe-separated values
    table_items = re.findall(r'\|\s*`?([A-Z][a-zA-Z0-9_]+)`?\s*\|', text)
    items.extend(table_items)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for item in items:
        normalized = item.strip()
        if normalized.lower() not in seen and len(normalized) > 2:
            seen.add(normalized.lower())
            unique.append(normalized)

    return unique


# ─── Main Extraction Pipeline ────────────────────────────────────────────────


def extract_result(session_file: Path, test_case_id: str, method: str) -> ExecutionResult:
    """
    Extract a full ExecutionResult from a session output file.
    """

    text = session_file.read_text(errors="replace")

    # Load metadata if available
    meta_file = session_file.with_suffix(".meta.json")
    meta = {}
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())

    # Extract the structured answer
    json_answer = extract_json_answer(text)

    if json_answer:
        answer_items = json_answer.get("answer_items", [])
        confidence = json_answer.get("confidence", 0.5)
        unsupported_claims = json_answer.get("unsupported_claims", [])
    else:
        # Fallback to heuristic extraction
        answer_items = extract_items_heuristically(text)
        confidence = 0.3  # Low confidence when we had to guess
        unsupported_claims = ["[WARN: No structured JSON answer found, items extracted heuristically]"]

    # Count tool calls
    tool_counts = count_tool_calls(text)
    total_tool_calls = sum(tool_counts.values())

    # Classify tool usage
    graph_queries = tool_counts.get("read_neo4j_cypher", 0) + tool_counts.get("get_neo4j_schema", 0)
    grep_calls = tool_counts.get("grep", 0)
    file_reads = tool_counts.get("read", 0) + tool_counts.get("glob", 0)

    # Estimate tokens
    tokens_in, tokens_out = estimate_tokens(text)

    # Build tool call list
    tool_call_list = [
        ToolCall(tool_name=name, input_summary=f"({count} calls)")
        for name, count in sorted(tool_counts.items())
    ]

    # Duration from metadata
    wall_time_ms = meta.get("duration_ms", 0)

    return ExecutionResult(
        method=Method(method),
        test_case_id=test_case_id,
        answer_items=answer_items,
        answer_summary=text[-500:] if len(text) > 500 else text,  # Last 500 chars as summary
        total_tokens_in=tokens_in,
        total_tokens_out=tokens_out,
        total_tool_calls=total_tool_calls,
        tool_calls=tool_call_list,
        wall_time_ms=wall_time_ms,
        graph_queries=graph_queries,
        grep_calls=grep_calls,
        file_reads=file_reads,
        confidence=confidence,
        unsupported_claims=unsupported_claims,
    )


def extract_all_sessions() -> list[dict]:
    """
    Extract results from all session files in the sessions/ directory.
    Returns list of {test_case_id, method, result}.
    """

    if not SESSIONS_DIR.exists():
        print("No sessions directory found.")
        return []

    results = []
    processed = set()

    for txt_file in sorted(SESSIONS_DIR.glob("*.txt")):
        # Parse filename: {test_case_id}_{method}.txt
        stem = txt_file.stem
        # Find the method suffix
        for method in ["graph_assisted", "tools_only"]:
            if stem.endswith(f"_{method}"):
                test_case_id = stem[: -(len(method) + 1)]
                key = (test_case_id, method)
                if key in processed:
                    continue
                processed.add(key)

                result = extract_result(txt_file, test_case_id, method)
                results.append({
                    "test_case_id": test_case_id,
                    "method": method,
                    "result": result,
                })
                print(f"  ✓ Extracted: {test_case_id} [{method}] → {len(result.answer_items)} items")
                break

    return results


def save_extracted_results(results: list[dict]):
    """Save extracted results to the recorded-results directory."""
    RECORDED_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    from models import _serialize

    for entry in results:
        test_case_id = entry["test_case_id"]
        method = entry["method"]
        result = entry["result"]

        filename = f"{test_case_id}_{method}.json"
        output_path = RECORDED_RESULTS_DIR / filename
        output_path.write_text(json.dumps(_serialize(result), indent=2))

    print(f"\n  ✓ Saved {len(results)} results to {RECORDED_RESULTS_DIR}")


# ─── CLI ─────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract answers from Kiro session output")
    parser.add_argument("--session", type=Path, help="Extract from a single session file")
    parser.add_argument("--case", help="Test case ID (required with --session)")
    parser.add_argument("--method", choices=["graph_assisted", "tools_only"],
                       help="Method (required with --session)")
    parser.add_argument("--all", action="store_true", help="Extract from all sessions in sessions/")

    args = parser.parse_args()

    if args.session:
        if not args.case or not args.method:
            parser.error("--case and --method required with --session")
        result = extract_result(args.session, args.case, args.method)
        print(f"Extracted {len(result.answer_items)} items:")
        for item in result.answer_items:
            print(f"  - {item}")
        print(f"Confidence: {result.confidence}")
        print(f"Tool calls: {result.total_tool_calls} (graph: {result.graph_queries}, grep: {result.grep_calls})")
        print(f"Tokens: ~{result.total_tokens_in + result.total_tokens_out}")

    elif args.all:
        print("Extracting from all sessions...")
        results = extract_all_sessions()
        if results:
            save_extracted_results(results)
    else:
        parser.print_help()
