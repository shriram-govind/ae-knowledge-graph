"""
Java AST Extractor — Uses tree-sitter-java to extract the full type and method graph.

Type-level extraction:
- JavaClass nodes for class / interface / enum / annotation / record declarations,
  at any nesting depth (including types nested in interface, enum and annotation bodies)
- Package nodes and the Module → Package → JavaClass CONTAINS hierarchy
- IMPORTS         (import statements resolved to internal classes)
- EXTENDS         (class → superclass AND interface → super-interface)
- IMPLEMENTS      (class/enum/record → interface)
- SPRING_IMPORTS  (@Import on Spring @Configuration classes)
- Entity detection (@Entity + @Table(name=...))

Method-level extraction:
- Method nodes for every method and constructor, keyed by <ownerFqn>#<name>(<arity>)<sig>
- DECLARES        (JavaClass → Method)
- OVERRIDES       (Method → Method) resolved through the full supertype closure,
                  so an impl method links to the interface/abstract method it satisfies
- OVERLOADS       (Method → Method) between same-named methods on the same type

Type references are resolved to FQNs using: explicit imports, wildcard imports,
same-compilation-unit siblings, enclosing-type scope, same-package, and java.lang.

Uses tree-sitter for fast, reliable AST parsing that handles malformed files gracefully.
"""

import logging
import re
from pathlib import Path
from collections import defaultdict

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Initialize tree-sitter parser once
JAVA_LANGUAGE = Language(tsjava.language())

# Every node type that declares a type. `record_declaration` was previously absent,
# which dropped 1,108 types from the graph on the ae codebase.
TYPE_DECL_TYPES = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "annotation_type_declaration",
    "record_declaration",
)

# Bodies that can contain nested type declarations. The old extractor only looked at
# `class_body`, so nested types inside interfaces (441), enums (19) and annotation
# types (11) were never visited.
BODY_TYPES = (
    "class_body",
    "interface_body",
    "enum_body",
    "annotation_type_body",
)

# Types that appear where a supertype is expected.
TYPE_REF_NODES = ("type_identifier", "generic_type", "scoped_type_identifier")

# java.lang is implicitly imported; these never resolve to repo classes but are
# common enough that guessing `<package>.String` would create bogus edges.
JAVA_LANG_COMMON = {
    "Object", "String", "Integer", "Long", "Short", "Byte", "Character", "Boolean",
    "Double", "Float", "Number", "Math", "System", "Thread", "Runnable", "Comparable",
    "Iterable", "Cloneable", "AutoCloseable", "CharSequence", "Enum", "Record",
    "Class", "ClassLoader", "StringBuilder", "StringBuffer", "Throwable",
    "Exception", "RuntimeException", "Error", "IllegalArgumentException",
    "IllegalStateException", "NullPointerException", "UnsupportedOperationException",
    "IndexOutOfBoundsException", "ClassCastException", "NumberFormatException",
    "InterruptedException", "ThreadLocal", "Iterator", "Override", "Deprecated",
    "SuppressWarnings", "FunctionalInterface", "SafeVarargs", "Void",
}


def _get_parser() -> Parser:
    """Create a new parser instance (parsers are not thread-safe)."""
    return Parser(JAVA_LANGUAGE)


def _node_text(node) -> str:
    """Extract text content of a tree-sitter node."""
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _find_children(node, type_name: str) -> list:
    """Find all direct children of a given type."""
    return [c for c in node.children if c.type == type_name]


def _find_child(node, type_name: str):
    """Find first direct child of a given type."""
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _scoped_identifier_to_str(node) -> str:
    """Convert a scoped_identifier AST node to a dotted string."""
    if node is None:
        return ""
    if node.type == "identifier":
        return _node_text(node)
    if node.type == "scoped_identifier":
        parts = []
        for child in node.children:
            if child.type in ("identifier", "scoped_identifier"):
                parts.append(_scoped_identifier_to_str(child))
        return ".".join(parts)
    if node.type == "type_identifier":
        return _node_text(node)
    return _node_text(node)


def _type_ref_name(node) -> str | None:
    """
    Extract the referenced type's name from any node that can appear in an
    `extends` / `implements` position, discarding type arguments.

    Handles the three real shapes, all of which occur in the ae codebase:
        type_identifier         ->  "Foo"                (12,702 extends)
        generic_type            ->  "Foo<A,B>"  -> "Foo" (2,471 extends — was dropped)
        scoped_type_identifier  ->  "a.b.Foo"            (61 extends, 170 implements — was dropped)

    For scoped names the full dotted path is returned so the resolver can decide
    whether it is an FQN (`com.bar.Outer.Inner`) or an inner-class reference
    relative to an import (`Outer.Inner`).
    """
    if node is None:
        return None

    if node.type == "type_identifier":
        return _node_text(node)

    if node.type == "generic_type":
        # `Base<T>` -> descend to the raw type, which may itself be scoped.
        for child in node.children:
            if child.type in ("type_identifier", "scoped_type_identifier"):
                return _type_ref_name(child)
        return None

    if node.type == "scoped_type_identifier":
        # Reconstruct the dotted path, dropping any type_arguments / annotations.
        parts: list[str] = []
        for child in node.children:
            if child.type == "scoped_type_identifier":
                nested = _type_ref_name(child)
                if nested:
                    parts.append(nested)
            elif child.type == "type_identifier":
                parts.append(_node_text(child))
        return ".".join(parts) if parts else None

    return None


