"""
Gradle Extractor — Parses .gradle and .gradle.kts files to extract module dependency graph.

Extracts:
- Module nodes (from settings.gradle module listing + individual gradle files)
- DEPENDS_ON relationships (from project(':path') declarations with configuration type)

Handles both Groovy DSL (.gradle) and Kotlin DSL (.gradle.kts).
"""

import re
import logging
from pathlib import Path

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Regex patterns for Gradle dependency declarations
# Groovy DSL: implementation project(':appian-libraries:records:records-api')
# Kotlin DSL: implementation(project(":appian-libraries:records:records-api"))
GROOVY_PROJECT_DEP = re.compile(
    r"(\w+)\s+project\(\s*['\"](:[\w:.-]+)['\"]\s*\)"
)
KOTLIN_PROJECT_DEP = re.compile(
    r"(\w+)\(\s*project\(\s*['\"](:[\w:.-]+)['\"]\s*\)\s*\)"
)

# Configurations we care about (maps to dependency scope)
DEPENDENCY_CONFIGS = {
    # Standard Gradle
    "implementation", "api", "compileOnly", "runtimeOnly",
    # SAIL
    "appianApp",
    # Standard test
    "testImplementation", "testRuntimeOnly", "testCompileOnly",
    "testFixturesImplementation",
    # Tiered test configs (Appian-specific)
    "integrationTestImplementation", "integrationTestRuntimeOnly",
    "unitTestImplementation", "unitTestRuntimeOnly",
    "sharedTestImplementation", "sharedTestRuntimeOnly",
    "systemTestImplementation", "systemTestRuntimeOnly", "systemTestCompileOnly",
    # Custom Appian configurations
    "appianLib",
    "osgiFrameworkBundles",
    "gwtar",
    "transpileSources",
    "portalBundledApps",
    "jmh",
    "tomcatLib",
    "connectedSystemsPlugins",
    "miscPluginImplementation",
    "gwtInputs",
}


