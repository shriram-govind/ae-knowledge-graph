# Knowledge Graph Evaluation Framework

Automated A/B testing framework that measures whether the Neo4j knowledge graph helps AI agents answer codebase questions better, faster, or cheaper compared to standard tools (grep/read/shell).

## TL;DR

```bash
# Run the full automated evaluation (no human intervention needed)
cd eval-framework
python run_eval.py --cases test-cases/seed-cases.json --repo-root ~/repo/ae
```

This will:
1. Launch Kiro CLI twice per test case (once with graph, once without)
2. Parse the agent's answers from the session output
3. Compare both methods against ground truth and each other
4. Produce a Markdown report with winners, exclusive findings, and recommendations

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              run_eval.py                                          │
│                         (Orchestrator — runs everything)                          │
└──────────────────────────────────┬──────────────────────────────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
         ▼                         ▼                         ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│  auto_runner.py  │    │  extractor.py    │    │ evaluator.py +       │
│                  │    │                  │    │ report.py            │
│ Launches kiro-cli│───▶│ Parses session   │───▶│ Scores + compares    │
│ --no-interactive │    │ output for items │    │ + generates reports  │
│ for each case    │    │ and tool counts  │    │                      │
└──────────────────┘    └──────────────────┘    └──────────────────────┘
         │                         │                         │
         ▼                         ▼                         ▼
   sessions/                 recorded-results/          results/
   ├── case_graph.txt        ├── case_graph.json       ├── report.md
   ├── case_tools.txt        └── case_tools.json       ├── report.csv
   └── case_*.meta.json                                └── report.json
```

---

## Prerequisites

- **Python 3.10+** (no external pip packages required — stdlib only)
- **kiro-cli** installed and in PATH
- **Neo4j** running with the AE knowledge graph loaded (for graph-assisted sessions)
- **Access to the `ae` repo** (for tools-only sessions to grep/read files)

---

## Quick Start

### 1. Automated (zero human intervention)

```bash
cd /path/to/AEKnowledgeGraph/eval-framework

# Run all seed cases
python run_eval.py --cases test-cases/seed-cases.json

# Run specific cases
python run_eval.py --cases test-cases/seed-cases.json --ids transitive-deps-001 blast-radius-001

# Run only blast_radius cases
python run_eval.py --cases test-cases/test-cases-md-imported.json --categories blast_radius

# Use a specific model
python run_eval.py --cases test-cases/seed-cases.json --model sonnet --timeout 600

# Clean previous results and start fresh
python run_eval.py --cases test-cases/seed-cases.json --clean
```

### 2. Re-evaluate existing sessions (no re-running kiro-cli)

```bash
# If you already have session output and just want to re-score
python run_eval.py --cases test-cases/seed-cases.json --skip-run
```

### 3. Manual mode (observe real sessions, record results yourself)

```bash
# List available test cases
python cli.py list

# Record results after observing a session
python cli.py record --case transitive-deps-001 --method graph_assisted \
    --answers "epex,actor-executor,observability,ae,records-java" \
    --tokens-in 8000 --tokens-out 5000