def _type_list_refs(container) -> list[str]:
    """
    Collect all type names from a `super_interfaces` / `extends_interfaces` node.

    Both wrap a `type_list` whose children are the individual interface refs.
    """
    if container is None:
        return []
    type_list = _find_child(container, "type_list")
    if type_list is None:
        return []
    names = []
    for child in type_list.children:
        if child.type in TYPE_REF_NODES:
            name = _type_ref_name(child)
            if name:
                names.append(name)
    return names


def _erase_type(node) -> str:
    """
    Produce an erased, normalised textual form of a type node, for method signatures.

    Erasure matters for override detection: an implementation may write
    `List<String>` where the interface writes `List<T>`, and `Object` where the
    interface is generic. Comparing erased simple names + arity is the pragmatic
    match criterion that works without full generic inference.
    """
    if node is None:
        return "?"
    t = node.type
    if t == "generic_type":
        for child in node.children:
            if child.type in ("type_identifier", "scoped_type_identifier"):
                return _erase_type(child)
        return "?"
    if t == "scoped_type_identifier":
        # Compare on the simple name so `java.util.List` == `List`.
        text = _type_ref_name(node) or "?"
        return text.rsplit(".", 1)[-1]
    if t == "array_type":
        element = None
        for child in node.children:
            if child.type != "dimensions":
                element = child
                break
        return _erase_type(element) + "[]"
    if t in ("type_identifier", "identifier"):
        return _node_text(node)
    if t in ("integral_type", "floating_point_type", "boolean_type", "void_type"):
        return _node_text(node)
    return _node_text(node) or "?"


def _param_types(formal_parameters) -> list[str]:
    """
    Extract the erased parameter types of a method/constructor.

    `spread_parameter` (varargs `int... xs`) is normalised to an array type so it
    matches an overriding method that declares `int[]`.
    """
    if formal_parameters is None:
        return []
    types: list[str] = []
    for p in formal_parameters.children:
        if p.type == "formal_parameter":
            type_node = None
            for c in p.children:
                if c.type in ("modifiers", "identifier", "dimensions"):
                    continue
                type_node = c
                break
            types.append(_erase_type(type_node))
        elif p.type == "spread_parameter":
            type_node = None
            for c in p.children:
                if c.type in ("modifiers", "variable_declarator", "identifier", "..."):
                    continue
                type_node = c
                break
            types.append(_erase_type(type_node) + "[]")
    return types


