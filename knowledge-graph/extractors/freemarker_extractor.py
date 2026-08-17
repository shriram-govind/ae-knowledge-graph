"""
FreeMarker Template Extractor — Maps .ftl/.ftlx template files to Java callers.

Extracts:
- FtlTemplate nodes (path, name, variables used)
- USES_TEMPLATE relationships (JavaClass → FtlTemplate) based on filename references in Java
- Module → FtlTemplate CONTAINS relationships
"""

import re
import logging
from pathlib import Path

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Pattern to find FTL variable usage: ${variableName}
FTL_VARIABLE_PATTERN = re.compile(r'\$\{(\w+)')


class FreemarkerExtractor(BaseExtractor):
    """Maps FreeMarker templates to Java code that uses them."""

    @property
    def name(self) -> str:
        return "FreeMarker Template Extractor"

    def extract(self):
        # Find all FTL files
        ftl_files = []
        for ext in (".ftl", ".ftlx"):
            for f in self.repo_root.rglob(f"*{ext}"):
                rel = str(f.relative_to(self.repo_root))
                if "node_modules" not in rel and "/build/" not in rel:
                    ftl_files.append(f)

        logger.info(f"  Found {len(ftl_files)} FreeMarker template files")

        # Load module map
        module_map = {}
        results = self.client.run_query("MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath")
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        # Create FtlTemplate nodes
        template_nodes = []
        contains_edges = []
        ftl_name_to_path = {}  # filename → rel_path (for matching against Java references)

        for ftl_file in ftl_files:
            rel_path = self.relative_path(ftl_file)

            # Extract template variables
            try:
                content = ftl_file.read_text(encoding="utf-8", errors="ignore")
                variables = set(FTL_VARIABLE_PATTERN.findall(content))
                variable_str = ",".join(sorted(variables)[:20])  # First 20
            except Exception:
                variable_str = ""

            template_nodes.append({
                "path": rel_path,
                "name": ftl_file.name,
                "variables": variable_str,
            })

            ftl_name_to_path[ftl_file.name] = rel_path
            # Also index without extension for flexible matching
            ftl_name_to_path[ftl_file.stem] = rel_path

            # Module containment
            parts = rel_path.split("/")
            for i in range(len(parts), 0, -1):
                candidate = "/".join(parts[:i])
                if candidate in module_map:
                    contains_edges.append({
                        "from_id": module_map[candidate],
                        "to_id": rel_path,
                    })
                    break

        if template_nodes:
            self.client.batch_create_nodes("FtlTemplate", template_nodes, merge_key="path")
            logger.info(f"  Created {len(template_nodes)} FtlTemplate nodes")

        if contains_edges:
            self.client.batch_create_relationships(
                "CONTAINS", contains_edges,
                from_label="Module", to_label="FtlTemplate",
                from_key="gradlePath", to_key="path",
            )
            logger.info(f"  Created {len(contains_edges)} Module→FtlTemplate CONTAINS edges")

        # Find Java files that reference template filenames
        uses_template_edges = self._find_java_references(ftl_name_to_path)
        if uses_template_edges:
            self.client.batch_create_relationships(
                "USES_TEMPLATE", uses_template_edges,
                from_label="JavaClass", to_label="FtlTemplate",
                from_key="fqn", to_key="path",
            )
        logger.info(f"  Created {len(uses_template_edges)} USES_TEMPLATE edges (Java→FtlTemplate)")

    def _find_java_references(self, ftl_name_to_path: dict) -> list[dict]:
        """Find Java files that reference FTL template filenames."""
        edges = []
        # Build a regex that matches any known template filename in a string
        ftl_names = [re.escape(name) for name in ftl_name_to_path.keys() if name.endswith((".ftl", ".ftlx"))]
        if not ftl_names:
            return edges

        # Search Java files for template name references
        ftl_ref_pattern = re.compile(r'"([^"]*(?:' + "|".join(ftl_names[:100]) + r')[^"]*)"')

        for java_file in self.repo_root.rglob("*.java"):
            rel = str(java_file.relative_to(self.repo_root))
            if "/build/" in rel or "node_modules/" in rel or "/.gradle/" in rel:
                continue
            if "/src/" not in rel:
                continue

            try:
                content = java_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Quick check: does this file mention .ftl at all?
            if ".ftl" not in content:
                continue

            # Derive FQN
            match = re.search(r'/java/(.+)$', rel)
            if not match:
                continue
            fqn = match.group(1).replace("/", ".").rstrip(".java")[:-5] if match.group(1).endswith(".java") else None
            if not fqn:
                fqn = match.group(1).replace("/", ".")[:-5]

            # Find template references
            for ftl_name, ftl_path in ftl_name_to_path.items():
                if ftl_name.endswith((".ftl", ".ftlx")) and ftl_name in content:
                    edges.append({
                        "from_id": fqn,
                        "to_id": ftl_path,
                        "sourceFile": rel,
                    })

        # Deduplicate
        seen = set()
        unique = []
        for e in edges:
            key = (e["from_id"], e["to_id"])
            if key not in seen:
                seen.add(key)
                unique.append(e)

        return unique
