# Real-World Test Cases — AE Knowledge Graph

These test cases use actual classes, modules, and relationships from the `ae` codebase to validate the knowledge graph produces correct, actionable results.

Run each query via `read_neo4j_cypher` MCP tool.

---

## Category 1: Impact Analysis (What breaks if I change X?)

### Test 1.1: Change a core service class

**Developer prompt:**
> "I'm about to change the retry logic in ImmediateSyncServiceImpl. What other files will I need to check for breakage?"

**Scenario:** You're modifying `ImmediateSyncServiceImpl.java` — changing the retry logic for failed syncs.

**Query:**
```cypher
MATCH (target:JavaClass {name: 'ImmediateSyncServiceImpl'})<-[:IMPORTS|EXTENDS|IMPLEMENTS|INJECTS*1..2]-(affected:JavaClass)
WHERE affected.isTest = false
RETURN DISTINCT affected.name, affected.path, affected.module
ORDER BY affected.name
```

**Expected output should include:**
- `RecordWriteServiceImpl` (imports ImmediateSyncService)
- `ReplicaUpdateTransactionProcess` (uses sync service)
- Spring config classes that wire it
- Classes in `records-sync-java` module

**Why this matters:** A change to sync retry could break write operations, replica update processes, and anything in the sync pipeline.

---

### Test 1.2: Change a SAIL system rule

**Developer prompt:**
> "I need to add a new required input to rtd_recordEvents_configuredState. What rules call it that would break?"

**Scenario:** You're adding a required parameter to `rtd_recordEvents_configuredState`.

**Query:**
```cypher
MATCH (target:SailRule {name: 'rtd_recordEvents_configuredState'})<-[:CALLS*1..3]-(caller:SailRule)
RETURN DISTINCT caller.name, caller.path
ORDER BY caller.name
```

**Expected output should include:**
- `rtd_recordEvents` (direct caller)
- Any rules that call `rtd_recordEvents` transitively

**Why this matters:** Adding a required param breaks all callers that don't pass it.

---

### Test 1.3: Change a database table

**Developer prompt:**
> "I'm adding a new column to the record_type table. What entities and Java code would be affected? What about child tables with FK relationships?"

**Scenario:** You're adding a column to the `record_type` table.

**Query:**
```cypher
MATCH (table:Table {name: 'record_type'})
OPTIONAL MATCH (table)<-[:FK_TO]-(childTable:Table)
OPTIONAL MATCH (entity:Entity)-[:MAPS_TO]->(table)
OPTIONAL MATCH (entity)<-[:IMPORTS]-(consumer:JavaClass)
WHERE consumer.isTest = false
RETURN table.name,
       collect(DISTINCT childTable.name) AS childTables,
       collect(DISTINCT entity.name) AS entities,
       collect(DISTINCT consumer.name)[0..10] AS topConsumers
```

**Expected output should include:**
- Entity: `RecordTypeDefinition`
- Child tables via FK (action configs, view configs, etc.)
- Consumer classes: DAOs, services, converters

---

### Test 1.4: Change a Spring configuration

**Developer prompt:**
> "I'm adding a new bean to RecordQuerySpringConfig. What other configs import this one? What does it already produce?"

**Scenario:** You're modifying `RecordQuerySpringConfig` to add a new bean.

**Query:**
```cypher
MATCH (config:JavaClass {name: 'RecordQuerySpringConfig'})
OPTIONAL MATCH (config)-[:SPRING_IMPORTS]->(imported)
OPTIONAL MATCH (config)-[:PRODUCES]->(bean)
OPTIONAL MATCH (config)<-[:SPRING_IMPORTS]-(importer)
RETURN config.name,
       collect(DISTINCT imported.name) AS imports,
       collect(DISTINCT bean.name) AS produces,
       collect(DISTINCT importer.name) AS importedBy
```

**Expected output:** Shows what the config already imports, what beans it produces, and what other configs depend on it.

---

## Category 2: Library Upgrade Assessment

### Test 2.1: Upgrade jackson-databind

