# Future Scope — AE Knowledge Graph

This document outlines enhancements that are **not in v1** but are architecturally supported by the current design. Each section explains what it would add, why it matters, and what's needed to implement it.

---

## 1. Dynamic Runtime Tracing

### What It Adds
Edges discovered by observing the running system — captures relationships that are invisible to static analysis (polymorphic dispatch, reflection, Kafka topic coupling, event listeners, Spring runtime bean selection).

### Why It Matters
Static analysis captures ~75-80% of relationships. The remaining 20-25% includes some of the most dangerous coupling — the kind that causes unexpected test failures when "unrelated" code changes.

### Architecture

```
┌────────────────────────────────┐
│  Running Appian (GDev agent)   │
│                                │
│  ┌──────────────────────────┐  │
│  │  OpenTelemetry Agent     │  │
│  │  (attached to JVM)       │  │
│  └──────────┬───────────────┘  │
│             │ spans                │
│  ┌──────────▼───────────────┐  │
│  │  Trace Collector         │  │
│  │  (Jaeger/OTLP exporter) │  │
│  └──────────┬───────────────┘  │
└─────────────┼──────────────────┘
              │ exported traces
              ▼
┌────────────────────────────────┐
│  Trace Ingestor (Python)       │
│                                │
│  • Parse span caller/callee    │
│  • Map to known JavaClass FQNs │
│  • Create edges with           │
│    source: 'dynamic'           │
└──────────────┬─────────────────┘
               │
               ▼
┌────────────────────────────────┐
│  Neo4j Knowledge Graph         │
│  (same graph, new edge type)   │
└────────────────────────────────┘
```

### Implementation Steps (when ready)

1. **Instrument a test run:** Attach OpenTelemetry Java agent to webapp during integration test suite run on GDev
2. **Export traces:** Configure OTLP exporter to write spans to a local file/service
3. **Build ingestor:** Python script that reads span data, extracts `(callerClass, calleeClass, method)` tuples
4. **Match to graph:** Map span class names to existing JavaClass nodes (by FQN)
5. **Create dynamic edges:** `CALLS_AT_RUNTIME` with `source: 'dynamic'`, `observedCount`, `lastObserved`

### What It Would Capture (that static can't)

- `@Autowired` fields where multiple implementations exist (Spring picks one at runtime)
- Event observer invocations (who actually fires `RecordTypeDefinitionEventObserver.onSave()`?)
- Kafka producer → consumer coupling (both reference the same topic, but through intermediary constants)
- Reflection-based class loading
- Plugin-loaded code execution paths
- `@ConditionalOnProperty` beans that are/aren't active based on environment

### Validation Use Case

A static edge that is NEVER observed dynamically across a full test run might be dead code. A dynamic edge with no static counterpart reveals a hidden coupling that should be documented.

---

## 2. Temporal Analysis (Git History Mining)

### What It Adds
Co-change coupling analysis — "which files historically change together?" This reveals logical coupling that isn't structural (no import/call relationship) but is empirically correlated.

### Why It Matters
Two files that always change together in PRs (but have no structural dependency) often indicate:
- Shared business logic that isn't formally abstracted
- A manual coordination requirement that should be automated
- A test/implementation pair
- Copy-pasted logic that should be deduplicated

### Architecture

```
┌─────────────────────────────────┐
│  Git History                     │
│  git log --numstat --follow      │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  Co-Change Analyzer (Python)     │
│                                  │
│  • For each commit, record       │
│    which files changed together  │
│  • Build co-occurrence matrix    │
│  • Apply threshold (>= N times   │
│    in last M months)             │
│  • Normalize by individual       │
│    change frequency              │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  Neo4j Knowledge Graph           │
│                                  │
│  New relationship:               │
│  (FileA)-[:CO_CHANGES_WITH {     │
│    count: N,                     │
│    confidence: 0.0-1.0,          │
│    lastObserved: date            │
│  }]->(FileB)                     │
└─────────────────────────────────┘
```

### Node Properties to Add

On every node that maps to a file:
- `lastModifiedBy` — Git author of most recent change
- `lastModifiedAt` — Timestamp of most recent change
- `changeFrequency` — Number of commits touching this file in last 6 months
- `hotness` — Normalized change frequency (0.0-1.0, relative to repo average)

### Query Patterns It Enables

```cypher
// "What files historically change with RecordWriteService?"
MATCH (f {name: 'RecordWriteService'})-[:CO_CHANGES_WITH]->(other)
WHERE NOT (f)-[:IMPORTS|EXTENDS|IMPLEMENTS]-(other)  // exclude structural deps
RETURN other.name, other.path
ORDER BY other.confidence DESC

// "What are the hottest files in records-java?"
MATCH (m:Module {name: 'records-java'})-[:CONTAINS*]->(f)
RETURN f.name, f.changeFrequency, f.lastModifiedBy
ORDER BY f.changeFrequency DESC
LIMIT 20
```

---

