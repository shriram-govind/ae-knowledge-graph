# AE Knowledge Graph

A Neo4j knowledge graph that maps **all statically-extractable dependencies** across the Appian `ae` monorepo — 94K+ nodes, 800K+ relationships, 21 extractors, queryable by AI agents via MCP in <2 seconds.

## What It Does

Given any file, class, module, library, SAIL rule, or database table in the codebase, the graph answers:

- **"What breaks if I change X?"** — full blast radius across Java, SAIL, TypeScript, DB, and config layers
- **"If I upgrade library Y, what's affected?"** — specific classes, modules, and transitive library cascade
- **"What tests cover this rule?"** — expression tests and Groovy tests mapped to production code
- **"Who owns this code?"** — CODEOWNERS team resolution
- **"What Java class backs this SAIL function?"** — cross-language tracing
- **"What gets injected into this Spring bean?"** — full DI wiring with @Primary/@Qualifier metadata

---

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| **Colima** | Docker runtime (enterprise alternative to Docker Desktop) | `brew install colima` |
| **Docker CLI + Compose** | Runs Neo4j container | `brew install docker docker-compose` |
| **Python 3.12+** | Extraction pipeline | Already installed via Homebrew |
| **Java JDK** (any version with `jar` command) | JAR scanning for library mapping | Should already exist if you build `ae` |
| **Node.js 18+** | MCP server for agent integration | `brew install node` |
| **Gradle cache populated** | Required for JAR scanning + dependency tree | Run `./gradlew testClasses` in `ae` repo once |
| **VPN connected** | Only needed for dependency tree generation (Gradle resolves from Artifactory) | — |

---

## Quick Start

```bash
# 1. Start Docker runtime
colima start

# 2. Start Neo4j
cd knowledge-graph
docker-compose up -d
# Wait ~30 seconds for health check to pass

# 3. Set up Python environment (one-time)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Run the full extraction (~7 minutes)
python3 extract_all.py --repo-root ~/repo/ae --stats

# 5. Query in browser
open http://localhost:7474
# Login: neo4j / knowledge
```

---

## Commands Reference

### Full rebuild (recommended daily workflow)

```bash
cd knowledge-graph && source .venv/bin/activate
python3 extract_all.py --repo-root ~/repo/ae --stats
```

Clears the graph, runs all 21 extractors, prints statistics. Takes ~7 minutes.

### Run specific extractors only

```bash
# Only re-run the Java extractor (keeps everything else intact)
python3 extract_all.py --repo-root ~/repo/ae --only java --no-clear

# Run multiple specific extractors
python3 extract_all.py --repo-root ~/repo/ae --only gradle,java,sail --no-clear
```

### Available extractor names

| Name | What it does | Time |
|------|-------------|------|
| `gradle` | Module nodes + DEPENDS_ON edges | ~20s |
| `java` | 50K class nodes + imports/extends/implements | ~37s |
| `sail` | 17K SAIL rule nodes + CALLS edges | ~19s |
| `cross_language` | Java↔SAIL bridges (FN_ID, Reactions) | ~16s |
| `spring_di` | @Autowired, @Bean, constructor injection | ~12s |
| `external_libs` | External library nodes from lockfile + globalDeps | ~16s |
| `lib_usage` | Class→library + module→library import mapping | ~107s (first run) / ~20s (cached) |
| `lib_transitivity` | Library→library transitive deps from Gradle tree | ~1s (cached) / 15min (regenerate) |
| `npm` | NPM package.json dependencies | ~3s |
| `typescript` | TS/React file imports + JSX renders | ~31s |
| `k` | K language file loads | ~5s |
| `db` | Liquibase tables + FK relationships | ~1s |
| `toggle` | Feature toggle → code gating | ~31s |
| `infra` | JSP, Docker, CODEOWNERS, OpenAPI | ~6s |
| `expression_tests` | Test→rule coverage mapping | ~78s |
| `resource_bundles` | i18n properties files | ~8s |
| `xsd_cdt` | CDT type definitions → SAIL usage | ~7s |
| `gitlab_ci` | CI pipeline definitions | ~3s |
| `freemarker` | FTL template → Java caller | ~19s |
| `web_xml` | Servlet mappings, TLD tags, URI templates | ~6s |
| `groovy_tests` | Groovy/Spock test → class coverage | ~6s |

### CLI flags