**Developer prompt:**
> "Security flagged a CVE in jackson-databind. How many modules and classes use it? What other libraries depend on it that might also break?"

**Scenario:** Security team requires upgrading jackson-databind to patch a CVE.

**Query:**
```cypher
MATCH (lib:ExternalLibrary {coordinate: 'com.fasterxml.jackson.core:jackson-databind'})
OPTIONAL MATCH (cls:JavaClass)-[:USES_LIBRARY]->(lib)
OPTIONAL MATCH (mod:Module)-[:USES_LIBRARY]->(lib)
OPTIONAL MATCH (parent:ExternalLibrary)-[:LIB_DEPENDS_ON]->(lib)
RETURN lib.latestVersion AS currentVersion,
       count(DISTINCT cls) AS classesAffected,
       count(DISTINCT mod) AS modulesAffected,
       collect(DISTINCT parent.coordinate) AS librariesDependingOnIt
```

**Expected output:**
- 60+ modules affected
- 200+ classes importing from it
- Multiple other libraries depending on it (jackson-datatype, jackson-jaxrs, etc.)

---

### Test 2.2: Upgrade React

**Developer prompt:**
> "We need to upgrade React. Which modules use it and what version is each on?"

**Scenario:** Frontend team wants to upgrade React from 17 to 19.

**Query:**
```cypher
MATCH (react:NpmPackage {name: 'react'})<-[:NPM_DEPENDS_ON]-(workspace:NpmPackage)
WHERE workspace.isWorkspace = true
RETURN workspace.name AS module, workspace.version AS reactVersion
UNION ALL
MATCH (pkg:NpmPackage)-[:NPM_DEPENDS_ON]->(react:NpmPackage {name: 'react-dom'})
WHERE pkg.isWorkspace = true
RETURN pkg.name AS module, pkg.version AS reactVersion
```

**Expected output:**
- `sail-client` (React 17)
- `sail-client-native` (React 19)
- `email-renderer` (React 16)
- `appian-desktop-application` (React 19)

---

### Test 2.3: Find all React ecosystem libraries needing upgrade

**Developer prompt:**
> "List all react-* libraries in the codebase with their versions and which package uses them. I need to know which ones will break with React 19."

**Query:**
```cypher
MATCH (pkg:NpmPackage)
WHERE pkg.name STARTS WITH 'react-' AND NOT pkg.isWorkspace
MATCH (workspace:NpmPackage)-[:NPM_DEPENDS_ON]->(pkg)
WHERE workspace.isWorkspace = true
RETURN pkg.name AS library, pkg.version, workspace.name AS usedBy
ORDER BY pkg.name
```

**Expected output:** All react-redux, react-konva, react-dnd, react-select, etc. with versions and which workspace uses them.

---

## Category 3: Cross-Language Tracing

### Test 3.1: SAIL → Java function bridge

**Developer prompt:**
> "What Java class implements the a!queryRecordType function? I need to look at its source."

**Scenario:** You need to find what Java code backs `a!queryRecordType()`.

**Query:**
```cypher
MATCH (s:SailRule)-[r:CALLS_BUILTIN]->(j:JavaClass)
WHERE r.functionName = 'queryRecordType'
RETURN j.name, j.fqn, j.path
```

**Expected output:** One or more Java classes with FN_ID registration for "queryRecordType".

---

### Test 3.2: SAIL reaction → Java handler

**Developer prompt:**
> "I see a!externalReaction('record.replica.security.refresh') in a SAIL rule. What Java class handles this reaction and which SAIL rule triggers it?"

**Scenario:** You see `a!externalReaction("record.replica.security.refresh")` in SAIL and need the Java implementation.

**Query:**
```cypher
MATCH (s:SailRule)-[r:CALLS_REACTION]->(j:JavaClass)
WHERE r.reactionKey = 'record.replica.security.refresh'
RETURN s.name AS sailCaller, j.name AS javaHandler, j.path
```

**Expected output:**
- sailCaller: `rtd_rls_refreshSecurityPolicy`
- javaHandler: `RefreshSecurityPolicyReaction`

---

### Test 3.3: End-to-end trace: SAIL → Java → DB