class JavaExtractor(BaseExtractor):
    """Extracts Java class-level structural relationships using tree-sitter AST parsing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populated after the parse pass; used by _resolve_type_to_fqn to decide
        # whether a candidate FQN corresponds to a real type in this repo.
        self._all_declared_fqns: set[str] = set()
        self._unresolved_supertypes: dict[str, int] = defaultdict(int)

    @property
    def name(self) -> str:
        return "Java AST Extractor"

    def extract(self):
        parser = _get_parser()

        # Collect all Java source files (main sources only, skip test/generated)
        java_files = self._find_java_files()
        logger.info(f"  Found {len(java_files)} Java source files to parse")

        # Data accumulators
        classes: list[dict] = []
        packages: dict[str, dict] = {}  # package_name → {name, path, module}
        imports: list[dict] = []
        extends: list[dict] = []
        implements: list[dict] = []
        spring_imports: list[dict] = []
        entities: list[dict] = []
        methods: list[dict] = []

        # Module mapping: file path → module gradle path
        module_map = self._build_module_map()

        # Parse each file
        parsed = 0
        failed = 0
        self._unresolved_supertypes: dict[str, int] = defaultdict(int)

        for java_file in java_files:
            try:
                result = self._parse_file(parser, java_file, module_map)
                if result:
                    classes.extend(result["classes"])
                    for pkg_name, pkg_info in result["packages"].items():
                        if pkg_name not in packages:
                            packages[pkg_name] = pkg_info
                    imports.extend(result["imports"])
                    extends.extend(result["extends"])
                    implements.extend(result["implements"])
                    spring_imports.extend(result["spring_imports"])
                    entities.extend(result["entities"])
                    methods.extend(result["methods"])
                    parsed += 1
            except Exception as e:
                failed += 1
                if failed <= 10:
                    logger.debug(f"  Failed to parse {java_file}: {e}")

        logger.info(f"  Parsed {parsed} files ({failed} failures)")

        # Deduplicate types by FQN. Two source files can declare the same FQN
        # (e.g. the same class under src/main and a platform-specific source set);
        # MERGE would collapse them anyway, so pick one deterministically.
        classes = self._dedupe_classes(classes)

        # ---- Resolution pass.
        # Now that every declared type is known, resolve the queued supertype names.
        self._all_declared_fqns = {c["fqn"] for c in classes}
        extends = self._resolve_pending(extends)
        implements = self._resolve_pending(implements)
        spring_imports = self._resolve_pending(spring_imports)

        logger.info(
            f"  Extracted {len(classes):,} types, {len(methods):,} methods/constructors, "
            f"{len(extends):,} extends, {len(implements):,} implements (resolved)"
        )

        # Write to Neo4j
        # 1. Package nodes
        pkg_nodes = [{"name": name, "path": info["path"]} for name, info in packages.items()]
        self.client.batch_create_nodes("Package", pkg_nodes, merge_key="name")
        logger.info(f"  Created {len(pkg_nodes)} Package nodes")

        # 2. JavaClass nodes
        self.client.batch_create_nodes("JavaClass", classes, merge_key="fqn")
        logger.info(f"  Created {len(classes)} JavaClass nodes")

        known_fqns = {c["fqn"] for c in classes}

        # 3. IMPORTS relationships
        valid_imports = self._filter_valid_relationships(imports, known_fqns)
        self.client.batch_create_relationships(
            "IMPORTS", valid_imports,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(f"  Created {len(valid_imports)} IMPORTS relationships (from {len(imports)} candidates)")

        # 4. EXTENDS relationships (class→superclass and interface→super-interface)
        valid_extends = self._filter_valid_relationships(extends, known_fqns)
        self.client.batch_create_relationships(
            "EXTENDS", valid_extends,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(
            f"  Created {len(valid_extends)} EXTENDS relationships "
            f"(from {len(extends)} candidates; remainder target classes outside this repo)"
        )

        # 5. IMPLEMENTS relationships
        valid_implements = self._filter_valid_relationships(implements, known_fqns)
        self.client.batch_create_relationships(
            "IMPLEMENTS", valid_implements,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(
            f"  Created {len(valid_implements)} IMPLEMENTS relationships "
            f"(from {len(implements)} candidates)"
        )

        # 6. SPRING_IMPORTS relationships
        valid_spring = self._filter_valid_relationships(spring_imports, known_fqns)
        self.client.batch_create_relationships(
            "SPRING_IMPORTS", valid_spring,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(f"  Created {len(valid_spring)} SPRING_IMPORTS relationships")

        # 7. Module CONTAINS Package, Package CONTAINS JavaClass
        self._create_containment_hierarchy(classes, packages, module_map)

        # 8. Entity nodes (dual-label)
        if entities:
            self._create_entity_nodes(entities)

        # 9. Method nodes + DECLARES / OVERRIDES / OVERLOADS
        self._write_method_graph(methods, known_fqns, valid_extends, valid_implements)

        # 10. Verification report
        self._report_coverage(classes, valid_extends, valid_implements)

    # ------------------------------------------------------------------
    # Method graph
    # ------------------------------------------------------------------

    def _write_method_graph(
        self,
        methods: list[dict],
        known_fqns: set[str],
        extends_rels: list[dict],
        implements_rels: list[dict],
    ):
        """
        Create Method nodes and the DECLARES / OVERRIDES / OVERLOADS edges.

        OVERRIDES is resolved by walking the transitive supertype closure built from
        the EXTENDS and IMPLEMENTS edges we just wrote. A method overrides a
        supertype method when the owner is a subtype and the erased signature
        (name + arity + erased parameter types) matches. Arity-only matching is used
        as a fallback because generic substitution (`T` vs `String`) makes exact
        erased-parameter comparison too strict in practice.
        """
        # Keep only methods whose owning type is in the graph
        methods = [m for m in methods if m["ownerFqn"] in known_fqns]
        if not methods:
            logger.info("  No methods to write")
            return

        # Deduplicate by method id (same signature can appear twice if a type is
        # declared in two source sets)
        seen: set[str] = set()
        unique: list[dict] = []
        for m in methods:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            unique.append(m)
        methods = unique

        node_rows = [
            {
                "id": m["id"],
                "name": m["name"],
                "ownerFqn": m["ownerFqn"],
                "signature": m["signature"],
                "arity": m["arity"],
                "returnType": m["returnType"],
                "isAbstract": m["isAbstract"],
                "isStatic": m["isStatic"],
                "isConstructor": m["isConstructor"],
                "visibility": m["visibility"],
                "hasOverrideAnnotation": m["hasOverrideAnnotation"],
                "path": m["path"],
                "lineNumber": m["lineNumber"],
            }
            for m in methods
        ]
        self.client.batch_create_nodes("Method", node_rows, merge_key="id")
        logger.info(f"  Created {len(node_rows):,} Method nodes")

        # ---- DECLARES: JavaClass → Method
        declares = [{"from_id": m["ownerFqn"], "to_id": m["id"]} for m in methods]
        self.client.batch_create_relationships(
            "DECLARES", declares,
            from_label="JavaClass", to_label="Method",
            from_key="fqn", to_key="id",
        )
        logger.info(f"  Created {len(declares):,} JavaClass-[:DECLARES]->Method edges")

        # ---- index methods by owner
        by_owner: dict[str, list[dict]] = defaultdict(list)
        for m in methods:
            by_owner[m["ownerFqn"]].append(m)

        # ---- OVERLOADS: same owner, same name, different signature
        # Emitted once per unordered pair to avoid duplicate reversed edges.
        overloads: list[dict] = []
        for owner, owned in by_owner.items():
            groups: dict[tuple, list[dict]] = defaultdict(list)
            for m in owned:
                groups[(m["name"], m["isConstructor"])].append(m)
            for (_name, _isctor), group in groups.items():
                if len(group) < 2:
                    continue
                ordered = sorted(group, key=lambda x: (x["arity"], x["signature"]))
                for i in range(len(ordered)):
                    for j in range(i + 1, len(ordered)):
                        overloads.append({
                            "from_id": ordered[i]["id"],
                            "to_id": ordered[j]["id"],
                            "ownerFqn": owner,
                        })

        if overloads:
            self.client.batch_create_relationships(
                "OVERLOADS", overloads,
                from_label="Method", to_label="Method",
                from_key="id", to_key="id",
            )
        logger.info(f"  Created {len(overloads):,} Method-[:OVERLOADS]->Method edges")

        # ---- OVERRIDES: walk the supertype closure
        direct_supertypes: dict[str, set[str]] = defaultdict(set)
        for r in extends_rels:
            direct_supertypes[r["from_id"]].add(r["to_id"])
        for r in implements_rels:
            direct_supertypes[r["from_id"]].add(r["to_id"])

        closure_cache: dict[str, list[str]] = {}

        def supertype_closure(fqn: str) -> list[str]:
            """Breadth-first transitive supertypes, nearest first."""
            cached = closure_cache.get(fqn)
            if cached is not None:
                return cached
            order: list[str] = []
            visited = {fqn}
            frontier = sorted(direct_supertypes.get(fqn, ()))
            while frontier:
                nxt: list[str] = []
                for st in frontier:
                    if st in visited:
                        continue
                    visited.add(st)
                    order.append(st)
                    nxt.extend(sorted(direct_supertypes.get(st, ())))
                frontier = nxt
            closure_cache[fqn] = order
            return order

        # Signature index per owner for fast lookup
        sig_index: dict[str, dict[tuple, dict]] = {}
        arity_index: dict[str, dict[tuple, list[dict]]] = {}
        for owner, owned in by_owner.items():
            sigs: dict[tuple, dict] = {}
            arities: dict[tuple, list[dict]] = defaultdict(list)
            for m in owned:
                if m["isConstructor"]:
                    continue  # constructors are never inherited/overridden
                sigs.setdefault((m["name"], m["signature"]), m)
                arities[(m["name"], m["arity"])].append(m)
            sig_index[owner] = sigs
            arity_index[owner] = arities

        overrides: list[dict] = []
        exact_matches = 0
        arity_matches = 0
        annotated_unresolved = 0

        for m in methods:
            if m["isConstructor"] or m["isStatic"]:
                continue
            target = None
            match_kind = ""
            for supertype in supertype_closure(m["ownerFqn"]):
                # 1. exact erased signature match
                cand = sig_index.get(supertype, {}).get((m["name"], m["signature"]))
                if cand is not None:
                    target, match_kind = cand, "signature"
                    break
                # 2. same name + same arity (covers generic substitution)
                bucket = arity_index.get(supertype, {}).get((m["name"], m["arity"]))
                if bucket:
                    target, match_kind = sorted(bucket, key=lambda x: x["id"])[0], "arity"
                    break
            if target is None:
                if m["hasOverrideAnnotation"]:
                    # @Override present but the supertype method isn't in the graph —
                    # almost always because the supertype is a JDK/library type.
                    annotated_unresolved += 1
                continue
            if target["id"] == m["id"]:
                continue
            overrides.append({
                "from_id": m["id"],
                "to_id": target["id"],
                "matchKind": match_kind,
                "annotated": m["hasOverrideAnnotation"],
            })
            if match_kind == "signature":
                exact_matches += 1
            else:
                arity_matches += 1

        if overrides:
            self.client.batch_create_relationships(
                "OVERRIDES", overrides,
                from_label="Method", to_label="Method",
                from_key="id", to_key="id",
            )
        logger.info(
            f"  Created {len(overrides):,} Method-[:OVERRIDES]->Method edges "
            f"({exact_matches:,} exact signature, {arity_matches:,} arity-matched)"
        )
        logger.info(
            f"  {annotated_unresolved:,} @Override methods target a supertype outside "
            f"this repo (JDK/library) — expected, not an error"
        )

    def _report_coverage(self, classes: list[dict], extends_rels: list[dict], implements_rels: list[dict]):
        """Report what the extractor found, so silent under-extraction is visible."""
        by_kind: dict[str, int] = defaultdict(int)
        for c in classes:
            by_kind[c["kind"]] += 1

        logger.info("  " + "-" * 58)
        logger.info("  Java type graph verification")
        logger.info("  " + "-" * 58)
        for kind in sorted(by_kind):
            logger.info(f"    {kind:12s}: {by_kind[kind]:>7,}")
        logger.info(f"    {'TOTAL':12s}: {len(classes):>7,}")
        logger.info(f"    EXTENDS edges (internal targets):    {len(extends_rels):>7,}")
        logger.info(f"    IMPLEMENTS edges (internal targets): {len(implements_rels):>7,}")

        if self._unresolved_supertypes:
            total = sum(self._unresolved_supertypes.values())
            logger.info(
                f"    Supertype names that could not be resolved to an FQN: {total:,} "
                f"({len(self._unresolved_supertypes):,} distinct)"
            )
            top = sorted(self._unresolved_supertypes.items(), key=lambda kv: kv[1], reverse=True)[:10]
            for name, cnt in top:
                logger.info(f"      {name}: {cnt:,}")
        logger.info("  " + "-" * 58)

    @staticmethod
    def _dedupe_classes(classes: list[dict]) -> list[dict]:
        """Collapse duplicate FQNs deterministically (prefer non-test, then shortest path)."""
        best: dict[str, dict] = {}
        for c in classes:
            existing = best.get(c["fqn"])
            if existing is None:
                best[c["fqn"]] = c
                continue
            key_new = (c["isTest"], len(c["path"]), c["path"])
            key_old = (existing["isTest"], len(existing["path"]), existing["path"])
            if key_new < key_old:
                best[c["fqn"]] = c
        return list(best.values())

    def _find_java_files(self) -> list[Path]:
        """Find all Java source files, including test code."""
        java_files = []
        for java_file in self.repo_root.rglob("*.java"):
            rel = str(java_file.relative_to(self.repo_root))

            # Skip build outputs and generated code only
            skip_patterns = [
                "/build/", "/generated/",
                "node_modules/", "/.gradle/", "/.idea/",
            ]
            if any(p in rel for p in skip_patterns):
                continue

            # Include both production AND test code
            if "/src/" in rel:
                java_files.append(java_file)

        return java_files

    def _build_module_map(self) -> dict[str, str]:
        """
        Build a mapping from directory path → module gradle path.
        Used to determine which module a file belongs to.
        """
        module_map = {}
        # Query existing modules from graph
        results = self.client.run_query(
            "MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath"
        )
        for row in results:
            module_map[row["path"]] = row["gradlePath"]
        return module_map

    def _file_to_module(self, file_path: Path, module_map: dict) -> str | None:
        """Find the module gradle path for a given file."""
        rel = str(file_path.relative_to(self.repo_root))
        # Walk up the path to find the module directory
        parts = rel.split("/")
        for i in range(len(parts), 0, -1):
            candidate = "/".join(parts[:i])
            if candidate in module_map:
                return module_map[candidate]
        return None

    def _parse_file(self, parser: Parser, file_path: Path, module_map: dict) -> dict | None:
        """Parse a single Java file and extract all types, relationships and methods."""
        try:
            content = file_path.read_bytes()
        except Exception:
            return None

        tree = parser.parse(content)
        root = tree.root_node

        if root.has_error:
            # Tree-sitter still produces a partial tree, we can continue
            pass

        rel_path = self.relative_path(file_path)
        module_gradle_path = self._file_to_module(file_path, module_map)

        # Extract package
        package_name = ""
        package_node = _find_child(root, "package_declaration")
        if package_node:
            for child in package_node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    package_name = _scoped_identifier_to_str(child)
                    break

        # Extract imports.
        #
        # A wildcard import (`import java.util.*;`) parses as an import_declaration
        # whose scoped_identifier is the PACKAGE ("java.util") plus a separate
        # `asterisk` child. The old code checked `fqn.endswith("*")`, which never
        # matched, so package names were injected into the single-type import list —
        # polluting it and never enabling wildcard resolution. Separate the two.
        import_fqns: list[str] = []
        wildcard_packages: list[str] = []
        static_imports: list[str] = []

        for import_node in _find_children(root, "import_declaration"):
            is_wildcard = _find_child(import_node, "asterisk") is not None
            is_static = any(c.type == "static" for c in import_node.children)
            path_str = ""
            for child in import_node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    path_str = _scoped_identifier_to_str(child)
                    break
            if not path_str:
                continue
            if is_wildcard:
                wildcard_packages.append(path_str)
            elif is_static:
                # `import static com.Foo.BAR;` — the type is the parent of the member.
                static_imports.append(path_str)
            else:
                import_fqns.append(path_str)

        result = {
            "classes": [],
            "packages": {},
            "imports": [],
            "extends": [],
            "implements": [],
            "spring_imports": [],
            "entities": [],
            "methods": [],
        }

        if package_name:
            result["packages"][package_name] = {
                "name": package_name,
                "path": self._derive_module_path_from_file(rel_path),
            }

        # Pass 1: collect every type declared in this compilation unit, at any depth,
        # so a supertype reference to a sibling or nested type resolves correctly.
        # e.g. `class A implements Handler {}` where `interface Handler` is declared
        # further down the same file, or `Outer.Inner` referenced from `Outer`.
        local_types: dict[str, str] = {}  # simple name -> fqn
        self._collect_local_types(root, package_name, None, local_types)

        ctx = {
            "package_name": package_name,
            "rel_path": rel_path,
            "import_fqns": import_fqns,
            "wildcard_packages": wildcard_packages,
            "static_imports": static_imports,
            "module_gradle_path": module_gradle_path,
            "local_types": local_types,
        }

        for decl in root.children:
            if decl.type in TYPE_DECL_TYPES:
                self._extract_type_declaration(decl, ctx, result, enclosing_fqn=None)

        return result

    def _collect_local_types(self, node, package_name: str, enclosing_fqn: str | None,
                            out: dict[str, str]):
        """
        Recursively record every type declared in this compilation unit.

        Populates simple name → FQN and (for nested types) `Outer.Inner` → FQN, so
        that scoped references within the file resolve without an import.
        """
        for child in node.children:
            if child.type not in TYPE_DECL_TYPES:
                # Descend through bodies to reach deeper nesting.
                if child.type in BODY_TYPES or child.type == "enum_body_declarations":
                    self._collect_local_types(child, package_name, enclosing_fqn, out)
                continue

            name_node = _find_child(child, "identifier")
            if not name_node:
                continue
            simple = _node_text(name_node)
            fqn = f"{enclosing_fqn}.{simple}" if enclosing_fqn else (
                f"{package_name}.{simple}" if package_name else simple
            )
            out.setdefault(simple, fqn)
            if enclosing_fqn:
                # Also register the dotted form relative to the outer type.
                short_outer = enclosing_fqn.rsplit(".", 1)[-1]
                out.setdefault(f"{short_outer}.{simple}", fqn)

            for body_type in BODY_TYPES:
                body = _find_child(child, body_type)
                if body is not None:
                    self._collect_local_types(body, package_name, fqn, out)
                    if body_type == "enum_body":
                        for ebd in _find_children(body, "enum_body_declarations"):
                            self._collect_local_types(ebd, package_name, fqn, out)

    def _extract_type_declaration(self, decl, ctx: dict, result: dict,
                                  enclosing_fqn: str | None = None):
        """
        Extract one type declaration (class / interface / enum / annotation / record),
        its supertype edges, its methods, and recurse into every nested type.
        """
        package_name = ctx["package_name"]
        rel_path = ctx["rel_path"]

        name_node = _find_child(decl, "identifier")
        if not name_node:
            return
        class_name = _node_text(name_node)

        if enclosing_fqn:
            fqn = f"{enclosing_fqn}.{class_name}"
        else:
            fqn = f"{package_name}.{class_name}" if package_name else class_name

        kind_map = {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "annotation_type_declaration": "annotation",
            "record_declaration": "record",
        }
        kind = kind_map.get(decl.type, "class")

        # ---- modifiers / annotations
        is_abstract = False
        is_final = False
        is_static = False
        is_spring_config = False
        annotations: list[str] = []
        modifiers_node = _find_child(decl, "modifiers")
        if modifiers_node:
            for mod_child in modifiers_node.children:
                t = mod_child.type
                if t == "abstract":
                    is_abstract = True
                elif t == "final":
                    is_final = True
                elif t == "static":
                    is_static = True
                elif t == "marker_annotation":
                    ann_name = _node_text(mod_child).lstrip("@").split(".")[-1]
                    annotations.append(ann_name)
                    if ann_name == "Configuration":
                        is_spring_config = True
                elif t == "annotation":
                    ann_text = _node_text(mod_child)
                    ann_name = ann_text.split("(")[0].lstrip("@").split(".")[-1]
                    annotations.append(ann_name)
                    if ann_name == "Configuration":
                        is_spring_config = True

        # Interfaces and annotation types are implicitly abstract.
        if kind in ("interface", "annotation"):
            is_abstract = True

        class_info = {
            "fqn": fqn,
            "name": class_name,
            "path": rel_path,
            "kind": kind,
            "isAbstract": is_abstract,
            "isFinal": is_final,
            "isNested": enclosing_fqn is not None,
            "isStatic": is_static,
            "enclosingFqn": enclosing_fqn or "",
            "isSpringConfig": is_spring_config,
            "isTest": self._is_test_file(rel_path),
            "package": package_name,
            "module": ctx["module_gradle_path"] or "",
            "lineNumber": decl.start_point[0] + 1,  # tree-sitter is 0-indexed
            "annotations": ",".join(annotations) if annotations else "",
        }
        result["classes"].append(class_info)

        # ---- IMPORTS: from import statements (resolved to FQN)
        for imp_fqn in ctx["import_fqns"]:
            result["imports"].append({
                "from_id": fqn,
                "to_id": imp_fqn,
                "sourceFile": rel_path,
            })

        def add_ref(bucket: str, raw_name: str):
            """
            Queue a supertype reference for resolution.

            Resolution is deferred to after every file has been parsed, because
            correctly resolving `Foo` requires knowing whether `<package>.Foo`
            actually exists — which is unknowable while still mid-scan. Resolving
            eagerly is what made the old extractor guess wrong and then silently
            discard the edge.
            """
            result[bucket].append({
                "from_id": fqn,
                "rawName": raw_name,
                "ctx": ctx,
                "enclosingFqn": enclosing_fqn,
                "selfFqn": fqn,
                "sourceFile": rel_path,
            })

        # ---- EXTENDS (a): class / enum / record superclass.
        #
        # `superclass` wraps ONE of type_identifier | generic_type |
        # scoped_type_identifier. The old code only read type_identifier, silently
        # dropping 2,471 generic superclasses (`extends GenericDaoHbImpl<X, Y>`) and
        # 61 scoped ones on the ae codebase.
        superclass_node = _find_child(decl, "superclass")
        if superclass_node:
            for child in superclass_node.children:
                if child.type in TYPE_REF_NODES:
                    sc_name = _type_ref_name(child)
                    if sc_name:
                        add_ref("extends", sc_name)
                    break

        # ---- EXTENDS (b): interface extends interface.
        #
        # For `interface A extends B, C` tree-sitter emits `extends_interfaces`,
        # NOT `superclass`. The old code never looked for this node, so ALL 2,786
        # interface-inheritance edges were missing from the graph.
        for iface_name in _type_list_refs(_find_child(decl, "extends_interfaces")):
            add_ref("extends", iface_name)

        # ---- IMPLEMENTS: class / enum / record implements interface
        for iface_name in _type_list_refs(_find_child(decl, "super_interfaces")):
            add_ref("implements", iface_name)

        # ---- SPRING_IMPORTS: @Import({X.class, Y.class}) on @Configuration classes
        if is_spring_config and modifiers_node:
            for mod_child in modifiers_node.children:
                if mod_child.type == "annotation":
                    ann_text = _node_text(mod_child)
                    if ann_text.startswith("@Import"):
                        class_refs = re.findall(r'([\w.]+)\.class', ann_text)
                        for ref_name in class_refs:
                            add_ref("spring_imports", ref_name)

        # ---- Entity detection: @Entity + @Table(name="...")
        if "Entity" in annotations:
            table_name = None
            if modifiers_node:
                for mod_child in modifiers_node.children:
                    if mod_child.type == "annotation":
                        ann_text = _node_text(mod_child)
                        if "@Table" in ann_text:
                            match = re.search(r'name\s*=\s*"([^"]+)"', ann_text)
                            if match:
                                table_name = match.group(1)
            if table_name:
                result["entities"].append({"fqn": fqn, "tableName": table_name})

        # ---- Methods + nested types.
        #
        # Iterate EVERY body kind. The old code only handled `class_body`, so methods
        # and nested types inside interfaces, enums, annotation types and records
        # were invisible.
        for body_type in BODY_TYPES:
            body = _find_child(decl, body_type)
            if body is None:
                continue

            containers = [body]
            if body_type == "enum_body":
                # `enum E { A, B; void m() {} }` — members live under enum_body_declarations
                containers.extend(_find_children(body, "enum_body_declarations"))

            for container in containers:
                for member in container.children:
                    if member.type == "method_declaration":
                        self._extract_method(member, fqn, kind, ctx, result, is_ctor=False)
                    elif member.type == "constructor_declaration":
                        self._extract_method(member, fqn, kind, ctx, result, is_ctor=True)
                    elif member.type == "compact_constructor_declaration":
                        self._extract_method(member, fqn, kind, ctx, result, is_ctor=True)
                    elif member.type in TYPE_DECL_TYPES:
                        self._extract_type_declaration(member, ctx, result, enclosing_fqn=fqn)

    def _extract_method(self, node, owner_fqn: str, owner_kind: str,
                        ctx: dict, result: dict, is_ctor: bool):
        """Extract a method or constructor declaration into a Method record."""
        name_node = _find_child(node, "identifier")
        if not name_node:
            return
        method_name = _node_text(name_node)

        formal_parameters = _find_child(node, "formal_parameters")
        param_types = _param_types(formal_parameters)
        signature = ",".join(param_types)

        # Return type: the child before the identifier that is a type node.
        return_type = "void" if is_ctor else "?"
        if not is_ctor:
            for child in node.children:
                if child is name_node:
                    break
                if child.type in ("modifiers", "type_parameters", "annotation",
                                  "marker_annotation"):
                    continue
                return_type = _erase_type(child)
                break

        is_abstract = False
        is_static = False
        is_default = False
        visibility = "package"
        has_override = False
        modifiers_node = _find_child(node, "modifiers")
        if modifiers_node:
            for mod_child in modifiers_node.children:
                t = mod_child.type
                if t == "abstract":
                    is_abstract = True
                elif t == "static":
                    is_static = True
                elif t == "default":
                    is_default = True
                elif t in ("public", "private", "protected"):
                    visibility = t
                elif t == "marker_annotation":
                    if _node_text(mod_child).lstrip("@").split(".")[-1] == "Override":
                        has_override = True
                elif t == "annotation":
                    if _node_text(mod_child).split("(")[0].lstrip("@").split(".")[-1] == "Override":
                        has_override = True

        # An interface method with no body and no `default`/`static` is abstract.
        if owner_kind in ("interface", "annotation") and not is_default and not is_static:
            if _find_child(node, "block") is None:
                is_abstract = True

        # Method identity must include the erased signature so overloads are distinct
        # nodes. Arity alone is not enough: `m(int)` and `m(String)` share arity 1.
        method_id = f"{owner_fqn}#{method_name}({signature})"

        result["methods"].append({
            "id": method_id,
            "name": method_name,
            "ownerFqn": owner_fqn,
            "signature": signature,
            "arity": len(param_types),
            "returnType": return_type,
            "isAbstract": is_abstract,
            "isStatic": is_static,
            "isConstructor": is_ctor,
            "visibility": visibility,
            "hasOverrideAnnotation": has_override,
            "path": ctx["rel_path"],
            "lineNumber": node.start_point[0] + 1,
        })

    def _derive_module_path_from_file(self, rel_path: str) -> str:
        """Derive the module directory path from a file path."""
        # Handle: src/main/java/, src/test/unit/java/, src/test/integration/java/, etc.
        for marker in ("/src/main/java/", "/src/test/", "/src/"):
            if marker in rel_path:
                return rel_path.split(marker)[0]
        return rel_path

    def _is_test_file(self, rel_path: str) -> bool:
        """Determine if a file is test code based on its path."""
        return "/src/test/" in rel_path or "/test/" in rel_path

    def _resolve_pending(self, pending: list[dict]) -> list[dict]:
        """
        Turn queued raw supertype references into concrete from_id/to_id edges.

        Runs after every file has been parsed so that `_is_known()` can consult the
        complete set of declared FQNs.
        """
        resolved: list[dict] = []
        for ref in pending:
            to_fqn = self._resolve_type_to_fqn(
                ref["rawName"], ref["ctx"], ref["enclosingFqn"], ref["selfFqn"]
            )
            if to_fqn:
                resolved.append({
                    "from_id": ref["from_id"],
                    "to_id": to_fqn,
                    "sourceFile": ref["sourceFile"],
                })
        return resolved

    def _resolve_type_to_fqn(self, type_name: str, ctx: dict,
                             enclosing_fqn: str | None, self_fqn: str) -> str | None:
        """
        Resolve a type name appearing in an extends/implements position to an FQN.

        The previous implementation checked explicit imports, then unconditionally
        returned `<package>.<SimpleName>`. That guess is wrong for anything coming
        from a wildcard import, java.lang, or a nested type, and because the caller
        then filters out FQNs with no matching node, those edges vanished silently
        with nothing in the log.

        Resolution follows Java's actual precedence:
          1. Already an FQN we know about
          2. Types declared in this same compilation unit (siblings + nested)
          3. Nested type of the enclosing type, or of `self`
          4. Explicit single-type import  (`import a.b.Foo;`)
          5. Scoped reference rooted at an import (`Outer.Inner` where Outer imported)
          6. Static import owner
          7. Same package
          8. Wildcard import packages (`import a.b.*;`)
        java.lang types are rejected outright rather than mis-attributed.

        Returns None when unresolvable, and records the name for the coverage report.
        """
        if not type_name:
            return None

        package_name = ctx["package_name"]
        local_types = ctx["local_types"]
        import_fqns = ctx["import_fqns"]

        head = type_name.split(".", 1)[0]
        simple = type_name.rsplit(".", 1)[-1]

        # A single-segment java.lang name can never be a repo class.
        if "." not in type_name and type_name in JAVA_LANG_COMMON:
            return None

        # Generic type variables (T, E, K, V, R, U, T1...) are not types.
        if "." not in type_name and self._looks_like_type_variable(type_name):
            return None

        # 1. Fully-qualified already
        if "." in type_name and self._is_known(type_name):
            return type_name

        # 2. Declared in this compilation unit (covers sibling + nested types)
        if type_name in local_types:
            return local_types[type_name]
        if head in local_types and "." in type_name:
            candidate = local_types[head] + type_name[len(head):]
            if self._is_known(candidate) or True:
                return candidate

        # 3. Nested inside the enclosing type or inside self
        for owner in (enclosing_fqn, self_fqn):
            if owner:
                candidate = f"{owner}.{type_name}"
                if self._is_known(candidate):
                    return candidate

        # 4. Explicit single-type import
        for imp in import_fqns:
            if imp.endswith(f".{simple}") or imp == simple:
                if "." in type_name and not imp.endswith(f".{type_name}"):
                    continue
                return imp

        # 5. Scoped reference whose root was imported: `Outer.Inner`
        if "." in type_name:
            for imp in import_fqns:
                if imp.endswith(f".{head}"):
                    return f"{imp}{type_name[len(head):]}"

        # 6. Static import owner (`import static a.b.Foo.BAR;` makes Foo visible)
        for imp in ctx["static_imports"]:
            parts = imp.split(".")
            if len(parts) >= 2 and parts[-2] == simple:
                return ".".join(parts[:-1])

        # 7. Same package
        if package_name:
            candidate = f"{package_name}.{type_name}"
            if self._is_known(candidate):
                return candidate

        # 8. Wildcard imports — only accept if the resulting FQN is a real node,
        #    otherwise we would invent edges.
        for wildcard_pkg in ctx["wildcard_packages"]:
            candidate = f"{wildcard_pkg}.{type_name}"
            if self._is_known(candidate):
                return candidate

        # Last resort: assume same package. This preserves the previous behaviour for
        # the common case where the target class exists but has not been indexed yet
        # (single-pass extraction means _known_fqns is incomplete on the first file).
        if package_name and "." not in type_name:
            return f"{package_name}.{type_name}"

        self._unresolved_supertypes[type_name] += 1
        return None

    @staticmethod
    def _looks_like_type_variable(name: str) -> bool:
        """
        Heuristic for generic type variables (`T`, `K`, `V`, `R`, `T1`, `SELF`).

        Java convention is a single uppercase letter, optionally followed by digits.
        Real class names are longer or mixed-case, so this is safe in practice.
        """
        if len(name) == 1 and name.isupper():
            return True
        if len(name) <= 3 and name[0].isupper() and name[1:].isdigit():
            return True
        return False

    def _is_known(self, fqn: str) -> bool:
        """Whether an FQN corresponds to a type declared in this repo."""
        return fqn in self._all_declared_fqns

    def _filter_valid_relationships(self, rels: list[dict], known_fqns: set[str]) -> list[dict]:
        """
        Keep only relationships whose endpoints both exist as JavaClass nodes.

        Duplicates are collapsed here because MERGE with differing properties would
        otherwise create multiple parallel edges for the same logical relationship.
        """
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for r in rels:
            if r["from_id"] not in known_fqns or r["to_id"] not in known_fqns:
                continue
            if r["from_id"] == r["to_id"]:
                continue  # a type cannot extend/implement itself
            key = (r["from_id"], r["to_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def _create_containment_hierarchy(self, classes: list[dict], packages: dict, module_map: dict):
        """Create Module→Package, Package→SubPackage, and Package→JavaClass CONTAINS edges."""
        # Package → JavaClass
        pkg_class_rels = []
        for cls in classes:
            if cls["package"]:
                pkg_class_rels.append({
                    "from_id": cls["package"],
                    "to_id": cls["fqn"],
                })

        if pkg_class_rels:
            self.client.batch_create_relationships(
                "CONTAINS", pkg_class_rels,
                from_label="Package", to_label="JavaClass",
                from_key="name", to_key="fqn",
            )
            logger.info(f"  Created {len(pkg_class_rels)} Package→JavaClass CONTAINS edges")

        # Package → SubPackage (hierarchical nesting)
        # e.g., com.appiancorp → com.appiancorp.record → com.appiancorp.record.domain
        all_package_names = set(packages.keys())
        pkg_pkg_rels = []
        for pkg_name in all_package_names:
            # Find parent package: com.appiancorp.record.domain → parent is com.appiancorp.record
            parts = pkg_name.rsplit(".", 1)
            if len(parts) == 2:
                parent_pkg = parts[0]
                if parent_pkg in all_package_names:
                    pkg_pkg_rels.append({
                        "from_id": parent_pkg,
                        "to_id": pkg_name,
                    })

        if pkg_pkg_rels:
            self.client.batch_create_relationships(
                "CONTAINS", pkg_pkg_rels,
                from_label="Package", to_label="Package",
                from_key="name", to_key="name",
            )
            logger.info(f"  Created {len(pkg_pkg_rels)} Package→SubPackage CONTAINS edges")

        # Module → Package (deduplicated: one module can contain many packages)
        module_packages = defaultdict(set)
        for cls in classes:
            if cls["module"] and cls["package"]:
                module_packages[cls["module"]].add(cls["package"])

        mod_pkg_rels = []
        for module_path, pkg_names in module_packages.items():
            for pkg_name in pkg_names:
                mod_pkg_rels.append({
                    "from_id": module_path,
                    "to_id": pkg_name,
                })

        if mod_pkg_rels:
            self.client.batch_create_relationships(
                "CONTAINS", mod_pkg_rels,
                from_label="Module", to_label="Package",
                from_key="gradlePath", to_key="name",
            )
            logger.info(f"  Created {len(mod_pkg_rels)} Module→Package CONTAINS edges")

    def _create_entity_nodes(self, entities: list[dict]):
        """Create Entity nodes (these are JavaClass nodes with additional Entity label)."""
        # Add Entity label to existing JavaClass nodes
        for entity in entities:
            self.client.run_query(
                "MATCH (c:JavaClass {fqn: $fqn}) SET c:Entity, c.tableName = $tableName",
                fqn=entity["fqn"],
                tableName=entity["tableName"],
            )
        logger.info(f"  Labeled {len(entities)} classes as :Entity with tableName")
