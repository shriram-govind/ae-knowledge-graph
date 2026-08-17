# Architecture — AE Knowledge Graph

## Design Decisions

### 1. Single Graph, Multi-Granularity

We use one Neo4j database with multiple node labels representing different granularity levels. A `CONTAINS` relationship hierarchy connects them:

```
Module → Package → JavaClass
Module → SailRule
Module → TsFile
Module → KFile
Module → JspPage
```

Agents query at whatever level they need. "What modules are affected?" traverses at module level. "What specific classes break?" traverses at class level. Same graph, different depth.

**Why not separate graphs per granularity:**
- Neo4j Community Edition supports only one database
- Cross-granularity queries are impossible with separate graphs
- A single graph enables "which *specific class* in module X depends on which *specific rule* in module Y?"

### 2. Every Edge Must Be Provable

Every relationship in the graph has these properties:
- `sourceFile` — The file where this relationship was extracted from
- `sourceLine` — The line number (where applicable)
- `extractedBy` — Which extractor produced this edge (for debugging)

If a relationship cannot be proven from static source code, it does NOT go in the graph. No heuristics, no probabilistic edges.

### 3. Cross-Language Bindings via String Matching

The `ae` repo uses several string-mediated cross-language contracts:
- Java `FN_ID = "functionName"` ↔ SAIL `a!functionName()`
- Java Reaction key `"key"` ↔ SAIL `a!externalReaction("key")`
- Java `@Path("/endpoint")` ↔ OpenAPI `paths: /endpoint`
- SAIL `'type!{ns}TypeName'` ↔ XSD `<complexType name="TypeName">`

Both sides of these contracts are literal strings in source code, making them statically provable.

### 4. Idempotent Full Rebuild

The extraction pipeline does a complete rebuild each run:
1. Clear the graph (`MATCH (n) DETACH DELETE n`)
2. Create indexes/constraints
3. Run each extractor in sequence
4. Post-hoc validation pass

This is simpler than incremental updates and guarantees consistency. A full rebuild takes ~5-10 minutes for the entire `ae` repo.

### 5. Future-Proofed for Dynamic Analysis

Every relationship has a `source` property defaulting to `'static'`. Future dynamic trace ingestion can add edges with `source: 'dynamic'`. This enables:
- Validation: static edge never observed at runtime → possibly dead code
- Discovery: dynamic edge with no static counterpart → reflection/string-mediated path

---

## Node Types

### `:Module`

A Gradle module — the primary unit of build-time dependency.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Module name (e.g., `records-java`) |
| `path` | String | Filesystem path relative to repo root |
| `gradlePath` | String | Full Gradle path (e.g., `:appian-libraries:records:records-java`) |
| `language` | String | Primary language (`java`, `sail`, `typescript`, `k`, `mixed`) |

**Source:** Parsed from `settings.gradle` and individual `*.gradle` files.

---

### `:Package`

A Java or TypeScript package/directory grouping.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Package name (e.g., `com.appiancorp.record.domain`) |
| `path` | String | Filesystem path |

**Source:** Derived from directory structure under `src/main/java/`.

---

### `:JavaClass`

A Java class, interface, enum, or annotation.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Simple class name |
| `fqn` | String | Fully qualified name (e.g., `com.appiancorp.record.RecordWriteService`) |
| `path` | String | File path relative to repo root |
| `kind` | String | `class`, `interface`, `enum`, `annotation` |
| `isAbstract` | Boolean | Whether the class is abstract |
| `isSpringConfig` | Boolean | Has `@Configuration` annotation |
| `hasSpringBeans` | Boolean | Contains `@Bean` methods |

**Source:** AST-parsed from `*.java` files using tree-sitter-java.

---

### `:SailRule`

A SAIL system rule defined in an XML file.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Rule name (e.g., `rtd_recordEvents_main`) |
| `uuid` | String | UUID (e.g., `SYSTEM_SYSRULES_rtd_recordEvents_main`) |
| `path` | String | File path |
| `parentUuid` | String | Parent folder UUID |
| `isSystemOnly` | Boolean | Whether marked `systemOnly: fn!true()` |
| `functionCategory` | String | Category if defined |

**Source:** Parsed from `SYSTEM_SYSRULES_*.xml` files using lxml.

---

### `:TsFile`

A TypeScript/JavaScript/React file.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | File name |
| `path` | String | File path relative to repo root |
| `isComponent` | Boolean | Whether it exports a React component |
| `isTest` | Boolean | Whether it's a test file |

**Source:** Parsed from `*.ts`, `*.tsx`, `*.js`, `*.jsx` files using tree-sitter-typescript.