**Developer prompt:**
> "Trace the full path from the writeRecords SAIL rule down to the database tables it touches. I want to understand the entire write path."

**Scenario:** Trace what database tables are touched when `writeRecords` SAIL rule executes.

**Query:**
```cypher
MATCH (sail:SailRule {name: 'writeRecords'})-[:CALLS_BUILTIN]->(java:JavaClass)
MATCH (java)<-[:IMPORTS*1..3]-(service:JavaClass)
WHERE service.name CONTAINS 'Write' OR service.name CONTAINS 'Sync'
MATCH (service)-[:IMPORTS]->(entity:Entity)
MATCH (entity)-[:MAPS_TO]->(table:Table)
RETURN DISTINCT sail.name AS sailRule, java.name AS javaFunction,
       entity.name AS entity, table.name AS dbTable
```

**Expected output:** Chain from SAIL writeRecords → Java Write function → WriteServiceImpl → Entity → table.

---

## Category 4: Feature Toggle Analysis

### Test 4.1: Scope of a toggle

**Developer prompt:**
> "We're about to enable the ae.records-powered-ui.record-documents toggle in production. What code will activate? I need a full list of affected classes and rules."

**Scenario:** You're enabling `ae.records-powered-ui.record-documents` and need to know what lights up.

**Query:**
```cypher
MATCH (t:FeatureToggle)-[:GATES]->(code)
WHERE t.name = 'ae.records-powered-ui.record-documents'
RETURN labels(code)[0] AS type, code.name, code.path
ORDER BY labels(code)[0], code.name
```

**Expected output:** Java classes and SAIL rules gated by this toggle.

---

### Test 4.2: Most impactful toggles

**Developer prompt:**
> "Which feature toggles gate the most code? I want to understand which ones are riskiest to flip."

**Scenario:** Which toggles gate the most code (highest risk to enable/disable)?

**Query:**
```cypher
MATCH (t:FeatureToggle)-[:GATES]->(code)
WITH t, count(code) AS gatedCount
ORDER BY gatedCount DESC
LIMIT 10
RETURN t.name, gatedCount
```

**Expected output:** Top 10 toggles by number of code units they gate.

---

## Category 5: Team Ownership & Responsibility

### Test 5.1: Who owns this code?

**Developer prompt:**
> "I found a bug in RecordWriteServiceImpl. Which team should I assign the Jira ticket to?"

**Scenario:** You found a bug in `RecordWriteServiceImpl` and need to know which team to assign it to.

**Query:**
```cypher
MATCH (c:JavaClass {name: 'RecordWriteServiceImpl'})<-[:CONTAINS*]-(m:Module)<-[:OWNS]-(t:Team)
RETURN c.name, m.name AS module, t.name AS team, t.handle
```

**Expected output:** The team that owns the records-java module.

---

### Test 5.2: What does a team own?

**Developer prompt:**
> "What modules does the data-fabric team own? I need to understand their scope."

**Scenario:** List everything owned by the data-fabric team.

**Query:**
```cypher
MATCH (t:Team)-[:OWNS]->(m:Module)
WHERE t.name CONTAINS 'data-fabric'
RETURN m.name AS module, m.path
ORDER BY m.name
```

**Expected output:** All modules owned by the data-fabric team.

---

## Category 6: Test Coverage Queries

### Test 6.1: What tests cover a SAIL rule?

**Developer prompt:**
> "I changed rtd_recordEvents_configuredState. What expression tests should I run to make sure it still works?"

**Scenario:** You changed `rtd_recordEvents_configuredState` and need to know what tests to run.

**Query:**
```cypher
MATCH (t:ExpressionTest)-[:TESTS_RULE]->(r:SailRule {name: 'rtd_recordEvents_configuredState'})
RETURN t.path, t.testType
```

**Expected output:** Expression test files (unit and integration) that test this specific rule.

---

### Test 6.2: What Groovy tests cover a Java class?

**Developer prompt:**
> "I'm refactoring FullDependencyCalculator. Are there any Spock tests I need to update?"

**Scenario:** You're changing `FullDependencyCalculator` and need Spock tests.

