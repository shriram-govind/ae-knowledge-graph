"""
Spring DI Extractor — Extracts Spring IoC dependency injection relationships.

Runs AFTER the Java AST extractor (needs JavaClass nodes to exist).
Extracts:
- INJECTS relationships (@Autowired fields, constructor injection, @Inject)
- PRODUCES relationships (@Bean methods → return type)
- @Primary, @Qualifier metadata on injection points
- @ConditionalOn* annotations on beans

Post-hoc resolution:
- For each injection point (interface type), finds ALL implementations in the graph
- Marks which implementation is @Primary
- Records @Qualifier values for targeted injection
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

JAVA_LANGUAGE = Language(tsjava.language())


def _get_parser() -> Parser:
    return Parser(JAVA_LANGUAGE)


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _find_child(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_children(node, type_name: str) -> list:
    return [c for c in node.children if c.type == type_name]


class SpringDIExtractor(BaseExtractor):
    """Extracts Spring IoC dependency injection relationships."""

    @property
    def name(self) -> str:
        return "Spring DI Extractor"

    def extract(self):
        parser = _get_parser()

        # Load known classes and their FQNs from the graph
        self._known_fqns = self._load_known_fqns()
        self._import_cache: dict[str, list[str]] = {}

        # Find Spring-relevant Java files
        java_files = self._find_spring_files()
        logger.info(f"  Found {len(java_files)} Spring-annotated Java files to analyze")

        # Data accumulators
        injection_points: list[dict] = []  # @Autowired fields + constructor params
        bean_declarations: list[dict] = []  # @Bean methods
        primary_classes: set[str] = set()  # Classes marked @Primary
        qualifier_map: dict[str, str] = {}  # FQN → qualifier value

        # Parse each file
        for java_file in java_files:
            try:
                result = self._parse_spring_file(parser, java_file)
                if result:
                    injection_points.extend(result["injections"])
                    bean_declarations.extend(result["beans"])
                    primary_classes.update(result["primary_classes"])
                    qualifier_map.update(result["qualifiers"])
            except Exception as e:
                logger.debug(f"  Failed to parse {java_file}: {e}")

        logger.info(f"  Found {len(injection_points)} injection points")
        logger.info(f"  Found {len(bean_declarations)} @Bean declarations")
        logger.info(f"  Found {len(primary_classes)} @Primary classes")
        logger.info(f"  Found {len(qualifier_map)} @Qualifier annotations")

        # Resolve injection points → create INJECTS edges
        self._resolve_injections(injection_points, primary_classes, qualifier_map)

        # Create PRODUCES edges from @Bean methods
        self._create_bean_edges(bean_declarations, primary_classes)

    def _load_known_fqns(self) -> set[str]:
        """Load all known JavaClass FQNs from the graph."""
        results = self.client.run_query("MATCH (c:JavaClass) RETURN c.fqn AS fqn")
        return {r["fqn"] for r in results}

    def _find_spring_files(self) -> list[Path]:
        """Find Java files that contain Spring DI annotations."""
        spring_files = []
        for java_file in self.repo_root.rglob("*.java"):
            rel = str(java_file.relative_to(self.repo_root))
            if "/build/" in rel or "node_modules/" in rel or "/.gradle/" in rel:
                continue
            if "/src/" not in rel:
                continue

            # Quick content check — only parse files with Spring annotations
            try:
                content = java_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            if any(marker in content for marker in (
                "@Autowired", "@Inject", "@Bean", "@Component", "@Service",
                "@Repository", "@Configuration", "@Primary", "@Qualifier",
            )):
                spring_files.append(java_file)

        return spring_files

    def _parse_spring_file(self, parser: Parser, file_path: Path) -> dict | None:
        """Parse a single Java file for Spring DI metadata."""
        try:
            content = file_path.read_bytes()
        except Exception:
            return None

        tree = parser.parse(content)
        root = tree.root_node
        rel_path = self.relative_path(file_path)

        # Extract package and imports
        package_name = ""
        import_fqns = []

        for child in root.children:
            if child.type == "package_declaration":
                for c in child.children:
                    if c.type == "scoped_identifier":
                        package_name = self._scoped_to_str(c)
            elif child.type == "import_declaration":
                for c in child.children:
                    if c.type == "scoped_identifier":
                        fqn = self._scoped_to_str(c)
                        if fqn and not fqn.endswith("*"):
                            import_fqns.append(fqn)

        result = {
            "injections": [],
            "beans": [],
            "primary_classes": set(),
            "qualifiers": {},
        }

        # Process class declarations
        for decl in root.children:
            if decl.type in ("class_declaration", "interface_declaration"):
                self._process_class(decl, package_name, import_fqns, rel_path, result)

        return result

    def _process_class(self, decl, package_name: str, import_fqns: list, rel_path: str, result: dict):
        """Process a class declaration for Spring DI annotations."""
        name_node = _find_child(decl, "identifier")
        if not name_node:
            return
        class_name = _node_text(name_node)
        class_fqn = f"{package_name}.{class_name}" if package_name else class_name

        # Check class-level annotations
        modifiers = _find_child(decl, "modifiers")
        class_annotations = self._extract_annotations(modifiers) if modifiers else {}

        if "Primary" in class_annotations:
            result["primary_classes"].add(class_fqn)

        # Check if this is a Spring-managed class
        is_spring_managed = any(a in class_annotations for a in (
            "Configuration", "Component", "Service", "Repository", "Controller",
        ))

        # Process class body
        body = _find_child(decl, "class_body")
        if not body:
            return

        for member in body.children:
            if member.type == "field_declaration":
                injection = self._extract_field_injection(
                    member, class_fqn, import_fqns, package_name, rel_path
                )
                if injection:
                    result["injections"].append(injection)

            elif member.type == "constructor_declaration":
                # Constructor injection (modern Spring: all params are injected in @Component classes)
                if is_spring_managed:
                    injections = self._extract_constructor_injection(
                        member, class_fqn, import_fqns, package_name, rel_path
                    )
                    result["injections"].extend(injections)

            elif member.type == "method_declaration":
                bean = self._extract_bean_method(
                    member, class_fqn, import_fqns, package_name, rel_path
                )
                if bean:
                    result["beans"].append(bean)

    def _extract_field_injection(self, field_decl, class_fqn: str, imports: list, package: str, path: str) -> dict | None:
        """Extract @Autowired/@Inject field injection."""
        modifiers = _find_child(field_decl, "modifiers")
        if not modifiers:
            return None

        annotations = self._extract_annotations(modifiers)

        if "Autowired" not in annotations and "Inject" not in annotations:
            return None

        # Get field type
        type_node = _find_child(field_decl, "type_identifier")
        if not type_node:
            # Try generic_type (e.g., List<X>)
            type_node = _find_child(field_decl, "generic_type")
            if type_node:
                type_node = _find_child(type_node, "type_identifier")
        if not type_node:
            return None

        field_type = _node_text(type_node)
        field_type_fqn = self._resolve_type(field_type, imports, package)

        # Get field name
        var_decl = _find_child(field_decl, "variable_declarator")
        field_name = _node_text(_find_child(var_decl, "identifier")) if var_decl else ""

        # Check for @Qualifier
        qualifier = annotations.get("Qualifier")

        return {
            "source_fqn": class_fqn,
            "target_type_fqn": field_type_fqn,
            "target_type_simple": field_type,
            "field_name": field_name,
            "injection_type": "field",
            "qualifier": qualifier,
            "source_file": path,
        }

    def _extract_constructor_injection(self, constructor, class_fqn: str, imports: list, package: str, path: str) -> list[dict]:
        """Extract constructor parameter injection."""
        injections = []
        params_node = _find_child(constructor, "formal_parameters")
        if not params_node:
            return injections

        for param in _find_children(params_node, "formal_parameter"):
            # Get parameter type
            type_node = _find_child(param, "type_identifier")
            if not type_node:
                type_node = _find_child(param, "generic_type")
                if type_node:
                    type_node = _find_child(type_node, "type_identifier")
            if not type_node:
                continue

            param_type = _node_text(type_node)
            param_type_fqn = self._resolve_type(param_type, imports, package)

            # Get parameter name
            name_node = _find_child(param, "identifier")
            param_name = _node_text(name_node) if name_node else ""

            # Check for @Qualifier on parameter
            param_modifiers = _find_child(param, "modifiers")
            qualifier = None
            if param_modifiers:
                param_annotations = self._extract_annotations(param_modifiers)
                qualifier = param_annotations.get("Qualifier")

            injections.append({
                "source_fqn": class_fqn,
                "target_type_fqn": param_type_fqn,
                "target_type_simple": param_type,
                "field_name": param_name,
                "injection_type": "constructor",
                "qualifier": qualifier,
                "source_file": path,
            })

        return injections

    def _extract_bean_method(self, method, class_fqn: str, imports: list, package: str, path: str) -> dict | None:
        """Extract @Bean method declaration. Parameters are also injection points."""
        modifiers = _find_child(method, "modifiers")
        if not modifiers:
            return None

        annotations = self._extract_annotations(modifiers)
        if "Bean" not in annotations:
            return None

        # Get return type
        return_type_node = _find_child(method, "type_identifier")
        if not return_type_node:
            return_type_node = _find_child(method, "generic_type")
            if return_type_node:
                return_type_node = _find_child(return_type_node, "type_identifier")
        if not return_type_node:
            return None

        return_type = _node_text(return_type_node)
        return_type_fqn = self._resolve_type(return_type, imports, package)

        # Get method name (used as bean name by default)
        method_name_node = _find_child(method, "identifier")
        method_name = _node_text(method_name_node) if method_name_node else ""

        # Check for @Primary on method
        is_primary = "Primary" in annotations

        # Check for @Qualifier on method
        qualifier = annotations.get("Qualifier")

        # Extract method parameters — these ARE injection points
        # Spring injects beans matching the parameter types when calling @Bean methods
        param_types = []
        params_node = _find_child(method, "formal_parameters")
        if params_node:
            for param in _find_children(params_node, "formal_parameter"):
                ptype_node = _find_child(param, "type_identifier")
                if not ptype_node:
                    ptype_node = _find_child(param, "generic_type")
                    if ptype_node:
                        ptype_node = _find_child(ptype_node, "type_identifier")
                if ptype_node:
                    ptype = _node_text(ptype_node)
                    ptype_fqn = self._resolve_type(ptype, imports, package)

                    # Check for @Qualifier on parameter
                    param_modifiers = _find_child(param, "modifiers")
                    param_qualifier = None
                    if param_modifiers:
                        param_annotations = self._extract_annotations(param_modifiers)
                        param_qualifier = param_annotations.get("Qualifier")

                    param_types.append({
                        "fqn": ptype_fqn,
                        "simple": ptype,
                        "qualifier": param_qualifier,
                    })

        return {
            "config_fqn": class_fqn,
            "return_type_fqn": return_type_fqn,
            "return_type_simple": return_type,
            "method_name": method_name,
            "is_primary": is_primary,
            "qualifier": qualifier,
            "param_types": param_types,
            "source_file": path,
        }

    def _extract_annotations(self, modifiers_node) -> dict[str, str | None]:
        """
        Extract annotations from a modifiers node.
        Returns: {annotation_name → value_or_None}
        """
        annotations = {}
        for child in modifiers_node.children:
            if child.type == "marker_annotation":
                name = _node_text(child).lstrip("@")
                annotations[name] = None
            elif child.type == "annotation":
                text = _node_text(child)
                name = text.split("(")[0].lstrip("@")
                # Extract string value if present
                value_match = re.search(r'"([^"]*)"', text)
                annotations[name] = value_match.group(1) if value_match else None
        return annotations

    def _resolve_type(self, simple_name: str, imports: list, package: str) -> str:
        """Resolve simple type name to FQN."""
        # Check imports
        for imp in imports:
            if imp.endswith(f".{simple_name}"):
                return imp
        # Same package
        if package:
            return f"{package}.{simple_name}"
        return simple_name

    def _scoped_to_str(self, node) -> str:
        """Convert scoped_identifier to dotted string."""
        if node.type == "identifier":
            return _node_text(node)
        if node.type == "scoped_identifier":
            parts = []
            for child in node.children:
                if child.type in ("identifier", "scoped_identifier"):
                    parts.append(self._scoped_to_str(child))
            return ".".join(parts)
        return _node_text(node)

    def _resolve_injections(self, injection_points: list[dict], primary_classes: set, qualifier_map: dict):
        """
        For each injection point, find implementations and create INJECTS edges.
        Links to ALL implementations with metadata about which is primary/qualified.
        """
        # Build the implements graph from Neo4j
        impl_map = defaultdict(list)  # interface_fqn → [impl_fqn, ...]
        results = self.client.run_query("""
            MATCH (impl:JavaClass)-[:IMPLEMENTS]->(iface:JavaClass)
            RETURN iface.fqn AS iface, impl.fqn AS impl
        """)
        for row in results:
            impl_map[row["iface"]].append(row["impl"])

        # Also include extends relationships (abstract class injection)
        results = self.client.run_query("""
            MATCH (child:JavaClass)-[:EXTENDS]->(parent:JavaClass)
            WHERE parent.isAbstract = true
            RETURN parent.fqn AS parent, child.fqn AS child
        """)
        for row in results:
            impl_map[row["parent"]].append(row["child"])

        # Create INJECTS edges
        injects_edges = []
        for injection in injection_points:
            source_fqn = injection["source_fqn"]
            target_type_fqn = injection["target_type_fqn"]

            if source_fqn not in self._known_fqns:
                continue

            # If the target type is a concrete class (exists directly), link to it
            if target_type_fqn in self._known_fqns:
                # Check if it's an interface/abstract with implementations
                implementations = impl_map.get(target_type_fqn, [])

                if implementations:
                    # Interface/abstract: link to ALL implementations
                    for impl_fqn in implementations:
                        is_primary = impl_fqn in primary_classes
                        injects_edges.append({
                            "from_id": source_fqn,
                            "to_id": impl_fqn,
                            "fieldName": injection["field_name"],
                            "injectionType": injection["injection_type"],
                            "qualifier": injection.get("qualifier") or "",
                            "isPrimary": is_primary,
                            "interfaceType": target_type_fqn,
                            "sourceFile": injection["source_file"],
                        })
                else:
                    # Concrete class: direct injection
                    injects_edges.append({
                        "from_id": source_fqn,
                        "to_id": target_type_fqn,
                        "fieldName": injection["field_name"],
                        "injectionType": injection["injection_type"],
                        "qualifier": injection.get("qualifier") or "",
                        "isPrimary": False,
                        "interfaceType": "",
                        "sourceFile": injection["source_file"],
                    })

        # Deduplicate (same source→target can appear from multiple injection points)
        seen = set()
        unique_edges = []
        for edge in injects_edges:
            key = (edge["from_id"], edge["to_id"], edge["fieldName"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(edge)

        if unique_edges:
            self.client.batch_create_relationships(
                "INJECTS", unique_edges,
                from_label="JavaClass", to_label="JavaClass",
                from_key="fqn", to_key="fqn",
            )
        logger.info(f"  Created {len(unique_edges)} INJECTS edges (from {len(injection_points)} injection points)")

    def _create_bean_edges(self, bean_declarations: list[dict], primary_classes: set):
        """
        Create PRODUCES edges from @Bean methods to their return types.
        Also create INJECTS edges from the @Bean method's config class to its parameter types
        (Spring injects beans into @Bean method parameters).
        """
        produces_edges = []
        bean_param_injections = []

        for bean in bean_declarations:
            config_fqn = bean["config_fqn"]
            return_type_fqn = bean["return_type_fqn"]

            if config_fqn not in self._known_fqns:
                continue

            # PRODUCES edge: config class → return type
            if return_type_fqn in self._known_fqns:
                produces_edges.append({
                    "from_id": config_fqn,
                    "to_id": return_type_fqn,
                    "methodName": bean["method_name"],
                    "isPrimary": bean["is_primary"],
                    "qualifier": bean.get("qualifier") or "",
                    "sourceFile": bean["source_file"],
                })

            # INJECTS edges: config class injects dependencies via @Bean method params
            for param in bean.get("param_types", []):
                param_fqn = param["fqn"] if isinstance(param, dict) else param
                param_qualifier = param.get("qualifier") if isinstance(param, dict) else None

                if param_fqn in self._known_fqns:
                    bean_param_injections.append({
                        "source_fqn": config_fqn,
                        "target_type_fqn": param_fqn,
                        "target_type_simple": param.get("simple", "") if isinstance(param, dict) else "",
                        "field_name": f"{bean['method_name']}_param",
                        "injection_type": "bean_method_param",
                        "qualifier": param_qualifier,
                        "source_file": bean["source_file"],
                    })

        # Write PRODUCES edges
        seen = set()
        unique_produces = []
        for edge in produces_edges:
            key = (edge["from_id"], edge["to_id"], edge["methodName"])
            if key not in seen:
                seen.add(key)
                unique_produces.append(edge)

        if unique_produces:
            self.client.batch_create_relationships(
                "PRODUCES", unique_produces,
                from_label="JavaClass", to_label="JavaClass",
                from_key="fqn", to_key="fqn",
            )
        logger.info(f"  Created {len(unique_produces)} PRODUCES edges (@Bean methods)")

        # Resolve and write bean param injection edges (same logic as field injection)
        if bean_param_injections:
            self._resolve_injections(bean_param_injections, primary_classes, {})