class GradleExtractor(BaseExtractor):
    """Extracts Gradle module dependency graph."""

    @property
    def name(self) -> str:
        return "Gradle Extractor"

    def extract(self):
        modules = self._discover_modules()
        dependencies = self._extract_dependencies(modules)

        # Create module nodes
        module_nodes = []
        for gradle_path, info in modules.items():
            module_nodes.append({
                "gradlePath": gradle_path,
                "name": info["name"],
                "path": info["path"],
                "language": info.get("language", "java"),
            })

        self.client.batch_create_nodes("Module", module_nodes, merge_key="gradlePath")
        logger.info(f"  Discovered {len(module_nodes)} modules")

        # Create DEPENDS_ON relationships
        dep_rels = []
        skipped = 0
        for dep in dependencies:
            # Only create edge if both modules exist
            if dep["from_gradle_path"] in modules and dep["to_gradle_path"] in modules:
                dep_rels.append({
                    "from_id": dep["from_gradle_path"],
                    "to_id": dep["to_gradle_path"],
                    "config": dep["config"],
                    "sourceFile": dep["source_file"],
                    "sourceLine": dep["source_line"],
                })
            else:
                skipped += 1

        self.client.batch_create_relationships(
            "DEPENDS_ON",
            dep_rels,
            from_label="Module",
            to_label="Module",
            from_key="gradlePath",
            to_key="gradlePath",
        )
        logger.info(f"  Created {len(dep_rels)} DEPENDS_ON relationships (skipped {skipped} unresolved)")

    def _discover_modules(self) -> dict[str, dict]:
        """
        Discover all modules from settings.gradle and filesystem structure.
        Returns: {gradlePath: {name, path, language}}
        """
        modules = {}

        # Parse settings.gradle for explicit includes
        settings_file = self.repo_root / "settings.gradle"
        if settings_file.exists():
            content = settings_file.read_text()
            # Match: include 'path:to:module' or include "path:to:module"
            for match in re.finditer(r"include\s+['\"](:[\w:.-]+)['\"]", content):
                gradle_path = match.group(1)
                modules[gradle_path] = self._module_info_from_gradle_path(gradle_path)

        # Also discover modules by finding .gradle files across the ENTIRE repo
        # (settings.gradle uses eachDir which we can't easily parse dynamically)
        scan_dirs = [
            self.repo_root / "appian-libraries",
            self.repo_root / "appian-services",
            self.repo_root / "test",
            self.repo_root / "server",
            self.repo_root / "infra",
            self.repo_root / "deployment",
            self.repo_root / "javadocs",
            self.repo_root / "log-collection",
        ]
        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue
            for gradle_file in scan_dir.rglob("*.gradle"):
                # Skip build-logic, node_modules, etc.
                if any(part.startswith(".") or part in ("node_modules", "build", "buildSrc") for part in gradle_file.parts):
                    continue

                # Derive gradle path from file location
                module_dir = gradle_file.parent
                try:
                    rel = module_dir.relative_to(self.repo_root)
                    gradle_path = ":" + str(rel).replace("/", ":")
                    if gradle_path not in modules:
                        modules[gradle_path] = self._module_info_from_path(module_dir, gradle_path)
                except ValueError:
                    continue

            # Also check for .gradle.kts files
            for gradle_file in scan_dir.rglob("*.gradle.kts"):
                if any(part.startswith(".") or part in ("node_modules", "build", "buildSrc") for part in gradle_file.parts):
                    continue
                module_dir = gradle_file.parent
                try:
                    rel = module_dir.relative_to(self.repo_root)
                    gradle_path = ":" + str(rel).replace("/", ":")
                    if gradle_path not in modules:
                        modules[gradle_path] = self._module_info_from_path(module_dir, gradle_path)
                except ValueError:
                    continue

        return modules

    def _module_info_from_gradle_path(self, gradle_path: str) -> dict:
        """Convert a Gradle path like :appian-libraries:records:records-java to module info."""
        parts = gradle_path.lstrip(":").split(":")
        name = parts[-1]
        fs_path = "/".join(parts)
        language = self._detect_language(self.repo_root / fs_path)
        return {"name": name, "path": fs_path, "language": language}

    def _module_info_from_path(self, module_dir: Path, gradle_path: str) -> dict:
        """Create module info from filesystem path."""
        name = module_dir.name
        rel_path = str(module_dir.relative_to(self.repo_root))
        language = self._detect_language(module_dir)
        return {"name": name, "path": rel_path, "language": language}

    def _detect_language(self, module_dir: Path) -> str:
        """Detect primary language of a module based on its contents."""
        if (module_dir / "src" / "appianApp").exists():
            return "sail"
        if (module_dir / "package.json").exists():
            return "typescript"
        if (module_dir / "src" / "main" / "java").exists():
            return "java"
        if any(module_dir.glob("*.k")):
            return "k"
        return "mixed"

    def _extract_dependencies(self, modules: dict) -> list[dict]:
        """
        Scan all .gradle and .gradle.kts files for project() dependency declarations.
        Returns list of {from_gradle_path, to_gradle_path, config, source_file, source_line}.
        """
        dependencies = []

        # Find all gradle files in the repo
        gradle_files = list(self.repo_root.rglob("*.gradle"))
        gradle_files += list(self.repo_root.rglob("*.gradle.kts"))

        # Filter out irrelevant files
        gradle_files = [
            f for f in gradle_files
            if not any(part.startswith(".") or part in ("node_modules", "build") for part in f.parts)
        ]

        for gradle_file in gradle_files:
            # Determine which module this gradle file belongs to
            module_dir = gradle_file.parent
            try:
                rel = module_dir.relative_to(self.repo_root)
                from_gradle_path = ":" + str(rel).replace("/", ":")
            except ValueError:
                continue

            # Skip if this module isn't in our discovered set (e.g., buildSrc)
            if from_gradle_path not in modules:
                continue

            # Parse the file for dependencies
            rel_file = self.relative_path(gradle_file)
            try:
                content = gradle_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for line_num, line in enumerate(content.splitlines(), start=1):
                # Skip comments
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                    continue

                # Try Groovy pattern
                for match in GROOVY_PROJECT_DEP.finditer(line):
                    config = match.group(1)
                    to_path = match.group(2)
                    if config in DEPENDENCY_CONFIGS:
                        dependencies.append({
                            "from_gradle_path": from_gradle_path,
                            "to_gradle_path": to_path,
                            "config": config,
                            "source_file": rel_file,
                            "source_line": line_num,
                        })

                # Try Kotlin pattern
                for match in KOTLIN_PROJECT_DEP.finditer(line):
                    config = match.group(1)
                    to_path = match.group(2)
                    if config in DEPENDENCY_CONFIGS:
                        dependencies.append({
                            "from_gradle_path": from_gradle_path,
                            "to_gradle_path": to_path,
                            "config": config,
                            "source_file": rel_file,
                            "source_line": line_num,
                        })

        return dependencies
