"""
External Library Extractor — Extracts external (Maven/Gradle) library dependencies.

Creates:
- :ExternalLibrary nodes (group:artifact:version)
- DEPENDS_ON_LIB relationships (Module → ExternalLibrary with config type)

Parses both Groovy DSL and Kotlin DSL patterns for external dependencies.
Also handles version catalog references (libs.versions.toml).
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Regex patterns for external dependency declarations
# Groovy: implementation 'group:artifact:version'
# Groovy: implementation "group:artifact:version"
GROOVY_EXT_DEP = re.compile(
    r"(\w+)\s+['\"]([^'\"]+):([^'\"]+):([^'\"]*)['\"]"
)

# Groovy without version (managed by BOM/platform): implementation 'group:artifact'
GROOVY_EXT_DEP_NO_VERSION = re.compile(
    r"(\w+)\s+['\"]([^'\"]+):([^'\"]+)['\"]"
)

# Kotlin DSL: implementation("group:artifact:version")
KOTLIN_EXT_DEP = re.compile(
    r"(\w+)\(\s*['\"]([^'\"]+):([^'\"]+):([^'\"]*)['\"]"
)

# Configurations we track
DEPENDENCY_CONFIGS = {
    "implementation", "api", "compileOnly", "runtimeOnly",
    "testImplementation", "testRuntimeOnly", "testCompileOnly",
    "integrationTestImplementation", "systemTestImplementation",
    "sharedTestImplementation", "annotationProcessor",
}

# Skip internal project references (these are handled by the Gradle extractor)
# Also skip common non-library patterns
SKIP_PATTERNS = {
    "project(", "files(", "fileTree(",
}


class ExternalLibraryExtractor(BaseExtractor):
    """Extracts external library dependencies from Gradle files."""

    @property
    def name(self) -> str:
        return "External Library Extractor"

    def extract(self):
        # Get module paths from graph
        module_map = self._load_module_map()

        # Parse globalDependencies.groovy for the master dependency registry (group:artifact:version)
        global_deps = self._parse_global_dependencies()
        logger.info(f"  Found {len(global_deps)} dependencies in globalDependencies.groovy")

        # Parse all gradle files for external dependencies (direct declarations)
        libraries, module_lib_deps = self._extract_external_deps(module_map)

        # Parse all gradle files for globalDep() references
        global_dep_usages = self._extract_global_dep_usages(module_map, global_deps)
        logger.info(f"  Found {len(global_dep_usages)} globalDep() usages across modules")

        # Parse the lockfile for the FULL resolved dependency set
        lockfile_libs = self._parse_lockfile()
        logger.info(f"  Found {len(lockfile_libs)} libraries in lockfile (full transitive closure)")

        # Parse version catalog (libs.versions.toml) for managed versions
        catalog_libs = self._parse_version_catalog()
        logger.info(f"  Found {len(catalog_libs)} libraries in version catalog")

        # Merge ALL sources: globalDependencies + lockfile + direct declarations + catalog
        for coord, info in global_deps.items():
            if coord not in libraries:
                libraries[coord] = info

        for coord, info in lockfile_libs.items():
            if coord not in libraries:
                libraries[coord] = info

        for coord, info in catalog_libs.items():
            if coord in libraries:
                libraries[coord]["catalogAlias"] = info.get("alias", "")
            else:
                libraries[coord] = info

        logger.info(f"  Total unique external libraries: {len(libraries)}")

        # Create ExternalLibrary nodes
        lib_nodes = []
        for lib_key, lib_info in libraries.items():
            lib_nodes.append({
                "coordinate": lib_key,  # group:artifact
                "group": lib_info["group"],
                "artifact": lib_info["artifact"],
                "latestVersion": lib_info.get("latest_version") or lib_info.get("version", ""),
                "name": lib_info["artifact"],  # for display
                "catalogAlias": lib_info.get("catalogAlias", ""),
            })

        if lib_nodes:
            self.client.batch_create_nodes("ExternalLibrary", lib_nodes, merge_key="coordinate")
            logger.info(f"  Created {len(lib_nodes)} ExternalLibrary nodes")

        # Create DEPENDS_ON_LIB relationships from direct declarations
        dep_edges = []
        for module_gradle_path, deps in module_lib_deps.items():
            for dep in deps:
                dep_edges.append({
                    "from_id": module_gradle_path,
                    "to_id": dep["coordinate"],
                    "config": dep["config"],
                    "version": dep["version"],
                    "sourceFile": dep["source_file"],
                })

        # Also add globalDep() usages as DEPENDS_ON_LIB
        for usage in global_dep_usages:
            dep_edges.append({
                "from_id": usage["module_gradle_path"],
                "to_id": usage["coordinate"],
                "config": usage["config"],
                "version": usage.get("version", ""),
                "sourceFile": usage["source_file"],
            })

        # Filter to only edges where both sides exist
        known_modules = set(module_map.values())
        known_libs = {n["coordinate"] for n in lib_nodes}
        valid_edges = [e for e in dep_edges if e["from_id"] in known_modules and e["to_id"] in known_libs]

        if valid_edges:
            self.client.batch_create_relationships(
                "DEPENDS_ON_LIB", valid_edges,
                from_label="Module", to_label="ExternalLibrary",
                from_key="gradlePath", to_key="coordinate",
            )
        logger.info(f"  Created {len(valid_edges)} DEPENDS_ON_LIB relationships (direct + globalDep)")

        # Log top libraries by usage
        lib_usage = defaultdict(int)
        for e in valid_edges:
            lib_usage[e["to_id"]] += 1

        top_libs = sorted(lib_usage.items(), key=lambda x: x[1], reverse=True)[:10]
        if top_libs:
            logger.info("  Top 10 most directly-declared external libraries:")
            for lib, count in top_libs:
                logger.info(f"    {lib}: {count} modules")

    def _parse_global_dependencies(self) -> dict[str, dict]:
        """Parse gradle/scripts/globalDependencies.groovy for the master dependency map."""
        global_file = self.repo_root / "gradle" / "scripts" / "globalDependencies.groovy"
        libraries = {}

        if not global_file.exists():
            return libraries

        content = global_file.read_text(encoding="utf-8", errors="ignore")

        # Pattern: dep "group:artifact:version" or dep "group:artifact:${variable}"
        dep_pattern = re.compile(r'^dep\s+"([^"]+)"', re.MULTILINE)

        for match in dep_pattern.finditer(content):
            dep_str = match.group(1)
            parts = dep_str.split(":")
            if len(parts) >= 2:
                group = parts[0]
                artifact = parts[1]
                version = parts[2] if len(parts) >= 3 else ""
                # Clean up version (may have ${variable} or !! suffix)
                version = re.sub(r'\$\{[^}]+\}', 'managed', version)
                version = version.rstrip("!")

                coordinate = f"{group}:{artifact}"
                libraries[coordinate] = {
                    "group": group,
                    "artifact": artifact,
                    "latest_version": version,
                    "source": "globalDependencies",
                }

        return libraries

    def _extract_global_dep_usages(self, module_map: dict, global_deps: dict) -> list[dict]:
        """Find all globalDep('group:artifact') usages across gradle files and map to modules."""
        usages = []

        # Pattern: configName globalDep('group:artifact') or configName(globalDep('group:artifact'))
        global_dep_pattern = re.compile(
            r"(\w+)\s*(?:\()?globalDep\(\s*['\"]([^'\"]+)['\"]"
        )

        gradle_files = list(self.repo_root.rglob("*.gradle"))
        gradle_files = [f for f in gradle_files if not any(
            part.startswith(".") or part in ("node_modules", "build", "buildSrc")
            for part in f.parts
        )]

        for gradle_file in gradle_files:
            module_dir = gradle_file.parent
            try:
                rel = str(module_dir.relative_to(self.repo_root))
            except ValueError:
                continue

            module_gradle_path = module_map.get(rel)
            if not module_gradle_path:
                # Root build.gradle uses ":" as its path
                if gradle_file.name == "build.gradle" and rel == ".":
                    module_gradle_path = ":"
                else:
                    continue

            rel_file = str(gradle_file.relative_to(self.repo_root))

            try:
                content = gradle_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for match in global_dep_pattern.finditer(content):
                config = match.group(1)
                coordinate = match.group(2)

                # Ensure it looks like a valid coordinate (has at least one colon)
                if ":" not in coordinate:
                    continue

                # Normalize: some have only group:artifact, some have group:artifact:version
                parts = coordinate.split(":")
                coord_key = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else coordinate

                usages.append({
                    "module_gradle_path": module_gradle_path,
                    "coordinate": coord_key,
                    "config": config,
                    "version": global_deps.get(coord_key, {}).get("latest_version", ""),
                    "source_file": rel_file,
                })

        return usages

    def _parse_lockfile(self) -> dict[str, dict]:
        """Parse deployment/gradle.lockfile for the full resolved dependency set."""
        lockfile = self.repo_root / "deployment" / "gradle.lockfile"
        libraries = {}

        if not lockfile.exists():
            return libraries

        try:
            content = lockfile.read_text()
        except Exception:
            return libraries

        for line in content.splitlines():
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue
            # Format: group:artifact:version=configuration
            if "=" in line:
                coord_version, _ = line.split("=", 1)
                parts = coord_version.split(":")
                if len(parts) >= 3:
                    group, artifact, version = parts[0], parts[1], parts[2]
                    coordinate = f"{group}:{artifact}"
                    libraries[coordinate] = {
                        "group": group,
                        "artifact": artifact,
                        "latest_version": version,
                        "version": version,
                        "source": "lockfile",
                    }

        return libraries

    def _parse_version_catalog(self) -> dict[str, dict]:
        """Parse gradle/libs.versions.toml for version catalog entries."""
        catalog_file = self.repo_root / "gradle" / "libs.versions.toml"
        libraries = {}

        if not catalog_file.exists():
            return libraries

        try:
            content = catalog_file.read_text()
        except Exception:
            return libraries

        # Simple TOML parsing for [libraries] section
        in_libraries = False
        for line in content.splitlines():
            line = line.strip()
            if line == "[libraries]":
                in_libraries = True
                continue
            if line.startswith("[") and in_libraries:
                break
            if not in_libraries or not line or line.startswith("#"):
                continue

            # Format: alias = { module = "group:artifact", version.ref = "key" }
            match = re.match(r'(\w[\w-]*)\s*=\s*\{.*module\s*=\s*"([^"]+):([^"]+)"', line)
            if match:
                alias, group, artifact = match.groups()
                coordinate = f"{group}:{artifact}"
                # Try to extract version
                version_match = re.search(r'version(?:\.ref)?\s*=\s*"([^"]+)"', line)
                version = version_match.group(1) if version_match else ""

                libraries[coordinate] = {
                    "group": group,
                    "artifact": artifact,
                    "latest_version": version,
                    "alias": alias,
                    "source": "catalog",
                }

        return libraries

    def _load_module_map(self) -> dict[str, str]:
        """Load module path → gradlePath from graph. Includes root project."""
        results = self.client.run_query(
            "MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath"
        )
        module_map = {row["path"]: row["gradlePath"] for row in results}
        # Add root project mapping
        module_map["."] = ":"
        module_map[""] = ":"
        return module_map

    def _extract_external_deps(self, module_map: dict) -> tuple[dict, dict]:
        """
        Parse all gradle files for external dependency declarations.
        Returns:
            libraries: {coordinate → {group, artifact, latest_version}}
            module_deps: {module_gradle_path → [{coordinate, config, version, source_file}]}
        """
        libraries = {}  # coordinate → info
        module_deps = defaultdict(list)  # module_gradle_path → [deps]

        gradle_files = list(self.repo_root.rglob("*.gradle"))
        gradle_files += list(self.repo_root.rglob("*.gradle.kts"))

        # Filter
        gradle_files = [
            f for f in gradle_files
            if not any(part.startswith(".") or part in ("node_modules", "build", "buildSrc")
                      for part in f.parts)
        ]

        for gradle_file in gradle_files:
            # Determine module
            module_dir = gradle_file.parent
            try:
                rel = str(module_dir.relative_to(self.repo_root))
            except ValueError:
                continue

            module_gradle_path = module_map.get(rel)
            if not module_gradle_path:
                continue

            rel_file = self.relative_path(gradle_file)

            try:
                content = gradle_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for line_num, line in enumerate(content.splitlines(), start=1):
                stripped = line.strip()

                # Skip comments and non-dependency lines
                if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                    continue
                if any(skip in stripped for skip in SKIP_PATTERNS):
                    continue

                # Try to match external dependency patterns
                dep_info = self._parse_dep_line(stripped)
                if dep_info and dep_info["config"] in DEPENDENCY_CONFIGS:
                    coordinate = f"{dep_info['group']}:{dep_info['artifact']}"

                    # Update library registry
                    if coordinate not in libraries:
                        libraries[coordinate] = {
                            "group": dep_info["group"],
                            "artifact": dep_info["artifact"],
                            "latest_version": dep_info["version"],
                        }
                    elif dep_info["version"] and dep_info["version"] > libraries[coordinate].get("latest_version", ""):
                        libraries[coordinate]["latest_version"] = dep_info["version"]

                    # Add to module deps
                    module_deps[module_gradle_path].append({
                        "coordinate": coordinate,
                        "config": dep_info["config"],
                        "version": dep_info["version"],
                        "source_file": rel_file,
                    })

        return libraries, dict(module_deps)

    def _parse_dep_line(self, line: str) -> dict | None:
        """Try to parse a dependency declaration from a single line."""
        # Skip project() references
        if "project(" in line:
            return None

        # Try Groovy with version: implementation 'group:artifact:version'
        match = GROOVY_EXT_DEP.search(line)
        if match:
            config, group, artifact, version = match.groups()
            # Validate it looks like a real coordinate (has dots in group)
            if "." in group:
                return {"config": config, "group": group, "artifact": artifact, "version": version}

        # Try Kotlin with version: implementation("group:artifact:version")
        match = KOTLIN_EXT_DEP.search(line)
        if match:
            config, group, artifact, version = match.groups()
            if "." in group:
                return {"config": config, "group": group, "artifact": artifact, "version": version}

        # Try without version: implementation 'group:artifact'
        match = GROOVY_EXT_DEP_NO_VERSION.search(line)
        if match:
            config, group, artifact = match.groups()
            # Must have dots in group and NOT look like a project reference
            if "." in group and ":" not in artifact:
                return {"config": config, "group": group, "artifact": artifact, "version": ""}

        return None
