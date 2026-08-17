"""
Feature Toggle Extractor — Maps feature toggles to code that references them.

Extracts:
- FeatureToggle nodes (name, defaultValue)
- GATES relationships (toggle → JavaClass/SailRule that checks it)
- DEFINED_IN relationships (toggle → JavaClass that defines the constant)

Uses exact string matching only. A toggle "ae.feature.x" creates a GATES edge
only where the EXACT string "ae.feature.x" appears in a isFeatureEnabled() call.
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Patterns for toggle usage in Java
JAVA_TOGGLE_PATTERNS = [
    re.compile(r'isFeatureEnabled\(\s*"([^"]+)"'),
    re.compile(r'getBoolean\(\s*"([^"]+)"'),
    re.compile(r'getBooleanFromModernClientWithFallback\(\s*"([^"]+)"'),
]

# Pattern for toggle constant definitions in Java
JAVA_TOGGLE_CONSTANT = re.compile(
    r'(?:static\s+final\s+String|final\s+static\s+String)\s+(\w+)\s*=\s*"(ae\.[^"]+)"'
)

# Pattern for toggle usage in SAIL
SAIL_TOGGLE_PATTERN = re.compile(r'a!isFeatureEnabled\(\s*"([^"]+)"')


class ToggleExtractor(BaseExtractor):
    """Extracts feature toggle definitions and usage across the codebase."""

    @property
    def name(self) -> str:
        return "Feature Toggle Extractor"

    def extract(self):
        # Parse feature-toggles.properties for toggle definitions
        toggles = self._parse_toggle_properties()
        logger.info(f"  Found {len(toggles)} feature toggle definitions from properties file")

        # Scan Java files for toggle references (this also discovers additional toggle names via constants)
        java_gates, java_definitions, discovered_toggles = self._scan_java_for_toggles(toggles)
        logger.info(f"  Found {len(java_gates)} Java toggle references, {len(java_definitions)} constant definitions")
        logger.info(f"  Discovered {len(discovered_toggles)} additional toggles from constants (not in properties)")

        # Merge: create nodes for ALL known toggles (properties + discovered)
        all_toggles = dict(toggles)
        for name in discovered_toggles:
            if name not in all_toggles:
                all_toggles[name] = "unknown"

        # Create FeatureToggle nodes for ALL toggles
        toggle_nodes = [{"name": name, "defaultValue": value} for name, value in all_toggles.items()]
        if toggle_nodes:
            self.client.batch_create_nodes("FeatureToggle", toggle_nodes, merge_key="name")
        logger.info(f"  Created {len(toggle_nodes)} FeatureToggle nodes (properties + discovered)")

        # Scan SAIL files for toggle references (using the full toggle set)
        sail_gates = self._scan_sail_for_toggles(all_toggles)
        logger.info(f"  Found {len(sail_gates)} SAIL toggle references")

        # Create GATES edges (toggle → Java class)
        if java_gates:
            self.client.batch_create_relationships(
                "GATES", java_gates,
                from_label="FeatureToggle", to_label="JavaClass",
                from_key="name", to_key="fqn",
            )
        logger.info(f"  Created {len(java_gates)} GATES→JavaClass edges")

        # Create GATES edges (toggle → SAIL rule)
        if sail_gates:
            self.client.batch_create_relationships(
                "GATES", sail_gates,
                from_label="FeatureToggle", to_label="SailRule",
                from_key="name", to_key="uuid",
            )
        logger.info(f"  Created {len(sail_gates)} GATES→SailRule edges")

        # Create DEFINED_IN edges
        if java_definitions:
            self.client.batch_create_relationships(
                "DEFINED_IN", java_definitions,
                from_label="FeatureToggle", to_label="JavaClass",
                from_key="name", to_key="fqn",
            )
        logger.info(f"  Created {len(java_definitions)} DEFINED_IN edges")

    def _parse_toggle_properties(self) -> dict[str, str]:
        """Parse feature-toggles.properties file."""
        toggles = {}
        props_file = self.repo_root / "infra" / "feature-toggles.properties"

        if not props_file.exists():
            return toggles

        for line in props_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                toggles[key.strip()] = value.strip()

        return toggles

    def _scan_java_for_toggles(self, toggles: dict) -> tuple[list[dict], list[dict], set[str]]:
        """Scan Java files for toggle usage and definitions. Two-pass for constant resolution."""
        gates = []
        definitions = []
        known_toggle_names = set(toggles.keys())
        discovered_toggles = set()  # Toggle names found via constants but not in properties

        # PASS 1: Collect ALL toggle constant definitions across the entire codebase
        # Maps: constant_name → toggle_value (e.g., "MFA_AUTHENTICATOR_APP_TOGGLE_KEY" → "ae.pev.mfa-authenticator-app")
        global_constant_map: dict[str, str] = {}

        # Also track which file defines each constant
        constant_defining_files: dict[str, list[str]] = {}  # constant_name → [file_fqns]

        for java_file in self.repo_root.rglob("*.java"):
            rel = str(java_file.relative_to(self.repo_root))
            if "/build/" in rel or "node_modules/" in rel:
                continue
            if "/src/" not in rel:
                continue

            try:
                content = java_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Find constant definitions: static final String X = "ae...."
            for match in JAVA_TOGGLE_CONSTANT.finditer(content):
                const_name = match.group(1)
                toggle_name = match.group(2)
                global_constant_map[const_name] = toggle_name

                fqn = self._path_to_fqn(rel)
                if fqn and toggle_name in known_toggle_names:
                    definitions.append({
                        "from_id": toggle_name,
                        "to_id": fqn,
                        "constantName": const_name,
                        "sourceFile": rel,
                    })
                    if const_name not in constant_defining_files:
                        constant_defining_files[const_name] = []
                    constant_defining_files[const_name].append(fqn)

                # Track toggles discovered from constants (even if not in properties)
                if toggle_name.startswith("ae.") and toggle_name not in known_toggle_names:
                    discovered_toggles.add(toggle_name)
                    # Also add definition edge for discovered toggles
                    if fqn:
                        definitions.append({
                            "from_id": toggle_name,
                            "to_id": fqn,
                            "constantName": const_name,
                            "sourceFile": rel,
                        })

        logger.info(f"  Pass 1: Found {len(global_constant_map)} toggle constant definitions")

        # PASS 2: Find ALL isFeatureEnabled() calls — resolve both direct strings AND constants
        # Patterns: isFeatureEnabled(CONSTANT_NAME) or isFeatureEnabled(ClassName.CONSTANT_NAME)
        JAVA_TOGGLE_CONSTANT_REF = re.compile(
            r'isFeatureEnabled\(\s*(?:[\w.]+\.)?([A-Z_][A-Z0-9_]*)\s*\)'
        )

        # Combined set: all known toggles (from properties AND discovered from constants)
        all_toggle_names = known_toggle_names | discovered_toggles

        for java_file in self.repo_root.rglob("*.java"):
            rel = str(java_file.relative_to(self.repo_root))
            if "/build/" in rel or "node_modules/" in rel:
                continue
            if "/src/" not in rel:
                continue

            try:
                content = java_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            fqn = self._path_to_fqn(rel)
            if not fqn:
                continue

            # Direct string references: isFeatureEnabled("ae.toggle.key")
            for pattern in JAVA_TOGGLE_PATTERNS:
                for match in pattern.finditer(content):
                    toggle_name = match.group(1)
                    if toggle_name in all_toggle_names:
                        gates.append({
                            "from_id": toggle_name,
                            "to_id": fqn,
                            "sourceFile": rel,
                        })

            # Constant references: isFeatureEnabled(CONSTANT_NAME)
            for match in JAVA_TOGGLE_CONSTANT_REF.finditer(content):
                const_name = match.group(1)
                # Resolve constant to actual toggle name
                if const_name in global_constant_map:
                    toggle_name = global_constant_map[const_name]
                    if toggle_name in all_toggle_names:
                        gates.append({
                            "from_id": toggle_name,
                            "to_id": fqn,
                            "sourceFile": rel,
                        })

        # Deduplicate gates
        seen = set()
        unique_gates = []
        for gate in gates:
            key = (gate["from_id"], gate["to_id"])
            if key not in seen:
                seen.add(key)
                unique_gates.append(gate)

        # Deduplicate definitions
        seen = set()
        unique_defs = []
        for d in definitions:
            key = (d["from_id"], d["to_id"])
            if key not in seen:
                seen.add(key)
                unique_defs.append(d)

        return unique_gates, unique_defs, discovered_toggles

    def _scan_sail_for_toggles(self, toggles: dict) -> list[dict]:
        """Scan SAIL XML files for toggle usage."""
        gates = []
        known_toggle_names = set(toggles.keys())

        for xml_file in self.repo_root.rglob("SYSTEM_SYSRULES_*.xml"):
            if "FOLDER" in xml_file.name:
                continue
            rel = str(xml_file.relative_to(self.repo_root))
            if "/test/" in rel:
                continue

            try:
                content = xml_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Derive UUID from filename
            uuid = xml_file.stem  # SYSTEM_SYSRULES_ruleName → uuid

            for match in SAIL_TOGGLE_PATTERN.finditer(content):
                toggle_name = match.group(1)
                if toggle_name in known_toggle_names:
                    gates.append({
                        "from_id": toggle_name,
                        "to_id": uuid,
                        "sourceFile": rel,
                    })

        # Deduplicate
        seen = set()
        unique_gates = []
        for gate in gates:
            key = (gate["from_id"], gate["to_id"])
            if key not in seen:
                seen.add(key)
                unique_gates.append(gate)

        return unique_gates

    def _path_to_fqn(self, rel_path: str) -> str | None:
        """Convert a Java file path to its FQN."""
        # Handle various source set patterns - find the /java/ marker and take everything after
        # Patterns: src/main/java/, src/test/unit/java/, src/test/integration/java/, etc.
        match = re.search(r'/java/(.+)$', rel_path)
        if match:
            java_path = match.group(1)
            if java_path.endswith(".java"):
                java_path = java_path[:-5]
            return java_path.replace("/", ".")
        return None