| Flag | Purpose |
|------|---------|
| `--repo-root PATH` | Path to the `ae` repository (required) |
| `--only name1,name2` | Run only specific extractors (comma-separated) |
| `--no-clear` | Don't wipe the graph before running (incremental add) |
| `--stats` | Print node/edge counts after extraction |
| `--validate` | Sample 50 random nodes and verify their file paths exist |
| `--verbose` | Debug-level logging |

---

## Nuances & Important Details

### 1. The JAR scanning cache (`data/package-to-library-cache.json`)

**What:** Maps 11,000+ Java package names to their owning library coordinates. Built by scanning `~/.gradle/caches/` JARs.

**Why it matters:** Without this, the graph can't link `import org.eclipse.jetty.http.HttpField` to the library `org.eclipse.jetty:jetty-http`. The Java package name and Maven coordinate don't match predictably.

**Behavior:**
- First run: Scans ~830 JARs from Gradle cache (~90 seconds), saves result to cache file
- Subsequent runs: Loads from cache instantly (<1 second)
- Auto-regenerates if `deployment/gradle.lockfile` changes (detected via file hash)
- Falls back to a manual 100-entry mapping if cache doesn't exist AND Gradle cache is empty

**To maximize coverage:** Run `./gradlew testClasses` in the ae repo first (downloads all dependency JARs to `~/.gradle/caches/`). This gets coverage from ~93% to ~98%.

### 2. The Gradle dependency tree (`data/gradle-dependency-tree.txt`)

**What:** Full transitive dependency tree showing which library depends on which other library (e.g., `jackson-databind → jackson-core → jackson-annotations`).

**Why it matters:** Enables `LIB_DEPENDS_ON` edges — critical for answering "if I upgrade library X, what other libraries in the project transitively depend on it?"

**Behavior:**
- If the file exists: parsed instantly (~1 second), produces ~2,000 LIB_DEPENDS_ON edges
- If missing: auto-generates by running `./gradlew :module:dependencies` for all 948 modules (requires VPN, takes 15-20 minutes)
- Gitignored (too large, machine-specific)

**To generate manually (faster than auto):**
```bash
cd ~/repo/ae
./gradlew :deployment:dependencies > ~/repo/AEKnowledgeGraph/knowledge-graph/data/gradle-dependency-tree.txt 2>/dev/null
# For more complete coverage, also scan key modules:
./gradlew :appian-libraries:epex:dependencies >> ~/repo/AEKnowledgeGraph/knowledge-graph/data/gradle-dependency-tree.txt 2>/dev/null
./gradlew :appian-services:rdo-in-ae-service:dependencies >> ~/repo/AEKnowledgeGraph/knowledge-graph/data/gradle-dependency-tree.txt 2>/dev/null
./gradlew :test:dependencies >> ~/repo/AEKnowledgeGraph/knowledge-graph/data/gradle-dependency-tree.txt 2>/dev/null
```

### 3. The `--no-clear` flag

**Without `--no-clear`:** Deletes the entire graph first, then rebuilds from scratch. Use for daily full rebuilds.

**With `--no-clear`:** Adds to the existing graph without deleting. Use when:
- You're developing/testing a single extractor and don't want to re-run all 21
- You've just added a new extractor and want to see its results without waiting 7 minutes

**Caution:** Running the same extractor twice with `--no-clear` may create duplicate edges (most extractors use MERGE so this is usually safe, but not guaranteed for all relationship types).

### 4. Neo4j goes down between sessions

Colima's VM and the Neo4j container persist across machine reboots, but may stop if:
- You ran `colima stop`
- The machine crashed
- Docker ran out of disk space

**To restart:**
```bash
colima start
docker-compose -f ~/repo/AEKnowledgeGraph/knowledge-graph/docker-compose.yml up -d
# Wait 30 seconds for Neo4j to become healthy
```

**The graph data persists** in the Docker volume. You don't need to re-run extraction unless the source code changed.

### 5. Feature toggle constant resolution

Toggles in this codebase are referenced via Java constants, not direct strings:
```java
static final String MY_TOGGLE = "ae.feature.x";
// ... later ...
isFeatureEnabled(MY_TOGGLE);
```

The extractor does a two-pass scan: first finds all constant definitions, then resolves `isFeatureEnabled(CONSTANT_NAME)` calls against the constant map. This catches ~85% of toggle usage. The remaining 15% use dynamic variable passing that can't be resolved statically.