**Query:**
```cypher
MATCH (t:GroovyTest)-[:TESTS_CLASS]->(c:JavaClass {name: 'FullDependencyCalculator'})
RETURN t.path, t.name, t.testFramework
```

**Expected output:** Groovy/Spock test files that import or target this class.

---

### Test 6.3: Untested SAIL rules (no expression tests)

**Developer prompt:**
> "Find me the biggest SAIL rules that have zero test coverage. These are our highest-risk areas."

**Scenario:** Find SAIL rules that have NO test coverage.

**Query:**
```cypher
MATCH (r:SailRule)
WHERE r.isSystemOnly = false
AND NOT exists { MATCH (:ExpressionTest)-[:TESTS_RULE]->(r) }
RETURN r.name, r.path, r.definitionLineCount
ORDER BY r.definitionLineCount DESC
LIMIT 20
```

**Expected output:** Large untested SAIL rules (potential risk areas).

---

## Category 7: Spring DI / Architecture Queries

### Test 7.1: What gets injected into a class?

**Developer prompt:**
> "Show me all the dependencies that get injected into RecordEventsGenerationManagerImpl. I want to understand its coupling."

**Scenario:** Understanding all dependencies of `RecordEventsGenerationManagerImpl`.

**Query:**
```cypher
MATCH (c:JavaClass {name: 'RecordEventsGenerationManagerImpl'})-[r:INJECTS]->(dep:JavaClass)
RETURN dep.name, r.injectionType, r.isPrimary, r.interfaceType, r.fieldName
ORDER BY dep.name
```

**Expected output:** All injected dependencies with injection type (field/constructor/bean_method_param).

---

### Test 7.2: Multiple implementations of an interface

**Developer prompt:**
> "Which interfaces in the codebase have multiple implementations? I want to understand where DI ambiguity could cause issues."

**Scenario:** Find interfaces with more than one implementation (potential DI ambiguity).

**Query:**
```cypher
MATCH (impl:JavaClass)-[:IMPLEMENTS]->(iface:JavaClass)
WHERE iface.kind = 'interface' AND iface.isTest = false
WITH iface, collect(impl.name) AS implementations, count(impl) AS implCount
WHERE implCount > 1 AND implCount < 10
RETURN iface.name, implCount, implementations
ORDER BY implCount DESC
LIMIT 15
```

**Expected output:** Interfaces like `SourceDataReader`, `DataSourceProvider`, etc. with their multiple implementations.

---

## Category 8: CDT / Type System Queries

### Test 8.1: What SAIL rules use PagingInfo CDT?

**Developer prompt:**
> "I'm changing the PagingInfo CDT. What SAIL rules reference it that I'd need to check?"

**Query:**
```cypher
MATCH (r:SailRule)-[:REFERENCES_CDT]->(cdt:CdtType {name: 'PagingInfo'})
RETURN r.name, r.path
ORDER BY r.name
LIMIT 20
```

**Expected output:** SAIL rules that instantiate or reference the PagingInfo CDT.

---

### Test 8.2: Most-used CDTs

**Developer prompt:**
> "What are the most commonly used CDTs in our SAIL code? I want to understand the core data structures."

**Query:**
```cypher
MATCH (cdt:CdtType)<-[:REFERENCES_CDT]-(r:SailRule)
WITH cdt, count(r) AS usageCount
ORDER BY usageCount DESC
LIMIT 15
RETURN cdt.name, cdt.kind, cdt.fieldCount, usageCount
```

**Expected output:** Top CDTs by SAIL rule usage count (likely includes Map, PagingInfo, SortInfo, etc.)

---

## Category 9: Module Architecture Queries

### Test 9.1: Circular module dependencies

**Developer prompt:**
> "Are there any circular dependencies between modules? Find modules that depend on each other bidirectionally."

**Scenario:** Find modules that depend on each other (circular dependency).

**Query:**
```cypher
MATCH (a:Module)-[:DEPENDS_ON]->(b:Module)-[:DEPENDS_ON]->(a)
WHERE a.name < b.name
RETURN a.name, b.name
LIMIT 10
```