---

### `:KFile`

A K language source file.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | File name |
| `path` | String | File path |
| `engine` | String | Which K engine (`personalization`, `exec`, `design`, `collaboration`, etc.) |

**Source:** Parsed from `*.k` files under `server/`.

---

### `:JspPage`

A JSP (JavaServer Pages) file.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | File name |
| `path` | String | File path |

**Source:** Parsed from `*.jsp` files under `deployment/web.war/`.

---

### `:Table`

A database table defined via Liquibase.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Table name (e.g., `record_type`) |
| `changelogFile` | String | Liquibase YAML file that created it |
| `migrationId` | String | Migration ID |

**Source:** Parsed from Liquibase YAML changelogs (`db/changelog/*.yaml`).

---

### `:Entity`

A JPA entity class (dual-labeled as `:JavaClass:Entity`).

| Property | Type | Description |
|----------|------|-------------|
| `tableName` | String | The `@Table(name=...)` value |
| *(inherits all JavaClass properties)* | | |

**Source:** Detected via `@Entity` + `@Table` annotations during Java AST extraction.

---

### `:FeatureToggle`

A feature flag / toggle.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Toggle key (e.g., `ae.records-powered-ui.record-documents`) |
| `defaultValue` | String | Default value from properties file |

**Source:** Parsed from `infra/feature-toggles.properties`.

---

### `:ApiEndpoint`

A REST API endpoint.

| Property | Type | Description |
|----------|------|-------------|
| `method` | String | HTTP method (`GET`, `POST`, `PUT`, `DELETE`) |
| `path` | String | URL path (e.g., `/record-types/{uuid}/fields`) |
| `specFile` | String | OpenAPI spec file it's defined in |

**Source:** Parsed from OpenAPI YAML specs under `api-specs/`.

---

### `:CiPipeline`

A CI pipeline or job definition.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Pipeline/job name |
| `filePath` | String | GitLab CI YAML file |
| `triggerPatterns` | String[] | File glob patterns that trigger this pipeline |

**Source:** Parsed from `.gitlab-ci*.yaml` and `infra/gitlab-ci/` files.

---

### `:Team`

A team from CODEOWNERS.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Team name (e.g., `squad-data-fabric`) |
| `handle` | String | GitHub handle (e.g., `@appian/squad-data-fabric`) |

**Source:** Parsed from `.github/CODEOWNERS`.

---

### `:DockerService`

A Docker Compose service definition.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Service name (e.g., `webapp`, `neo4j`) |
| `composeFile` | String | Docker Compose file path |
| `image` | String | Docker image |

**Source:** Parsed from `docker-compose*.yml` files.

---

### `:CdtType`

A Custom Data Type defined in XSD.

| Property | Type | Description |
|----------|------|-------------|
| `name` | String | Type name (e.g., `PagingInfo`) |
| `namespace` | String | XML namespace |
| `isHidden` | Boolean | Whether marked HIDDEN |

**Source:** Parsed from `system-record-types.xsd`.


---

## Relationship Types

### Structural (Containment Hierarchy)

| Relationship | From | To | Description |
|-------------|------|-----|-------------|
| `CONTAINS` | Module | Package | Module contains a Java package |
| `CONTAINS` | Package | JavaClass | Package contains a class |
| `CONTAINS` | Module | SailRule | Module contains a SAIL rule |
| `CONTAINS` | Module | TsFile | Module contains a TS/JS file |
| `CONTAINS` | Module | KFile | Module contains a K file |
| `CONTAINS` | Module | JspPage | Module contains a JSP page |

### Build-Time Dependencies

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `DEPENDS_ON` | Module | Module | `config`: implementation\|api\|appianApp\|testImplementation | Gradle module dependency |
| `NPM_DEPENDS_ON` | Module | Module | `type`: dependency\|devDependency | package.json dependency |

### Java Structural

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `IMPORTS` | JavaClass | JavaClass | | Java import statement |
| `EXTENDS` | JavaClass | JavaClass | | Class inheritance |
| `IMPLEMENTS` | JavaClass | JavaClass | | Interface implementation |
| `SPRING_IMPORTS` | JavaClass | JavaClass | | `@Import({X.class})` on Spring configs |
| `INJECTS` | JavaClass | JavaClass | `fieldName` | `@Autowired`/`@Inject` (only where single implementation exists) |