### 6. Spring DI captures ALL injection paths

The graph captures three injection mechanisms:
- `@Autowired` fields
- Constructor parameters (on `@Component`/`@Service`/`@Repository` classes)
- `@Bean` method parameters (Spring injects dependencies into `@Bean` method params)

When an interface has multiple implementations, ALL implementations get `INJECTS` edges with metadata:
- `isPrimary: true/false` — which one has `@Primary`
- `qualifier: "value"` — `@Qualifier` annotation value
- `interfaceType: "com.x.InterfaceName"` — the interface being injected

### 7. Module discovery scans ALL directories

The Gradle extractor scans these directories recursively for `.gradle`/`.gradle.kts` files:
- `appian-libraries/` (main product code)
- `appian-services/` (standalone services)
- `test/` (test infrastructure)
- `server/` (K engines)
- `infra/` (CI, toggles)
- `deployment/` (webapp packaging)
- `javadocs/`
- `log-collection/`

Total: 948 modules discovered. Build infrastructure (`build-logic/`, `configure/`, `gradle/scripts/`) is intentionally excluded — no product source code there.

---

## Exposing to AI Agents (MCP)

### Setup

1. Install the MCP server:
```bash
cd knowledge-graph && source .venv/bin/activate
pip install mcp-neo4j-cypher
```

2. Add to Kiro MCP config (`.kiro/settings/mcp.json` in the ae repo):
```json
{
  "mcpServers": {
    "neo4j-knowledge-graph": {
      "command": "/path/to/AEKnowledgeGraph/knowledge-graph/.venv/bin/mcp-neo4j-cypher",
      "args": [],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "knowledge"
      }
    }
  }
}
```

3. The agent also needs the schema reference. Create `.kiro/steering/knowledge-graph-schema.md` with `inclusion: always` — this ensures the agent always has node labels and relationship types in context (see existing file in the ae repo on the `knowledge-graph-mcp-integration` branch).

### Agent tools available

| MCP Tool | Purpose |
|----------|---------|
| `read_neo4j_cypher` | Run read-only Cypher queries |
| `write_neo4j_cypher` | Run write queries (rarely needed) |
| `get_neo4j_schema` | Get live schema from Neo4j |

---

## Graph Statistics (current state)

| Node Type | Count |
|-----------|-------|
| JavaClass | ~50,000 |
| SailRule | ~17,000 |
| ExpressionTest | ~7,400 |
| TsFile | ~5,500 |
| Package | ~4,400 |
| ResourceBundle | ~1,900 |
| CdtType | ~1,575 |
| JspPage | ~1,500 |
| Module | 948 |
| ExternalLibrary | ~895 |
| FeatureToggle | ~627 |
| KFile | 508 |
| GroovyTest | 417 |
| NpmPackage | 316 |
| ApiEndpoint | ~276 |
| Table | 268 |
| Entity | ~249 |
| FtlTemplate | 253 |
| Team | 26 |
| DockerService | 16 |
| **Total** | **~94,100** |

| Relationship Type | Count | What it represents |
|-------------------|-------|-------------------|
| IMPORTS | ~415,000 | Java import statements |
| CONTAINS | ~93,000 | Hierarchy (Module→Package→Class) |
| CALLS | ~78,000 | SAIL rule→rule invocations |
| CALLS_BUILTIN | ~70,000 | SAIL→Java function bridge |
| TESTS_RULE | ~31,000 | Expression test→SAIL rule |
| USES_LIBRARY | ~28,000 | Class/Module→External library |
| INJECTS | ~18,000 | Spring DI wiring |
| EXTENDS | ~11,000 | Java inheritance |
| IMPLEMENTS | ~9,000 | Interface implementation |
| DEPENDS_ON | ~7,100 | Gradle module→module |
| PRODUCES | ~7,000 | @Bean method→return type |
| REFERENCES_CDT | ~5,600 | SAIL rule→CDT type |
| DEPENDS_ON_LIB | ~5,400 | Module→external library (declared) |
| SPRING_IMPORTS | ~4,000 | @Import on Spring configs |
| TESTS_CLASS | ~2,900 | Groovy test→Java class |
| LIB_DEPENDS_ON | ~2,100 | Library→library transitivity |
| TS_IMPORTS | ~2,200 | TypeScript ES module imports |
| JSP_INCLUDES | ~1,800 | JSP include directives |
| GATES | ~1,700 | Feature toggle→gated code |
| Others | ~3,000 | RENDERS, CSS_IMPORTS, LOADS, FK_TO, etc. |
| **Total** | **~797,000** |