**Expected output:** Module pairs with bidirectional dependencies (architecture smell).

---

### Test 9.2: Most coupled modules (highest dependency count)

**Developer prompt:**
> "Which modules have the most outgoing dependencies? These are our most coupled modules that are hardest to change independently."

**Query:**
```cypher
MATCH (m:Module)-[:DEPENDS_ON]->(dep:Module)
WITH m, count(dep) AS depCount
ORDER BY depCount DESC
LIMIT 10
RETURN m.name, depCount
```

**Expected output:** Modules like `ae`, `records-java`, `test` that have the most dependencies.

---

### Test 9.3: Modules with most dependents (most critical)

**Developer prompt:**
> "What are the most critical modules — the ones with the most things depending on them? Changes to these need extra care."

**Query:**
```cypher
MATCH (m:Module)<-[:DEPENDS_ON]-(dependent:Module)
WITH m, count(dependent) AS dependentCount
ORDER BY dependentCount DESC
LIMIT 10
RETURN m.name, dependentCount
```

**Expected output:** `ae` (300+), `expression-evaluator` (290+), `asl` (198+), etc.

---

## Category 10: Infrastructure Queries

### Test 10.1: Docker service dependency chain

**Developer prompt:**
> "Show me the Docker service dependency graph. What depends on what in our docker-compose setup?"

**Query:**
```cypher
MATCH (s:DockerService)-[:SERVICE_DEPENDS_ON]->(dep:DockerService)
RETURN s.name AS service, dep.name AS dependsOn, s.composeFile
ORDER BY s.name
```

**Expected output:** Docker service dependency graph (webapp → db, etc.)

---

### Test 10.2: JSP include tree for a specific page

**Developer prompt:**
> "What JSPs does main.jsp include? I need to understand the full include chain to debug a rendering issue."

**Query:**
```cypher
MATCH path = (page:JspPage)-[:JSP_INCLUDES*1..3]->(included:JspPage)
WHERE page.name = 'main.jsp' OR page.name = 'tempo-index.jsp'
RETURN page.name, [n IN nodes(path) | n.name] AS includeChain
LIMIT 10
```

**Expected output:** The include chain from main JSP pages.

---

## Category 11: Dead Code Detection

### Test 11.1: Java classes with no imports (potential dead code)

**Developer prompt:**
> "Find Java classes that nothing imports, extends, implements, or injects. These might be dead code we can clean up."

**Query:**
```cypher
MATCH (c:JavaClass)
WHERE c.isTest = false AND c.kind = 'class'
AND NOT ()-[:IMPORTS]->(c)
AND NOT ()-[:EXTENDS]->(c)
AND NOT ()-[:IMPLEMENTS]->(c)
AND NOT ()-[:INJECTS]->(c)
AND NOT ()-[:CALLS_BUILTIN]->(c)
AND NOT c.name ENDS WITH 'SpringConfig'
RETURN c.name, c.path, c.module
ORDER BY c.module, c.name
LIMIT 30
```

**Expected output:** Classes with no incoming references — candidates for dead code review.

---

### Test 11.2: Orphaned SAIL rules (never called)

**Developer prompt:**
> "Find internal SAIL rules that are never called by anything else. These are orphaned rules we might be able to delete."

**Query:**
```cypher
MATCH (r:SailRule)
WHERE r.isSystemOnly = true
AND NOT ()-[:CALLS]->(r)
AND NOT ()-[:CALLS_BUILTIN]->(r)
AND r.definitionLineCount > 10
RETURN r.name, r.path, r.definitionLineCount
ORDER BY r.definitionLineCount DESC
LIMIT 20
```

**Expected output:** Internal SAIL rules with substantial code that are never called by anything — potential dead code.

---

## Scoring

For each test case, measure:
- **Correctness:** Does the output match reality? (Verify by reading the actual files)
- **Completeness:** Did it miss anything important?
- **Speed:** Query returns in < 2 seconds?
- **Actionability:** Can an agent directly use this output to make decisions?

Target: ≥80% of test cases produce correct, complete results in <2s.