# Generate report from manually recorded results
python cli.py report
```

---

## Project Structure

```
eval-framework/
├── run_eval.py             # 🎯 Main entry point — runs the full pipeline
├── auto_runner.py          # Launches kiro-cli sessions
├── extractor.py            # Parses session output → structured results
├── evaluator.py            # Scores baseline + compares methods
├── report.py               # Generates Markdown/CSV/JSON reports
├── models.py               # Data structures (TestCase, Result, etc.)
├── cli.py                  # Manual CLI for listing/recording/reporting
├── test-cases/
│   ├── seed-cases.json             # 12 curated cases
│   └── test-cases-md-imported.json # 27 cases from TEST_CASES.md
├── sessions/               # Raw kiro-cli output (gitignored)
├── recorded-results/       # Extracted/parsed results (gitignored)
├── results/                # Final reports (gitignored)
└── README.md               # This file
```

---

## Evaluation Philosophy

### The Ground Truth Problem

We **cannot** know the complete correct answer for most codebase questions. Instead, we use a **hybrid approach**:

| What we check | How |
|---------------|-----|
| Did it find the **minimum known-correct** items? | `must_include` list (manually verified) |
| Did it avoid **known-wrong** items? | `must_exclude` list |
| Did it find **enough** items? | `minimum_count` threshold |
| Which method found **more**? | Relative comparison (no ground truth needed) |
| What did each find **exclusively**? | Set difference between methods |
| Which was **cheaper**? | Token count comparison |

### Winner Determination (priority order)

1. **Baseline failure** — if one method missed a `must_include` item and the other didn't → passer wins
2. **Exclusive findings** — if one method found 2x+ more exclusive items → it wins
3. **Total items** — if one method found 3+ more items total → it wins
4. **Token efficiency** — if results are similar but one used 50%+ fewer tokens → it wins
5. **Tie** — both methods performed similarly

---

## Test Case Format

```json
{
  "id": "transitive-deps-001",
  "scenario": "What modules are affected by a vulnerability in library X?",
  "category": "transitive_deps",
  "ground_truth": {
    "must_include": ["epex", "records-java"],
    "must_exclude": ["admin-console"],
    "minimum_count": 5,
    "description": "Should find modules transitively depending on this lib",
    "verification_method": "Verified via Neo4j query + manual Gradle tree inspection",
    "confidence": 0.9
  },
  "expected_graph_advantage": "strong",
  "instructions": "List all affected modules and explain the dependency chain.",
  "tags": ["library-upgrade", "transitive"]
}
```

### Ground Truth Fields

| Field | Required? | Description |
|-------|-----------|-------------|
| `must_include` | Yes | Items you're SURE must appear (high confidence, verified) |
| `must_exclude` | No | Items that would clearly be wrong (false positive markers) |
| `minimum_count` | No | At minimum, a good answer should have this many items |
| `description` | No | Human-readable explanation of what correct looks like |
| `verification_method` | No | How you verified the must_include items |
| `confidence` | No | Your confidence in must_include (0.0–1.0, default 1.0) |

### Categories

| Category | What it tests | Graph expected to help? |
|----------|---------------|------------------------|
| `blast_radius` | What breaks if I change X? | ✅ Strong |
| `transitive_deps` | Library dependency chains | ✅ Strong |
| `presence_check` | Is X used anywhere? | ❌ None |
| `ownership` | Who owns this code? | ✅ Strong |
| `cross_language` | SAIL ↔ Java tracing | ✅ Moderate |
| `feature_toggle` | What does toggle X gate? | ✅ Strong |
| `test_coverage` | What tests cover Z? | ✅ Strong |
| `dependency_chain` | Module architecture | ✅ Moderate |
| `api_surface` | What endpoints exist? | ✅ Moderate |

---

## How the Automation Works

### Step 1: auto_runner.py

For each test case × each method, launches:

```bash
kiro-cli chat --no-interactive --trust-all-tools --effort high "<prompt>"
```

**Graph-assisted prompt** tells the agent to prefer Neo4j tools and asks for a JSON answer block.

**Tools-only prompt** explicitly forbids Neo4j tools and asks for the same JSON answer block.

The raw stdout is saved to `sessions/{case_id}_{method}.txt`.

### Step 2: extractor.py

Parses each session file looking for:

1. **Structured JSON answer** — the `{"answer_items": [...], "confidence": ...}` block the agent was asked to produce
2. **Tool calls** — counts `<invoke name="...">` patterns to tally graph queries, grep calls, etc.
3. **Token estimate** — rough calculation from text length (4 chars ≈ 1 token)
4. **Fallback extraction** — if no JSON block found, heuristically extracts items from bullet points

### Step 3: evaluator.py

For each test case with both methods recorded:

1. **Baseline check** — did each method find `must_include` items and avoid `must_exclude`?
2. **Relative comparison** — what's exclusive to each, what they agree on, token costs
3. **Winner determination** — priority rubric (baseline → exclusives → count → cost)

### Step 4: report.py

Produces three output formats:
- `evaluation-report.md` — human-readable with tables, icons, per-case details
- `evaluation-data.csv` — for spreadsheet analysis
- `evaluation-report.json` — full structured data for programmatic consumption

---

## CLI Reference

### `run_eval.py` — Full Pipeline (Automated)

```
python run_eval.py --cases <file> [options]

Required:
  --cases FILE          Path to test cases JSON

Options:
  --repo-root PATH      Path to ae repo (default: ~/repo/ae)
  --model MODEL         LLM model (default: Auto)
  --effort LEVEL        Agent effort: low/medium/high/xhigh/max (default: high)
  --timeout SECS        Max time per case (default: 300)
  --ids ID [ID ...]     Only run specific case IDs
  --categories CAT ...  Only run specific categories
  --skip-run            Skip kiro-cli execution, re-evaluate existing sessions
  --output DIR          Output directory (default: results/)
  --clean               Delete previous sessions and results first
