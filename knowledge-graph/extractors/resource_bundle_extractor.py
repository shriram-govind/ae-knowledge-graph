"""
Resource Bundle Extractor — Maps i18n resource bundles to code that references their keys.

Extracts:
- ResourceBundle nodes (path, name, module, keyCount)
- Module → ResourceBundle CONTAINS relationships
- SailRule → ResourceBundle USES_RESOURCE relationships (via getResourceString patterns)

Resource bundles are *_en_US.properties files containing translation keys.
SAIL rules reference them via a!module_getResourceString(keys: "key.name") or
fn!resource_appian_internal("key") or fn!resourceFromBundle_appian_internal("bundle", "key").
"""

import re
import logging
from pathlib import Path

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)


class ResourceBundleExtractor(BaseExtractor):
    """Maps i18n resource bundles to code that references them."""

    @property
    def name(self) -> str:
        return "Resource Bundle Extractor"

    def extract(self):
        # Find all _en_US.properties files (the primary locale)
        bundle_files = list(self.repo_root.rglob("*_en_US.properties"))
        bundle_files = [f for f in bundle_files if "node_modules" not in str(f) and "/build/" not in str(f)]
        logger.info(f"  Found {len(bundle_files)} resource bundle files")

        # Load module map
        module_map = {}
        results = self.client.run_query("MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath")
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        # Create ResourceBundle nodes
        bundle_nodes = []
        contains_edges = []

        for bundle_file in bundle_files:
            rel_path = self.relative_path(bundle_file)

            # Count keys
            try:
                content = bundle_file.read_text(encoding="utf-8", errors="ignore")
                key_count = sum(1 for line in content.splitlines()
                               if line.strip() and not line.startswith("#") and "=" in line)
            except Exception:
                key_count = 0

            bundle_nodes.append({
                "path": rel_path,
                "name": bundle_file.stem,  # e.g., "endUserReporting_en_US"
                "keyCount": key_count,
            })

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

        if bundle_nodes:
            self.client.batch_create_nodes("ResourceBundle", bundle_nodes, merge_key="path")
            logger.info(f"  Created {len(bundle_nodes)} ResourceBundle nodes (total {sum(n['keyCount'] for n in bundle_nodes):,} keys)")

        if contains_edges:
            self.client.batch_create_relationships(
                "CONTAINS", contains_edges,
                from_label="Module", to_label="ResourceBundle",
                from_key="gradlePath", to_key="path",
            )
            logger.info(f"  Created {len(contains_edges)} Module→ResourceBundle CONTAINS edges")
