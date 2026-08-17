"""
NPM Package Extractor — Parses package.json files to extract JavaScript/TypeScript
dependency relationships.

Extracts:
- NpmPackage nodes (name, version, type: dependency/devDependency)
- NPM_DEPENDS_ON relationships (NpmPackage → NpmPackage, or Module → NpmPackage)
- Links modules to their package.json-declared dependencies

Covers ALL package.json files in the repo (excluding node_modules).
"""

import json
import logging
from pathlib import Path
from collections import defaultdict

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)


class NpmExtractor(BaseExtractor):
    """Extracts NPM/JavaScript package dependencies from package.json files."""

    @property
    def name(self) -> str:
        return "NPM Package Extractor"

    def extract(self):
        # Find all package.json files
        package_files = self._find_package_jsons()
        logger.info(f"  Found {len(package_files)} package.json files")

        # Parse each package.json
        all_packages: dict[str, dict] = {}  # coordinate → info
        module_npm_deps: list[dict] = []  # module → package edges
        pkg_to_pkg_deps: list[dict] = []  # package → package edges

        # Load module map for linking
        module_map = self._load_module_map()

        for pkg_file in package_files:
            result = self._parse_package_json(pkg_file, module_map)
            if not result:
                continue

            # The package itself becomes a node
            pkg_name = result["name"]
            pkg_version = result["version"]
            if pkg_name:
                all_packages[pkg_name] = {
                    "name": pkg_name,
                    "version": pkg_version,
                    "path": result["path"],
                    "isWorkspace": True,  # It's a workspace package in this repo
                }

            # All dependencies become nodes + edges
            for dep_name, dep_version, dep_type in result["dependencies"]:
                # Create the dependency as a node
                if dep_name not in all_packages:
                    all_packages[dep_name] = {
                        "name": dep_name,
                        "version": dep_version,
                        "path": "",
                        "isWorkspace": False,
                    }

                # Create edge: workspace package → dependency
                if pkg_name:
                    pkg_to_pkg_deps.append({
                        "from_id": pkg_name,
                        "to_id": dep_name,
                        "depType": dep_type,
                        "version": dep_version,
                        "sourceFile": result["path"],
                    })

                # Create edge: Module → NpmPackage (if we can identify the module)
                if result["module_gradle_path"]:
                    module_npm_deps.append({
                        "from_id": result["module_gradle_path"],
                        "to_id": dep_name,
                        "depType": dep_type,
                        "version": dep_version,
                        "sourceFile": result["path"],
                    })

        logger.info(f"  Discovered {len(all_packages)} unique NPM packages")

        # Create NpmPackage nodes
        npm_nodes = []
        for pkg_name, info in all_packages.items():
            npm_nodes.append({
                "name": pkg_name,
                "version": info["version"],
                "path": info["path"],
                "isWorkspace": info["isWorkspace"],
            })

        if npm_nodes:
            self.client.batch_create_nodes("NpmPackage", npm_nodes, merge_key="name")
            logger.info(f"  Created {len(npm_nodes)} NpmPackage nodes")

        # Create NPM_DEPENDS_ON edges (package → package)
        # Deduplicate
        seen = set()
        unique_pkg_deps = []
        for dep in pkg_to_pkg_deps:
            key = (dep["from_id"], dep["to_id"])
            if key not in seen:
                seen.add(key)
                unique_pkg_deps.append(dep)

        if unique_pkg_deps:
            self.client.batch_create_relationships(
                "NPM_DEPENDS_ON", unique_pkg_deps,
                from_label="NpmPackage", to_label="NpmPackage",
                from_key="name", to_key="name",
            )
        logger.info(f"  Created {len(unique_pkg_deps)} NPM_DEPENDS_ON edges (package→package)")

        # Create Module → NpmPackage USES_NPM edges
        seen = set()
        unique_mod_deps = []
        for dep in module_npm_deps:
            key = (dep["from_id"], dep["to_id"])
            if key not in seen:
                seen.add(key)
                unique_mod_deps.append(dep)

        if unique_mod_deps:
            self.client.batch_create_relationships(
                "USES_NPM", unique_mod_deps,
                from_label="Module", to_label="NpmPackage",
                from_key="gradlePath", to_key="name",
            )
        logger.info(f"  Created {len(unique_mod_deps)} Module-[:USES_NPM]->NpmPackage edges")

        # Log top packages by dependent count
        dep_count = defaultdict(int)
        for dep in unique_pkg_deps:
            dep_count[dep["to_id"]] += 1

        top_deps = sorted(dep_count.items(), key=lambda x: x[1], reverse=True)[:15]
        if top_deps:
            logger.info("  Top 15 most-depended-on NPM packages:")
            for pkg, count in top_deps:
                logger.info(f"    {pkg}: {count} workspace packages depend on it")

    def _find_package_jsons(self) -> list[Path]:
        """Find all package.json files, excluding node_modules."""
        pkg_files = []
        for f in self.repo_root.rglob("package.json"):
            rel = str(f.relative_to(self.repo_root))
            if "node_modules" in rel or "/build/" in rel or "/.gradle/" in rel:
                continue
            pkg_files.append(f)
        return pkg_files

    def _load_module_map(self) -> dict[str, str]:
        """Load module path → gradlePath."""
        results = self.client.run_query(
            "MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath"
        )
        return {row["path"]: row["gradlePath"] for row in results}

    def _parse_package_json(self, pkg_file: Path, module_map: dict) -> dict | None:
        """Parse a package.json and extract all dependency information."""
        try:
            data = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception:
            return None

        pkg_name = data.get("name", "")
        pkg_version = data.get("version", "")
        rel_path = self.relative_path(pkg_file)

        # Find the owning module
        module_gradle_path = None
        pkg_dir = str(pkg_file.parent.relative_to(self.repo_root))
        for mod_path, gradle_path in module_map.items():
            if pkg_dir.startswith(mod_path) or pkg_dir == mod_path:
                module_gradle_path = gradle_path
                break

        # Collect all dependencies
        dependencies = []

        for dep_name, dep_version in data.get("dependencies", {}).items():
            dependencies.append((dep_name, dep_version, "dependency"))

        for dep_name, dep_version in data.get("devDependencies", {}).items():
            dependencies.append((dep_name, dep_version, "devDependency"))

        for dep_name, dep_version in data.get("peerDependencies", {}).items():
            dependencies.append((dep_name, dep_version, "peerDependency"))

        for dep_name, dep_version in data.get("optionalDependencies", {}).items():
            dependencies.append((dep_name, dep_version, "optionalDependency"))

        return {
            "name": pkg_name,
            "version": pkg_version,
            "path": rel_path,
            "module_gradle_path": module_gradle_path,
            "dependencies": dependencies,
        }
