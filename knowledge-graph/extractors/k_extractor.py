"""
K Language Extractor — Parses .k files for load directives and function definitions.

Extracts:
- KFile nodes (path, name, engine)
- LOADS relationships (\\l filename directives)
- K_DEFINES relationships (function definitions within files)
- Module → KFile CONTAINS relationships

K is a terse array-processing language. Files are loaded via \\l directives.
Functions are defined as name:{[params] body} or name: expression.
"""

import re
import logging
from pathlib import Path

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Regex patterns
# \l filename or \l path/to/filename (standard K load - can appear anywhere in a line)
K_LOAD_PATTERN = re.compile(r'\\l\s+(\S+)')

# Appian custom loading: .a.s.l["filename"] or .a.s.l`filename
K_APPIAN_LOAD_QUOTED = re.compile(r'\.a\.s\.l\[\s*"([^"]+)"\s*\]')
K_APPIAN_LOAD_SYMBOL = re.compile(r'\.a\.s\.l\s*`"?(\w[\w-]*)"?')

# .a.s.ld["path/"; "name"] (load from directory)
K_APPIAN_LOADDIR = re.compile(r'\.a\.s\.ld\[\s*"([^"]+)"\s*;\s*"([^"]+)"\s*\]')

# Function definition: name:{[params] body} or name:expression (at start of line)
K_FUNC_DEF_PATTERN = re.compile(r'^([a-zA-Z_]\w*)\s*:\s*\{', re.MULTILINE)

# Engine mapping based on directory
ENGINE_MAP = {
    "personalization": "personalization",
    "exec": "exec",
    "design": "design",
    "collaboration": "collaboration",
    "forums": "forums",
    "portal": "portal",
    "channels": "channels",
    "analytics": "analytics",
    "notifications": "notifications",
    "process": "process",
    "gateway": "gateway",
}


class KExtractor(BaseExtractor):
    """Extracts K language file dependencies and function definitions."""

    @property
    def name(self) -> str:
        return "K Language Extractor"

    def extract(self):
        # Find all .k files
        k_files = list(self.repo_root.rglob("*.k"))
        k_files = [f for f in k_files if not any(
            p in str(f) for p in ("node_modules/", "/build/", "/.gradle/")
        )]
        logger.info(f"  Found {len(k_files)} K language files")

        # Build path index for resolving \l references
        k_file_map = {}  # filename → relative path
        for f in k_files:
            rel = self.relative_path(f)
            k_file_map[f.name] = rel
            # Also index by relative path from server/
            if "server/" in rel:
                server_rel = rel.split("server/", 1)[1] if "server/" in rel else rel
                k_file_map[server_rel] = rel

        # Parse each file
        file_nodes: list[dict] = []
        load_edges: list[dict] = []
        func_defs: list[dict] = []

        for k_file in k_files:
            rel_path = self.relative_path(k_file)
            engine = self._detect_engine(rel_path)

            file_nodes.append({
                "path": rel_path,
                "name": k_file.name,
                "engine": engine,
                "isTest": "test" in rel_path.lower(),
            })

            try:
                content = k_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Extract \l load directives (standard K)
            for match in K_LOAD_PATTERN.finditer(content):
                loaded_name = match.group(1).strip()
                resolved = self._resolve_k_load(loaded_name, k_file, k_file_map)
                if resolved:
                    load_edges.append({
                        "from_id": rel_path,
                        "to_id": resolved,
                        "loadedName": loaded_name,
                        "sourceFile": rel_path,
                    })

            # Extract .a.s.l["filename"] loads (Appian custom)
            for match in K_APPIAN_LOAD_QUOTED.finditer(content):
                loaded_name = match.group(1).strip()
                resolved = self._resolve_k_load(loaded_name, k_file, k_file_map)
                if resolved:
                    load_edges.append({
                        "from_id": rel_path,
                        "to_id": resolved,
                        "loadedName": loaded_name,
                        "sourceFile": rel_path,
                    })

            # Extract .a.s.l`filename loads (symbol form)
            for match in K_APPIAN_LOAD_SYMBOL.finditer(content):
                loaded_name = match.group(1).strip()
                resolved = self._resolve_k_load(loaded_name, k_file, k_file_map)
                if resolved:
                    load_edges.append({
                        "from_id": rel_path,
                        "to_id": resolved,
                        "loadedName": loaded_name,
                        "sourceFile": rel_path,
                    })

            # Extract .a.s.ld["dir/";"name"] loads
            for match in K_APPIAN_LOADDIR.finditer(content):
                dir_name = match.group(1).strip()
                file_name = match.group(2).strip()
                loaded_name = f"{dir_name}{file_name}"
                resolved = self._resolve_k_load(loaded_name, k_file, k_file_map)
                if resolved:
                    load_edges.append({
                        "from_id": rel_path,
                        "to_id": resolved,
                        "loadedName": loaded_name,
                        "sourceFile": rel_path,
                    })

            # Extract function definitions
            for match in K_FUNC_DEF_PATTERN.finditer(content):
                func_name = match.group(1)
                func_defs.append({
                    "file_path": rel_path,
                    "func_name": func_name,
                })

        # Create KFile nodes
        self.client.batch_create_nodes("KFile", file_nodes, merge_key="path")
        logger.info(f"  Created {len(file_nodes)} KFile nodes")

        # Create LOADS edges
        known_paths = {n["path"] for n in file_nodes}
        valid_loads = [e for e in load_edges if e["to_id"] in known_paths]
        if valid_loads:
            self.client.batch_create_relationships(
                "LOADS", valid_loads,
                from_label="KFile", to_label="KFile",
                from_key="path", to_key="path",
            )
        logger.info(f"  Created {len(valid_loads)} LOADS edges (from {len(load_edges)} candidates)")

        # Store function count on nodes
        file_func_count = {}
        for fd in func_defs:
            file_func_count[fd["file_path"]] = file_func_count.get(fd["file_path"], 0) + 1

        for file_path, count in file_func_count.items():
            self.client.run_query(
                "MATCH (k:KFile {path: $path}) SET k.functionCount = $count",
                path=file_path, count=count,
            )
        logger.info(f"  Found {len(func_defs)} function definitions across {len(file_func_count)} files")

        # Module → KFile CONTAINS
        self._create_module_containment(file_nodes)

    def _detect_engine(self, rel_path: str) -> str:
        """Detect which K engine a file belongs to based on path."""
        for dir_name, engine_name in ENGINE_MAP.items():
            if f"/{dir_name}/" in rel_path or rel_path.startswith(f"server/{dir_name}/"):
                return engine_name
        return "unknown"

    def _resolve_k_load(self, loaded_name: str, from_file: Path, k_file_map: dict) -> str | None:
        """Resolve a \\l reference to an actual file path."""
        # Try direct filename match
        if loaded_name in k_file_map:
            return k_file_map[loaded_name]

        # Try relative to the loading file's directory
        from_dir = from_file.parent
        candidate = from_dir / loaded_name
        if candidate.exists():
            try:
                return str(candidate.relative_to(self.repo_root))
            except ValueError:
                pass

        # Try with .k extension appended
        if not loaded_name.endswith(".k"):
            if f"{loaded_name}.k" in k_file_map:
                return k_file_map[f"{loaded_name}.k"]

        return None

    def _create_module_containment(self, file_nodes: list[dict]):
        """Create Module → KFile CONTAINS edges."""
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
                from_label="Module", to_label="KFile",
                from_key="gradlePath", to_key="path",
            )
            logger.info(f"  Created {len(contains_rels)} Module→KFile CONTAINS edges")
