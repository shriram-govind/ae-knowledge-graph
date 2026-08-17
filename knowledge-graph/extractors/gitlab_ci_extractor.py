"""
GitLab CI Extractor — Maps CI pipeline definitions to the modules/paths that trigger them.

Extracts:
- CiPipeline nodes (name, filePath, triggerPatterns)
- CI_TRIGGERED_BY relationships (CiPipeline → Module, based on changes: glob patterns)

Parses .gitlab-ci*.yaml files and per-module .gitlab-ci.yml files for job definitions
and their `rules:` / `changes:` trigger patterns.
"""

import re
import logging
from pathlib import Path

import yaml

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)


class GitlabCiExtractor(BaseExtractor):
    """Extracts GitLab CI pipeline definitions and trigger mappings."""

    @property
    def name(self) -> str:
        return "GitLab CI Extractor"

    def extract(self):
        # Find all GitLab CI YAML files
        ci_files = self._find_ci_files()
        logger.info(f"  Found {len(ci_files)} GitLab CI YAML files")

        # Load module map for matching trigger patterns
        module_map = {}
        results = self.client.run_query("MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath")
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        # Parse each CI file
        pipeline_nodes: list[dict] = []
        trigger_edges: list[dict] = []

        for ci_file in ci_files:
            result = self._parse_ci_file(ci_file, module_map)
            if result:
                pipeline_nodes.extend(result["pipelines"])
                trigger_edges.extend(result["triggers"])

        # Create CiPipeline nodes
        if pipeline_nodes:
            self.client.batch_create_nodes("CiPipeline", pipeline_nodes, merge_key="name")
            logger.info(f"  Created {len(pipeline_nodes)} CiPipeline nodes")

        # Create CI_TRIGGERED_BY edges
        # Deduplicate
        seen = set()
        unique_triggers = []
        for e in trigger_edges:
            key = (e["from_id"], e["to_id"])
            if key not in seen:
                seen.add(key)
                unique_triggers.append(e)

        if unique_triggers:
            self.client.batch_create_relationships(
                "CI_TRIGGERED_BY", unique_triggers,
                from_label="CiPipeline", to_label="Module",
                from_key="name", to_key="gradlePath",
            )
        logger.info(f"  Created {len(unique_triggers)} CI_TRIGGERED_BY edges")

    def _find_ci_files(self) -> list[Path]:
        """Find all GitLab CI YAML files."""
        ci_files = []
        # Root level
        for f in self.repo_root.glob(".gitlab-ci*.yaml"):
            ci_files.append(f)
        for f in self.repo_root.glob(".gitlab-ci*.yml"):
            ci_files.append(f)
        # Per-module .gitlab-ci.yml
        for f in self.repo_root.rglob(".gitlab-ci.yml"):
            if "node_modules" not in str(f) and "/build/" not in str(f):
                ci_files.append(f)
        # infra/gitlab-ci/ templates
        gitlab_ci_dir = self.repo_root / "infra" / "gitlab-ci"
        if gitlab_ci_dir.exists():
            for f in gitlab_ci_dir.rglob("*.yml"):
                ci_files.append(f)
            for f in gitlab_ci_dir.rglob("*.yaml"):
                ci_files.append(f)
        return ci_files

    def _parse_ci_file(self, ci_file: Path, module_map: dict) -> dict | None:
        """Parse a GitLab CI YAML file for job definitions and trigger patterns."""
        try:
            content = ci_file.read_text(encoding="utf-8", errors="ignore")
            data = yaml.safe_load(content)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        rel_path = self.relative_path(ci_file)
        pipelines = []
        triggers = []

        for key, value in data.items():
            # Skip special GitLab CI keys
            if key.startswith(".") or key in ("include", "variables", "default", "stages", "workflow"):
                continue

            if not isinstance(value, dict):
                continue

            # This looks like a job definition
            job_name = key
            trigger_patterns = []

            # Extract changes: patterns from rules
            rules = value.get("rules", [])
            if isinstance(rules, list):
                for rule in rules:
                    if isinstance(rule, dict):
                        changes = rule.get("changes", [])
                        if isinstance(changes, list):
                            trigger_patterns.extend(changes)
                        elif isinstance(changes, dict) and "paths" in changes:
                            trigger_patterns.extend(changes["paths"])

            # Also check top-level only/changes (older syntax)
            only = value.get("only", {})
            if isinstance(only, dict) and "changes" in only:
                trigger_patterns.extend(only["changes"])

            if trigger_patterns or value.get("script"):
                pipelines.append({
                    "name": job_name,
                    "filePath": rel_path,
                    "triggerPatterns": ",".join(trigger_patterns[:20]),  # Store first 20
                })

                # Match trigger patterns to modules
                for pattern in trigger_patterns:
                    matched_modules = self._match_pattern_to_modules(pattern, module_map)
                    for module_gradle_path in matched_modules:
                        triggers.append({
                            "from_id": job_name,
                            "to_id": module_gradle_path,
                            "pattern": pattern,
                        })

        return {"pipelines": pipelines, "triggers": triggers}

    def _match_pattern_to_modules(self, pattern: str, module_map: dict) -> list[str]:
        """Match a CI trigger glob pattern to module paths."""
        matched = []
        # Normalize pattern: remove leading **/ and trailing /**
        clean_pattern = pattern.strip()

        for module_path, gradle_path in module_map.items():
            # Simple prefix matching (covers most cases)
            if clean_pattern.startswith(module_path):
                matched.append(gradle_path)
            elif module_path.startswith(clean_pattern.rstrip("/**/*")):
                matched.append(gradle_path)
            # Also match if pattern contains the module path
            elif module_path in clean_pattern:
                matched.append(gradle_path)

        return matched
