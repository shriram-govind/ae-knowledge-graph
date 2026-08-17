"""
Expression Tests Extractor — Maps expression test files to the SAIL rules they test.

Extracts:
- ExpressionTest nodes (path, name, testType: unit/integration)
- TESTS_RULE relationships (ExpressionTest → SailRule)
- TESTS_IN relationships (Module → ExpressionTest, for CI mapping)

The naming convention `expressions-<ruleName>.txt` directly maps to the rule being tested.
Also parses test content for `a!evalWithMocks(a!ruleName, ...)` patterns to find additional tested rules.
"""

import re
import logging
from pathlib import Path

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Pattern to extract rule name from test filename: expressions-ruleName.txt
TEST_FILENAME_PATTERN = re.compile(r'^expressions-(.+)\.txt$')

# Pattern to find rule references inside test files
TEST_RULE_REF_PATTERN = re.compile(r'a!(\w+)\s*\(')


class ExpressionTestExtractor(BaseExtractor):
    """Maps expression test files to the SAIL rules they test."""

    @property
    def name(self) -> str:
        return "Expression Tests Extractor"

    def extract(self):
        # Find all expression test files
        test_files = self._find_test_files()
        logger.info(f"  Found {len(test_files)} expression test files")

        # Load known SAIL rule names for validation
        results = self.client.run_query("MATCH (r:SailRule) RETURN r.name AS name, r.uuid AS uuid")
        rule_name_to_uuid = {r["name"]: r["uuid"] for r in results}
        logger.info(f"  Loaded {len(rule_name_to_uuid)} known SAIL rule names for matching")

        # Load module map
        module_map = {}
        results = self.client.run_query("MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath")
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        # Process each test file
        test_nodes: list[dict] = []
        tests_rule_edges: list[dict] = []
        contains_edges: list[dict] = []

        for test_file in test_files:
            rel_path = self.relative_path(test_file)

            # Determine test type from path
            if "/unit/" in rel_path:
                test_type = "unit"
            elif "/integration/" in rel_path:
                test_type = "integration"
            elif "/shared/" in rel_path:
                test_type = "shared"
            else:
                test_type = "unknown"

            # Extract rule name from filename
            filename = test_file.name
            match = TEST_FILENAME_PATTERN.match(filename)
            primary_rule_name = match.group(1) if match else ""

            # Create test node
            test_nodes.append({
                "path": rel_path,
                "name": filename,
                "testType": test_type,
                "primaryRule": primary_rule_name,
            })

            # Link to primary rule (from filename)
            if primary_rule_name and primary_rule_name in rule_name_to_uuid:
                tests_rule_edges.append({
                    "from_id": rel_path,
                    "to_id": rule_name_to_uuid[primary_rule_name],
                    "relationship": "primary",
                })

            # Parse file content for additional rule references
            try:
                content = test_file.read_text(encoding="utf-8", errors="ignore")
                # Find all a!ruleName( patterns in the test
                referenced_rules = set(TEST_RULE_REF_PATTERN.findall(content))
                for rule_name in referenced_rules:
                    if rule_name in rule_name_to_uuid and rule_name != primary_rule_name:
                        tests_rule_edges.append({
                            "from_id": rel_path,
                            "to_id": rule_name_to_uuid[rule_name],
                            "relationship": "references",
                        })
            except Exception:
                pass

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

        # Create ExpressionTest nodes
        if test_nodes:
            self.client.batch_create_nodes("ExpressionTest", test_nodes, merge_key="path")
            logger.info(f"  Created {len(test_nodes)} ExpressionTest nodes")

        # Deduplicate and create TESTS_RULE edges
        seen = set()
        unique_edges = []
        for edge in tests_rule_edges:
            key = (edge["from_id"], edge["to_id"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(edge)

        if unique_edges:
            self.client.batch_create_relationships(
                "TESTS_RULE", unique_edges,
                from_label="ExpressionTest", to_label="SailRule",
                from_key="path", to_key="uuid",
            )
        logger.info(f"  Created {len(unique_edges)} TESTS_RULE edges")

        # Create Module → ExpressionTest CONTAINS
        if contains_edges:
            self.client.batch_create_relationships(
                "CONTAINS", contains_edges,
                from_label="Module", to_label="ExpressionTest",
                from_key="gradlePath", to_key="path",
            )
        logger.info(f"  Created {len(contains_edges)} Module→ExpressionTest CONTAINS edges")

    def _find_test_files(self) -> list[Path]:
        """Find all expression test .txt files."""
        test_files = []
        for f in self.repo_root.rglob("expressions-*.txt"):
            rel = str(f.relative_to(self.repo_root))
            if "node_modules" in rel or "/build/" in rel:
                continue
            test_files.append(f)
        return test_files