### SAIL Invocations

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `CALLS` | SailRule | SailRule | | `a!ruleName()` invocation |
| `CALLS_BUILTIN` | SailRule | JavaClass | `functionName` | SAIL calling a Java built-in function |
| `CALLS_REACTION` | SailRule | JavaClass | `reactionKey` | `a!externalReaction("key")` |
| `REFERENCES_CDT` | SailRule | CdtType | | `'type!{ns}TypeName'` usage |

### TypeScript/React

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `TS_IMPORTS` | TsFile | TsFile | `importedName` | ES module import |
| `RENDERS` | TsFile | TsFile | `componentName` | JSX `<Component>` usage |

### K Language

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `LOADS` | KFile | KFile | | `\l filename` directive |
| `K_CALLS` | KFile | KFile | `functionName` | Cross-file function call |

### JSP

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `JSP_INCLUDES` | JspPage | JspPage | `directive`: include\|jsp:include | File inclusion |

### Data Layer

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `MAPS_TO` | Entity | Table | | JPA `@Table(name=...)` mapping |
| `FK_TO` | Table | Table | `constraintName`, `columnName` | Foreign key relationship |

### Feature Toggles

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `GATES` | FeatureToggle | JavaClass\|SailRule | `usage`: isFeatureEnabled\|getBoolean | Code gated by toggle |
| `DEFINED_IN` | FeatureToggle | JavaClass | `constantName` | Where toggle constant lives |

### API Surface

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `IMPLEMENTED_BY` | ApiEndpoint | JavaClass | | `@Path` annotation match |
| `SPECIFIED_IN` | ApiEndpoint | Module | | Which OpenAPI spec file |

### Infrastructure

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `SERVICE_DEPENDS_ON` | DockerService | DockerService | | Docker Compose `depends_on` |
| `CI_TRIGGERED_BY` | CiPipeline | Module | `pattern` | GitLab CI `changes:` glob match |

### Ownership

| Relationship | From | To | Properties | Description |
|-------------|------|-----|------------|-------------|
| `OWNS` | Team | Module | `pattern` | CODEOWNERS path match |

---

## Indexes and Constraints

```cypher
// Unique constraints
CREATE CONSTRAINT FOR (m:Module) REQUIRE m.gradlePath IS UNIQUE;
CREATE CONSTRAINT FOR (c:JavaClass) REQUIRE c.fqn IS UNIQUE;
CREATE CONSTRAINT FOR (r:SailRule) REQUIRE r.uuid IS UNIQUE;
CREATE CONSTRAINT FOR (t:Table) REQUIRE t.name IS UNIQUE;
CREATE CONSTRAINT FOR (ft:FeatureToggle) REQUIRE ft.name IS UNIQUE;

// Indexes for fast lookup
CREATE INDEX FOR (c:JavaClass) ON (c.name);
CREATE INDEX FOR (c:JavaClass) ON (c.path);
CREATE INDEX FOR (r:SailRule) ON (r.name);
CREATE INDEX FOR (f:TsFile) ON (f.path);
CREATE INDEX FOR (k:KFile) ON (k.path);
CREATE INDEX FOR (j:JspPage) ON (j.path);
CREATE INDEX FOR (p:Package) ON (p.name);
CREATE INDEX FOR (m:Module) ON (m.name);
CREATE INDEX FOR (a:ApiEndpoint) ON (a.path);
```

---

## Graph Visualization (Example Subgraph)

```
                    ┌─────────────┐
                    │ Team:       │
                    │ data-fabric │
                    └──────┬──────┘
                           │ OWNS
                           ▼
┌──────────┐  DEPENDS_ON  ┌──────────────┐  DEPENDS_ON  ┌──────────┐
│records-db├─────────────►│ records-java ├─────────────►│ ae-common│
└──────────┘              └──────┬───────┘              └──────────┘
                                 │ CONTAINS
                                 ▼
                    ┌─────────────────────────┐
                    │ JavaClass:              │
                    │ RecordWriteService      │
                    └──────┬──────────────────┘
                           │ IMPLEMENTS
                           ▼
                    ┌─────────────────────────┐
                    │ JavaClass:              │
                    │ RecordWriteServiceImpl  │──── INJECTS ──► ImmediateSyncService
                    └──────┬──────────────────┘
                           │
              ┌────────────┼────────────────┐
              │ CALLS_BUILTIN              │ MAPS_TO
              ▼                            ▼
    ┌──────────────┐              ┌──────────────┐
    │ SailRule:    │              │ Table:       │
    │ writeRecords │              │ record_type  │
    └──────────────┘              └──────┬───────┘
                                         │ FK_TO
                                         ▼
                                  ┌──────────────┐
                                  │ Table:       │
                                  │ record_list_ │
                                  │ action_cfg   │
                                  └──────────────┘
```
