"""
Java AST Extractor — Uses tree-sitter-java to extract class-level structural relationships.

Extracts:
- JavaClass nodes (class, interface, enum, annotation declarations)
- Package nodes
- IMPORTS relationships (import statements)
- EXTENDS relationships (superclass)
- IMPLEMENTS relationships (interfaces)
- SPRING_IMPORTS relationships (@Import annotation on @Configuration classes)
- Entity detection (@Entity + @Table(name=...))
- CONTAINS hierarchy (Module → Package → JavaClass)

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


class JavaExtractor(BaseExtractor):
    """Extracts Java class-level structural relationships using tree-sitter AST parsing."""

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

        # Module mapping: file path → module gradle path
        module_map = self._build_module_map()

        # Parse each file
        parsed = 0
        failed = 0
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
                    parsed += 1
            except Exception as e:
                failed += 1
                if failed <= 10:
                    logger.debug(f"  Failed to parse {java_file}: {e}")

        logger.info(f"  Parsed {parsed} files ({failed} failures)")

        # Write to Neo4j
        # 1. Package nodes
        pkg_nodes = [{"name": name, "path": info["path"]} for name, info in packages.items()]
        self.client.batch_create_nodes("Package", pkg_nodes, merge_key="name")
        logger.info(f"  Created {len(pkg_nodes)} Package nodes")

        # 2. JavaClass nodes
        self.client.batch_create_nodes("JavaClass", classes, merge_key="fqn")
        logger.info(f"  Created {len(classes)} JavaClass nodes")

        # 3. IMPORTS relationships
        valid_imports = self._filter_valid_relationships(imports, classes)
        self.client.batch_create_relationships(
            "IMPORTS", valid_imports,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(f"  Created {len(valid_imports)} IMPORTS relationships (from {len(imports)} candidates)")

        # 4. EXTENDS relationships
        valid_extends = self._filter_valid_relationships(extends, classes)
        self.client.batch_create_relationships(
            "EXTENDS", valid_extends,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(f"  Created {len(valid_extends)} EXTENDS relationships")

        # 5. IMPLEMENTS relationships
        valid_implements = self._filter_valid_relationships(implements, classes)
        self.client.batch_create_relationships(
            "IMPLEMENTS", valid_implements,
            from_label="JavaClass", to_label="JavaClass",
            from_key="fqn", to_key="fqn",
        )
        logger.info(f"  Created {len(valid_implements)} IMPLEMENTS relationships")

        # 6. SPRING_IMPORTS relationships
        valid_spring = self._filter_valid_relationships(spring_imports, classes)
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
        """Parse a single Java file and extract all relationships."""
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
                if child.type == "scoped_identifier":
                    package_name = _scoped_identifier_to_str(child)
                    break

        # Extract imports
        import_fqns = []
        for import_node in _find_children(root, "import_declaration"):
            for child in import_node.children:
                if child.type == "scoped_identifier":
                    fqn = _scoped_identifier_to_str(child)
                    if fqn and not fqn.endswith("*"):
                        import_fqns.append(fqn)
                    break

        # Extract class/interface/enum declarations
        result = {
            "classes": [],
            "packages": {},
            "imports": [],
            "extends": [],
            "implements": [],
            "spring_imports": [],
            "entities": [],
        }

        if package_name:
            result["packages"][package_name] = {
                "name": package_name,
                "path": self._derive_module_path_from_file(rel_path),
            }

        # Find all type declarations (top-level only for now)
        type_decl_types = ("class_declaration", "interface_declaration",
                          "enum_declaration", "annotation_type_declaration")

        for decl in root.children:
            if decl.type in type_decl_types:
                self._extract_type_declaration(
                    decl, package_name, rel_path, import_fqns,
                    module_gradle_path, result, enclosing_fqn=None
                )

        return result

    def _extract_type_declaration(
        self, decl, package_name: str, rel_path: str,
        import_fqns: list[str], module_gradle_path: str | None, result: dict,
        enclosing_fqn: str | None = None
    ):
        """Extract a single class/interface/enum declaration, including inner classes."""
        # Get class name
        name_node = _find_child(decl, "identifier")
        if not name_node:
            return
        class_name = _node_text(name_node)

        # FQN: if nested, use OuterClass$InnerClass format
        if enclosing_fqn:
            fqn = f"{enclosing_fqn}.{class_name}"
        else:
            fqn = f"{package_name}.{class_name}" if package_name else class_name

        # Determine kind
        kind_map = {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "annotation_type_declaration": "annotation",
        }
        kind = kind_map.get(decl.type, "class")

        # Check modifiers for annotations
        is_abstract = False
        is_spring_config = False
        annotations = []
        modifiers_node = _find_child(decl, "modifiers")
        if modifiers_node:
            for mod_child in modifiers_node.children:
                if mod_child.type == "abstract":
                    is_abstract = True
                elif mod_child.type == "marker_annotation":
                    ann_name = _node_text(mod_child).lstrip("@")
                    annotations.append(ann_name)
                    if ann_name == "Configuration":
                        is_spring_config = True
                elif mod_child.type == "annotation":
                    ann_text = _node_text(mod_child)
                    ann_name = ann_text.split("(")[0].lstrip("@")
                    annotations.append(ann_name)
                    if ann_name == "Configuration":
                        is_spring_config = True

        # Create class node
        class_info = {
            "fqn": fqn,
            "name": class_name,
            "path": rel_path,
            "kind": kind,
            "isAbstract": is_abstract,
            "isSpringConfig": is_spring_config,
            "isTest": self._is_test_file(rel_path),
            "package": package_name,
            "module": module_gradle_path or "",
            "lineNumber": decl.start_point[0] + 1,  # tree-sitter is 0-indexed
            "annotations": ",".join(annotations) if annotations else "",
        }
        result["classes"].append(class_info)

        # IMPORTS: from import statements (resolved to FQN)
        for imp_fqn in import_fqns:
            result["imports"].append({
                "from_id": fqn,
                "to_id": imp_fqn,
                "sourceFile": rel_path,
            })

        # EXTENDS: superclass
        superclass_node = _find_child(decl, "superclass")
        if superclass_node:
            sc_type = _find_child(superclass_node, "type_identifier")
            if sc_type:
                sc_name = _node_text(sc_type)
                sc_fqn = self._resolve_type_to_fqn(sc_name, import_fqns, package_name)
                if sc_fqn:
                    result["extends"].append({
                        "from_id": fqn,
                        "to_id": sc_fqn,
                        "sourceFile": rel_path,
                    })

        # IMPLEMENTS: interfaces
        interfaces_node = _find_child(decl, "super_interfaces")
        if interfaces_node:
            type_list = _find_child(interfaces_node, "type_list")
            if type_list:
                for child in type_list.children:
                    if child.type == "type_identifier":
                        iface_name = _node_text(child)
                        iface_fqn = self._resolve_type_to_fqn(iface_name, import_fqns, package_name)
                        if iface_fqn:
                            result["implements"].append({
                                "from_id": fqn,
                                "to_id": iface_fqn,
                                "sourceFile": rel_path,
                            })
                    elif child.type == "generic_type":
                        # Handle cases like `implements List<X>`
                        type_id = _find_child(child, "type_identifier")
                        if type_id:
                            iface_name = _node_text(type_id)
                            iface_fqn = self._resolve_type_to_fqn(iface_name, import_fqns, package_name)
                            if iface_fqn:
                                result["implements"].append({
                                    "from_id": fqn,
                                    "to_id": iface_fqn,
                                    "sourceFile": rel_path,
                                })

        # SPRING_IMPORTS: @Import({X.class, Y.class}) on @Configuration classes
        if is_spring_config and modifiers_node:
            for mod_child in modifiers_node.children:
                if mod_child.type == "annotation":
                    ann_text = _node_text(mod_child)
                    if ann_text.startswith("@Import"):
                        # Extract class references from @Import
                        class_refs = re.findall(r'(\w+)\.class', ann_text)
                        for ref_name in class_refs:
                            ref_fqn = self._resolve_type_to_fqn(ref_name, import_fqns, package_name)
                            if ref_fqn:
                                result["spring_imports"].append({
                                    "from_id": fqn,
                                    "to_id": ref_fqn,
                                    "sourceFile": rel_path,
                                })

        # Entity detection: @Entity + @Table(name="...")
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
                result["entities"].append({
                    "fqn": fqn,
                    "tableName": table_name,
                })

        # Recurse into inner/nested class declarations
        body = _find_child(decl, "class_body")
        if body:
            type_decl_types = ("class_declaration", "interface_declaration",
                              "enum_declaration", "annotation_type_declaration")
            for member in body.children:
                if member.type in type_decl_types:
                    self._extract_type_declaration(
                        member, package_name, rel_path, import_fqns,
                        module_gradle_path, result, enclosing_fqn=fqn
                    )

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

    def _resolve_type_to_fqn(self, simple_name: str, import_fqns: list[str], package_name: str) -> str | None:
        """
        Resolve a simple type name to its FQN using import list.
        Returns None if unresolvable (external dependency).
        """
        # Check imports for explicit match
        for imp in import_fqns:
            if imp.endswith(f".{simple_name}"):
                return imp

        # Same package (might be there)
        if package_name:
            return f"{package_name}.{simple_name}"

        return None

    def _filter_valid_relationships(self, rels: list[dict], classes: list[dict]) -> list[dict]:
        """Filter relationships to only include those where both endpoints exist as nodes."""
        known_fqns = {c["fqn"] for c in classes}
        return [r for r in rels if r["from_id"] in known_fqns and r["to_id"] in known_fqns]

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