---

## Limitations (what the graph CANNOT tell you)

- **Runtime dispatch** — when multiple implementations exist with no @Primary, Spring picks at runtime
- **Reflection** — `Class.forName(variable)` is unknowable statically
- **Event observers** — who fires events is runtime behavior
- **Kafka topic coupling** — producer↔consumer linked by config strings, often not co-located
- **Plugin-loaded code** — JARs loaded dynamically at runtime
- **Record type relationships** — configured in running Appian, not in source code
- **~7% of library imports** — JARs not in local Gradle cache can't be scanned
- **Dynamic SAIL expressions** — `fn!call_appian_internal(variableRef, ...)` is runtime-resolved

---

## Project Structure

```
AEKnowledgeGraph/
├── README.md                        # This file
├── .gitignore
├── ARCHITECTURE.md                  # Graph schema design decisions
├── RELATIONSHIP_MATRIX.md           # Complete inventory of all extractable relationships
├── IMPLEMENTATION_PLAN.md           # Original phased build plan
├── EXTRACTION_STRATEGIES.md         # Per-language parsing details
├── QUERY_PATTERNS.md                # Cypher cookbook for impact analysis
├── FUTURE_SCOPE.md                  # Dynamic tracing, temporal analysis roadmap
├── VALIDATION_TEST_PLAN.md          # How to prove ROI (or kill the project)
├── TEST_CASES.md                    # 24 real-world test scenarios with expected outputs
├── GETTING_STARTED.md               # Step-by-step setup guide
│
└── knowledge-graph/                 # Implementation
    ├── docker-compose.yml           # Neo4j 5.26 + APOC
    ├── .env                         # Neo4j credentials (gitignored)
    ├── .env.example                 # Template for credentials
    ├── .gitignore
    ├── requirements.txt             # Python dependencies
    ├── extract_all.py               # Main orchestrator (21 extractors)
    ├── data/
    │   ├── package-to-library-cache.json   # JAR scan cache (committed)
    │   └── gradle-dependency-tree.txt      # Transitive deps (gitignored, auto-generated)
    ├── graph/
    │   ├── __init__.py
    │   ├── neo4j_client.py          # Connection + batch write helpers
    │   └── schema.py                # Index/constraint definitions
    └── extractors/
        ├── __init__.py              # BaseExtractor ABC
        ├── gradle_extractor.py
        ├── java_extractor.py
        ├── sail_extractor.py
        ├── cross_language_binder.py
        ├── spring_di_extractor.py
        ├── external_lib_extractor.py
        ├── library_usage_mapper.py
        ├── lib_transitivity_extractor.py
        ├── npm_extractor.py
        ├── typescript_extractor.py
        ├── k_extractor.py
        ├── db_extractor.py
        ├── toggle_extractor.py
        ├── infra_extractor.py
        ├── expression_test_extractor.py
        ├── resource_bundle_extractor.py
        ├── xsd_cdt_extractor.py
        ├── gitlab_ci_extractor.py
        ├── freemarker_extractor.py
        ├── web_xml_extractor.py
        └── groovy_test_extractor.py
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Cannot connect to Neo4j` | `colima start && docker-compose up -d && sleep 30` |
| `Module not found: neo4j` | `source .venv/bin/activate` |
| `JAR scan found 0 mappings` | Run `./gradlew testClasses` in ae repo first (populates Gradle cache) |
| `LIB_DEPENDS_ON has 0 edges` | Delete `data/gradle-dependency-tree.txt` and re-run `lib_transitivity` (requires VPN) |
| `MCP server not connecting` | Check Neo4j is running: `docker-compose ps` should show "healthy" |
| `Agent uses wrong node labels` | Ensure `.kiro/steering/knowledge-graph-schema.md` exists with `inclusion: always` |
| `Graph is stale after rebase` | Re-run `python3 extract_all.py --repo-root ~/repo/ae --stats` |
| Port 7474/7687 already in use | Another Neo4j is running — stop it or change ports in docker-compose.yml |
| `colima start` fails | Try `colima delete && colima start` to recreate the VM |
