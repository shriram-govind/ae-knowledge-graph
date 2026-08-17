"""
TypeScript/React Extractor — Parses TS/JS/TSX/JSX files for import and component usage graphs.

Extracts:
- TsFile nodes (path, name, isComponent, isTest, language)
- TS_IMPORTS relationships (ES module imports with resolved paths)
- RENDERS relationships (JSX <ComponentName> usage)
- CSS_IMPORTS relationships (import of .less/.css files)
- Module → TsFile CONTAINS relationships

Covers ALL TypeScript/JavaScript files in the repo, including tests.
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

TSX_LANGUAGE = Language(tstypescript.language_tsx())

# JSX component pattern: matches PascalCase tags like <ButtonWidget, <MyComponent
JSX_COMPONENT_PATTERN = re.compile(r'<([A-Z]\w+)')


def _get_parser() -> Parser:
    return Parser(TSX_LANGUAGE)


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


class TypeScriptExtractor(BaseExtractor):
    """Extracts TypeScript/JavaScript/React file dependencies."""

    @property
    def name(self) -> str:
        return "TypeScript/React Extractor"

    def extract(self):
        parser = _get_parser()

        # Find all TS/JS/TSX/JSX files
        ts_files = self._find_ts_files()
        logger.info(f"  Found {len(ts_files)} TypeScript/JavaScript files")

        # Build path index for import resolution
        path_index = self._build_path_index(ts_files)

        # Parse each file
        file_nodes: list[dict] = []
        import_edges: list[dict] = []
        jsx_edges: list[dict] = []
        css_import_edges: list[dict] = []

        for ts_file in ts_files:
            result = self._parse_file(parser, ts_file, path_index)
            if result:
                file_nodes.append(result["node"])
                import_edges.extend(result["imports"])
                jsx_edges.extend(result["jsx_renders"])
                css_import_edges.extend(result["css_imports"])

        logger.info(f"  Parsed {len(file_nodes)} files successfully")

        # Create TsFile nodes
        self.client.batch_create_nodes("TsFile", file_nodes, merge_key="path")
        logger.info(f"  Created {len(file_nodes)} TsFile nodes")

        # Create TS_IMPORTS edges (only where target exists)
        known_paths = {n["path"] for n in file_nodes}
        valid_imports = [e for e in import_edges if e["to_id"] in known_paths]
        if valid_imports:
            self.client.batch_create_relationships(
                "TS_IMPORTS", valid_imports,
                from_label="TsFile", to_label="TsFile",
                from_key="path", to_key="path",
            )
        logger.info(f"  Created {len(valid_imports)} TS_IMPORTS edges (from {len(import_edges)} candidates)")

        # Create RENDERS edges (JSX component usage)
        valid_jsx = [e for e in jsx_edges if e["to_id"] in known_paths]
        if valid_jsx:
            self.client.batch_create_relationships(
                "RENDERS", valid_jsx,
                from_label="TsFile", to_label="TsFile",
                from_key="path", to_key="path",
            )
        logger.info(f"  Created {len(valid_jsx)} RENDERS edges (from {len(jsx_edges)} candidates)")

        # Create CSS_IMPORTS edges
        valid_css = [e for e in css_import_edges if e["to_id"] in known_paths]
        if valid_css:
            self.client.batch_create_relationships(
                "CSS_IMPORTS", valid_css,
                from_label="TsFile", to_label="TsFile",
                from_key="path", to_key="path",
            )
        logger.info(f"  Created {len(valid_css)} CSS_IMPORTS edges")

        # Module → TsFile CONTAINS
        self._create_module_containment(file_nodes)

    def _find_ts_files(self) -> list[Path]:
        """Find all TS/JS/TSX/JSX/Less/CSS files."""
        extensions = {".ts", ".tsx", ".js", ".jsx", ".less", ".css"}
        ts_files = []

        for ext in extensions:
            for f in self.repo_root.rglob(f"*{ext}"):
                rel = str(f.relative_to(self.repo_root))
                # Skip node_modules, build outputs, dist
                if any(p in rel for p in ("node_modules/", "/build/", "/dist/", "/.gradle/")):
                    continue
                ts_files.append(f)

        return ts_files

    def _build_path_index(self, ts_files: list[Path]) -> dict[str, str]:
        """
        Build an index for import resolution.
        Maps: filename (without ext) → relative path
        Also maps: relative path → relative path (identity)
        """
        index = {}
        for f in ts_files:
            rel = self.relative_path(f)
            # Index by relative path
            index[rel] = rel
            # Index by stem (for resolving './ButtonWidget' → actual file)
            stem = f.stem
            # Handle index files: ButtonWidget/index.tsx → can be imported as './ButtonWidget'
            if stem == "index":
                parent_name = f.parent.name
                index[f"_dir_{parent_name}_{f.parent}"] = rel
        return index

    def _parse_file(self, parser: Parser, file_path: Path, path_index: dict) -> dict | None:
        """Parse a single TS/JS file."""
        rel_path = self.relative_path(file_path)
        ext = file_path.suffix

        # For CSS/Less files, just create the node (no parsing needed)
        if ext in (".less", ".css"):
            return {
                "node": {
                    "path": rel_path,
                    "name": file_path.name,
                    "isComponent": False,
                    "isTest": self._is_test_file(rel_path),
                    "language": "css" if ext == ".css" else "less",
                },
                "imports": [],
                "jsx_renders": [],
                "css_imports": [],
            }

        try:
            content = file_path.read_bytes()
        except Exception:
            return None

        # Parse with tree-sitter
        tree = parser.parse(content)
        root = tree.root_node
        content_str = content.decode("utf-8", errors="replace")

        # Detect if it's a component (exports JSX)
        has_jsx = bool(JSX_COMPONENT_PATTERN.search(content_str))

        node = {
            "path": rel_path,
            "name": file_path.name,
            "isComponent": has_jsx,
            "isTest": self._is_test_file(rel_path),
            "language": ext.lstrip("."),
        }

        imports = []
        jsx_renders = []
        css_imports = []

        # Extract import statements
        for child in root.children:
            if child.type == "import_statement":
                import_info = self._extract_import(child, file_path, path_index)
                if import_info:
                    if import_info["is_css"]:
                        css_imports.append({
                            "from_id": rel_path,
                            "to_id": import_info["resolved_path"],
                            "sourceFile": rel_path,
                        })
                    else:
                        imports.append({
                            "from_id": rel_path,
                            "to_id": import_info["resolved_path"],
                            "importedName": import_info.get("imported_name", ""),
                            "sourceFile": rel_path,
                        })

        # Extract JSX component usage (PascalCase tags)
        if has_jsx:
            jsx_components = set(JSX_COMPONENT_PATTERN.findall(content_str))
            # Try to resolve each component to an imported file
            for comp_name in jsx_components:
                # Check if this component is imported in this file
                for imp in imports:
                    if imp.get("importedName") == comp_name:
                        jsx_renders.append({
                            "from_id": rel_path,
                            "to_id": imp["to_id"],
                            "componentName": comp_name,
                            "sourceFile": rel_path,
                        })
                        break

        return {
            "node": node,
            "imports": imports,
            "jsx_renders": jsx_renders,
            "css_imports": css_imports,
        }

    def _extract_import(self, import_node, file_path: Path, path_index: dict) -> dict | None:
        """Extract import source path and resolve it."""
        # Find the string literal (source path)
        source_str = None
        imported_names = []

        for child in import_node.children:
            if child.type == "string":
                source_str = _node_text(child).strip("'\"")
            elif child.type == "import_clause":
                for sc in child.children:
                    if sc.type == "identifier":
                        imported_names.append(_node_text(sc))
                    elif sc.type == "named_imports":
                        for imp in sc.children:
                            if imp.type == "import_specifier":
                                for isc in imp.children:
                                    if isc.type == "identifier":
                                        imported_names.append(_node_text(isc))
                                        break

        if not source_str:
            return None

        # Skip external package imports (not relative)
        if not source_str.startswith(".") and not source_str.startswith("/"):
            return None

        # Resolve relative path
        resolved = self._resolve_import_path(source_str, file_path, path_index)
        if not resolved:
            return None

        is_css = resolved.endswith(".less") or resolved.endswith(".css")

        return {
            "resolved_path": resolved,
            "imported_name": imported_names[0] if imported_names else "",
            "is_css": is_css,
        }

    def _resolve_import_path(self, import_source: str, from_file: Path, path_index: dict) -> str | None:
        """Resolve a relative import path to an actual file path."""
        from_dir = from_file.parent

        # Resolve the relative path
        if import_source.startswith("."):
            target = (from_dir / import_source).resolve()
        else:
            target = (self.repo_root / import_source.lstrip("/")).resolve()

        # Try various extensions
        extensions = [".tsx", ".ts", ".js", ".jsx", ".less", ".css", "/index.tsx", "/index.ts", "/index.js"]

        # Check if it already has an extension
        if target.suffix in (".tsx", ".ts", ".js", ".jsx", ".less", ".css"):
            try:
                rel = str(target.relative_to(self.repo_root))
                if rel in path_index:
                    return rel
            except ValueError:
                pass
            return None

        # Try each extension
        for ext in extensions:
            candidate = Path(str(target) + ext)
            try:
                rel = str(candidate.relative_to(self.repo_root))
                if rel in path_index:
                    return rel
            except ValueError:
                continue

        return None

    def _is_test_file(self, rel_path: str) -> bool:
        """Check if a file is a test file."""
        return (
            "__tests__" in rel_path
            or "__test__" in rel_path
            or rel_path.endswith(".test.ts")
            or rel_path.endswith(".test.tsx")
            or rel_path.endswith(".test.js")
            or rel_path.endswith(".test.jsx")
            or rel_path.endswith(".spec.ts")
            or rel_path.endswith(".spec.tsx")
            or "-test." in rel_path
        )

    def _create_module_containment(self, file_nodes: list[dict]):
        """Create Module → TsFile CONTAINS edges."""
        module_map = {}
        results = self.client.run_query(
            "MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath"
        )
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        contains_rels = []
        for node in file_nodes:
            file_path = node["path"]
            parts = file_path.split("/")
            for i in range(len(parts), 0, -1):
                candidate = "/".join(parts[:i])
                if candidate in module_map:
                    contains_rels.append({
                        "from_id": module_map[candidate],
                        "to_id": node["path"],
                    })
                    break

        if contains_rels:
            self.client.batch_create_relationships(
                "CONTAINS", contains_rels,
                from_label="Module", to_label="TsFile",
                from_key="gradlePath", to_key="path",
            )
            logger.info(f"  Created {len(contains_rels)} Module→TsFile CONTAINS edges")