```

### `cli.py` — Manual Mode

```
python cli.py list [--cases FILE]
python cli.py prompt --case ID --method METHOD [--cases FILE]
python cli.py record --case ID --method METHOD --answers "a,b,c" [options]
python cli.py report [--output DIR] [--cases FILE]
python cli.py interactive [--category CAT] [--cases FILE]
```

**`record` options:**
| Arg | Description |
|-----|-------------|
| `--answers "a,b,c"` | Comma-separated findings |
| `--tokens-in N` | Input tokens consumed |
| `--tokens-out N` | Output tokens generated |
| `--tools-used "t1,t2"` | Comma-separated tool names invoked |
| `--confidence 0.9` | Self-reported confidence |
| `--wall-time MS` | Wall clock time in ms |
| `--hallucinations "c1,c2"` | Claims without tool evidence |

---

## Understanding the Report

### Executive Summary

```
| Metric | Value |
|--------|-------|
| Graph wins | 7 |
| Tools wins | 2 |
| Ties | 3 |
| Token Efficiency (tools/graph) | 2.1x |      ← graph used ~half the tokens
| Graph-exclusive findings | 23 |               ← things ONLY graph found
| Tools-exclusive findings | 5 |                ← things ONLY tools found
| Agreed findings | 31 |                        ← things BOTH found
```

### Per-Case Verdicts

- 🟢 = Graph wins
- 🔵 = Tools wins
- ⚪ = Tie
- ❓ = Inconclusive

### Key Metrics to Watch

| Metric | What it tells you |
|--------|-------------------|
| **Graph-exclusive findings** | The graph's raison d'être — things it found that grep literally cannot |
| **Token efficiency ratio** | >1.0 means graph is cheaper; <1.0 means tools-only is cheaper |
| **must_include pass rate** | Did the method find the things we're SURE about? |
| **Agreement count** | High = both methods converge (answer is likely correct) |

---

## Adding New Test Cases

1. Add to `test-cases/seed-cases.json` or create a new file:

```json
{
  "id": "my-new-case-001",
  "scenario": "Your natural language question here",
  "category": "blast_radius",
  "ground_truth": {
    "must_include": ["ThingYouAreConfidentAbout"],
    "must_exclude": [],
    "minimum_count": 3,
    "verification_method": "How you verified the must_include items"
  },
  "expected_graph_advantage": "strong",
  "tags": ["my-feature"]
}
```

2. Verify `must_include` items are correct (run the query yourself, check the code)
3. Run: `python run_eval.py --cases test-cases/my-file.json --ids my-new-case-001`

---

## Limitations & Known Issues

| Limitation | Impact | Workaround |
|-----------|--------|------------|
| Token counts are estimated (~4 chars/token) | Cost comparison is approximate | Use LLM provider API for exact counts if available |
| kiro-cli may not always produce the JSON answer block | Extractor falls back to heuristic bullet-point parsing | Confidence is automatically lowered for heuristic extractions |
| Graph-exclusive findings are unverified by default | Could be false positives from the graph | Use `FindingVerdict` field to record human review |
| Non-interactive mode may behave differently than interactive | Agent might make different choices without follow-up | Accept as a controlled variable — both methods have the same constraint |
| Sessions share the same Neo4j instance | Tools-only method *could* theoretically use the graph | The prompt explicitly forbids it — honor system enforced |

---

## FAQ

**Q: How long does a full run take?**
A: ~5 minutes per test case × 2 methods = ~10 min per case. 12 cases ≈ 2 hours. Use `--timeout` and `--ids` to control scope.

**Q: Can I run just one method?**
A: Use `auto_runner.py` directly with `--methods graph_assisted` or `--methods tools_only`.

**Q: What if kiro-cli isn't installed?**
A: The runner will report exit code -2 and the extractor will produce empty results. Install kiro-cli first.

**Q: What if Neo4j is down?**
A: Graph-assisted sessions will still run but Neo4j queries will fail/return empty. The agent will likely fall back to grep. This is actually a valid data point — it shows what happens when the graph is unavailable.

**Q: Can I add more test case categories?**
A: Yes — add a new value to the `Category` enum in `models.py` and create test cases using it.

**Q: The agent didn't produce the JSON answer block. What now?**
A: The extractor has a heuristic fallback that extracts PascalCase names from bullet points. Confidence is automatically set to 0.3 for these cases. You can also manually record via `cli.py record`.