## 3. Incremental Graph Updates

### What It Adds
Instead of full rebuild (~5-10 min), update only the nodes/edges affected by recent changes (~seconds).

### Why It Matters
Developers switch branches, make changes, and want fresh graph data. A full rebuild after every change is too slow for interactive use.

### Architecture

```
┌────────────────────────────────┐
│  Git Diff (git diff HEAD~1)    │
│  → List of changed files       │
└──────────────┬─────────────────┘
               │
               ▼
┌────────────────────────────────┐
│  Incremental Extractor          │
│                                 │
│  For each changed file:         │
│  1. Determine which extractor   │
│     handles this file type      │
│  2. Delete old nodes/edges      │
│     sourced from this file      │
│  3. Re-extract just this file   │
│  4. Reconcile with existing     │
│     graph (update, not replace) │
└──────────────┬─────────────────┘
               │
               ▼
┌────────────────────────────────┐
│  Neo4j (partial update)         │
└────────────────────────────────┘
```

### Requirements for This to Work

1. Every edge needs `sourceFile` property (already in v1 design ✅)
2. Each extractor needs a `extract_single_file(path)` method (not just `extract_all()`)
3. Deletion query: `MATCH ()-[r {sourceFile: $path}]-() DELETE r` removes stale edges before re-extraction
4. Node lifecycle: If a file is deleted, all nodes extracted from it must be removed

### Trigger Options

- **Git hook (`post-checkout`, `post-merge`):** Automatic on branch switch
- **File watcher (watchdog):** Real-time as you save files
- **Manual:** `python extract_all.py --incremental` (uses `git diff` against last extraction timestamp)

---

## 4. Graph-Assisted Code Generation

### What It Adds
Agents use the graph not just for impact analysis but for code generation — "generate a class that follows the same patterns as its peers."

### Use Cases

1. **"Create a new extractor following the existing pattern"** — Query graph for all existing extractor classes, analyze their shared interface/structure, generate new one conforming to the pattern.

2. **"Add a new feature toggle"** — Query graph for how existing toggles are defined, referenced, and documented. Generate the toggle constant, properties entry, and gating code.

3. **"Create a new SAIL system rule"** — Query graph for similar rules (same parentUuid/category), understand their parameter patterns, generate conforming XML.

### How the Graph Helps

```cypher
// "Show me all classes that implement SourceDataReader, their methods, and what module they're in"
MATCH (impl:JavaClass)-[:IMPLEMENTS]->(iface:JavaClass {name: 'SourceDataReader'})
MATCH (impl)<-[:CONTAINS*]-(m:Module)
RETURN impl.name, impl.path, m.name
```

The agent then reads those files to understand the pattern, and generates new code conforming to it.

---

## 5. Graph Diff Between Branches

### What It Adds
Compare the knowledge graph state between two git branches to understand "what dependencies changed in this PR?"

### Architecture

1. Extract graph from `main` branch → store as baseline
2. Extract graph from feature branch → store as candidate
3. Diff: new nodes, removed nodes, new edges, removed edges, modified properties

### Query Pattern

```cypher
// After loading both graphs (with branch labels):
MATCH (n:JavaClass:BranchFeature)
WHERE NOT EXISTS {
  MATCH (m:JavaClass:BranchMain {fqn: n.fqn})
}
RETURN n.name AS newClasses

MATCH (n:JavaClass:BranchMain)
WHERE NOT EXISTS {
  MATCH (m:JavaClass:BranchFeature {fqn: n.fqn})
}
RETURN n.name AS removedClasses
```

### Why It Matters
PR reviews could auto-generate an "impact summary" showing what new dependencies a PR introduces and what existing dependency paths it breaks.

---

## 6. Semantic Search Over the Graph

### What It Adds
Natural language queries: "find all code related to record syncing" without knowing exact class/rule names.

### Implementation
- Add text embeddings to node descriptions/names
- Use Neo4j's vector index for semantic similarity search
- Agent queries: "what's related to data export?" → returns nodes whose names/descriptions are semantically similar

### Requirements
- Neo4j 5.x vector indexes (available in Community Edition)
- Embedding model (small, local — e.g., sentence-transformers)
- Embedding stored as node property

---

## Priority Ordering

| Enhancement | Value | Effort | Dependencies |
|-------------|-------|--------|--------------|
| Incremental Updates | High (developer experience) | Medium | v1 complete |
| Temporal Analysis | High (hidden coupling) | Low | Just git history |
| Dynamic Tracing | Very High (coverage) | High | Running instance + instrumentation |
| Graph Diff | Medium (PR reviews) | Medium | v1 complete |
| Code Generation | Medium (productivity) | Low | v1 complete |
| Semantic Search | Low (convenience) | Medium | Embedding infrastructure |

**Recommended next steps after v1:**
1. Temporal analysis (low effort, high value — just mine git log)
2. Incremental updates (needed for developer experience)
3. Dynamic tracing (highest coverage gain, but requires infra)
