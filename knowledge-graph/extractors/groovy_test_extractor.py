"""
Groovy Test Extractor — Maps Groovy/Spock test files to the Java classes they test.

Extracts:
- GroovyTest nodes (path, name, testFramework: spock/junit)
- TESTS_CLASS relationships (GroovyTest → JavaClass)
- Module → GroovyTest CONTAINS relationships

Uses:
- Import statements in Groovy to find tested classes
- Class name conventions (XTest, XSpec → tests X)
"""

import re
import logging
from pathlib import Path

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Patterns
GROOVY_IMPORT = re.compile(r'^import\s+([\w.]+)\s*$', re.MULTILINE)
GROOVY_EXTENDS = re.compile(r'class\s+\w+\s+extends\s+(\w+)')


class GroovyTestExtractor(BaseExtractor):
    """Maps Groovy test files to the Java classes they test."""

    @property
    def name(self) -> str:
        return "Groovy Test Extractor"

    def extract(self):
        # Find all Groovy files
        groovy_files = list(self.repo_root.rglob("*.groovy"))
        groovy_files = [f for f in groovy_files if "node_modules" not in str(f) and "/build/" not in str(f)]
        logger.info(f"  Found {len(groovy_files)} Groovy files")

        # Load known Java class FQNs
        results = self.client.run_query("MATCH (c:JavaClass) RETURN c.fqn AS fqn")
        known_fqns = {r["fqn"] for r in results}

        # Load module map
        module_map = {}
        results = self.client.run_query("MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath")
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        # Process each Groovy file
        test_nodes: list[dict] = []
        tests_class_edges: list[dict] = []
        contains_edges: list[dict] = []

        for groovy_file in groovy_files:
            rel_path = self.relative_path(groovy_file)

            # Determine test framework
            try:
                content = groovy_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            if "spock" in content.lower() or "Specification" in content:
                framework = "spock"
            else:
                framework = "groovy-junit"

            test_nodes.append({
                "path": rel_path,
                "name": groovy_file.stem,
                "testFramework": framework,
            })

            # Extract imports → link to tested Java classes
            imports = GROOVY_IMPORT.findall(content)
            for imp_fqn in imports:
                if imp_fqn in known_fqns:
                    # Skip common test infrastructure imports
                    if any(skip in imp_fqn for skip in ("org.junit", "spock.", "org.mockito", "org.hamcrest")):
                        continue
                    tests_class_edges.append({
                        "from_id": rel_path,
                        "to_id": imp_fqn,
                    })

            # Also try convention: TestClassName → ClassName mapping
            stem = groovy_file.stem
            for suffix in ("Test", "Spec", "IntegrationTest", "UnitTest", "SystemTest"):
                if stem.endswith(suffix):
                    tested_name = stem[:-len(suffix)]
                    # Search for this class name in known FQNs
                    matching = [fqn for fqn in known_fqns if fqn.endswith(f".{tested_name}")]
                    for fqn in matching[:3]:  # Limit to avoid ambiguity
                        tests_class_edges.append({
                            "from_id": rel_path,
                            "to_id": fqn,
                        })
                    break

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

        # Create GroovyTest nodes
        if test_nodes:
            self.client.batch_create_nodes("GroovyTest", test_nodes, merge_key="path")
            logger.info(f"  Created {len(test_nodes)} GroovyTest nodes")

        # Deduplicate and create TESTS_CLASS edges
        seen = set()
        unique_edges = []
        for e in tests_class_edges:
            key = (e["from_id"], e["to_id"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(e)

        if unique_edges:
            self.client.batch_create_relationships(
                "TESTS_CLASS", unique_edges,
                from_label="GroovyTest", to_label="JavaClass",
                from_key="path", to_key="fqn",
            )
        logger.info(f"  Created {len(unique_edges)} TESTS_CLASS edges (GroovyTest→JavaClass)")

        # Module containment
        if contains_edges:
            self.client.batch_create_relationships(
                "CONTAINS", contains_edges,
                from_label="Module", to_label="GroovyTest",
                from_key="gradlePath", to_key="path",
            )
        logger.info(f"  Created {len(contains_edges)} Module→GroovyTest CONTAINS edges")
