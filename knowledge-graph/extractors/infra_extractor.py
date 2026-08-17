"""
Infrastructure Extractor — Covers JSP, Docker Compose, CODEOWNERS, GitLab CI, OpenAPI.

Extracts:
- JspPage nodes + JSP_INCLUDES relationships
- DockerService nodes + SERVICE_DEPENDS_ON relationships
- Team nodes + OWNS relationships
- CiPipeline nodes + CI_TRIGGERED_BY relationships
- ApiEndpoint nodes + IMPLEMENTED_BY relationships
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

import yaml

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# JSP include patterns
JSP_INCLUDE_DIRECTIVE = re.compile(r'<%@\s*include\s+file\s*=\s*"([^"]+)"', re.IGNORECASE)
JSP_INCLUDE_TAG = re.compile(r'<jsp:include\s+page\s*=\s*"([^"]+)"', re.IGNORECASE)
JSP_SCRIPT_SRC = re.compile(r'<script[^>]+src\s*=\s*"([^"]+\.js)"', re.IGNORECASE)


class InfraExtractor(BaseExtractor):
    """Extracts infrastructure relationships: JSP, Docker, CODEOWNERS, CI, API."""

    @property
    def name(self) -> str:
        return "Infrastructure Extractor"

    def extract(self):
        self._extract_jsp()
        self._extract_docker()
        self._extract_codeowners()
        self._extract_openapi()

    # ===== JSP =====

    def _extract_jsp(self):
        """Extract JSP files and include relationships."""
        jsp_files = list(self.repo_root.rglob("*.jsp"))
        jsp_files = [f for f in jsp_files if "node_modules" not in str(f) and "/build/" not in str(f)]
        logger.info(f"  [JSP] Found {len(jsp_files)} JSP files")

        # Create nodes
        jsp_nodes = []
        for f in jsp_files:
            rel = self.relative_path(f)
            jsp_nodes.append({
                "path": rel,
                "name": f.name,
                "isTest": "/test/" in rel,
            })

        if jsp_nodes:
            self.client.batch_create_nodes("JspPage", jsp_nodes, merge_key="path")

        # Extract include relationships
        known_paths = {n["path"] for n in jsp_nodes}
        include_edges = []

        for jsp_file in jsp_files:
            rel_path = self.relative_path(jsp_file)
            try:
                content = jsp_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Find all includes
            includes = []
            includes.extend(JSP_INCLUDE_DIRECTIVE.findall(content))
            includes.extend(JSP_INCLUDE_TAG.findall(content))

            for inc_path in includes:
                # Resolve relative to the JSP file or to web root
                resolved = self._resolve_jsp_path(inc_path, jsp_file)
                if resolved and resolved in known_paths:
                    include_edges.append({
                        "from_id": rel_path,
                        "to_id": resolved,
                        "sourceFile": rel_path,
                    })

        # Deduplicate
        seen = set()
        unique_includes = []
        for e in include_edges:
            key = (e["from_id"], e["to_id"])
            if key not in seen:
                seen.add(key)
                unique_includes.append(e)

        if unique_includes:
            self.client.batch_create_relationships(
                "JSP_INCLUDES", unique_includes,
                from_label="JspPage", to_label="JspPage",
                from_key="path", to_key="path",
            )
        logger.info(f"  [JSP] Created {len(jsp_nodes)} nodes, {len(unique_includes)} JSP_INCLUDES edges")

    def _resolve_jsp_path(self, inc_path: str, from_file: Path) -> str | None:
        """Resolve a JSP include path."""
        if inc_path.startswith("/"):
            # Absolute from web root
            candidate = self.repo_root / "deployment" / "web.war" / inc_path.lstrip("/")
        else:
            # Relative to current file
            candidate = from_file.parent / inc_path

        candidate = candidate.resolve()
        if candidate.exists():
            try:
                return str(candidate.relative_to(self.repo_root))
            except ValueError:
                pass
        return None

    # ===== Docker Compose =====

    def _extract_docker(self):
        """Extract Docker Compose service definitions."""
        compose_files = list(self.repo_root.glob("docker-compose*.yml"))
        compose_files += list(self.repo_root.glob("docker-compose*.yaml"))
        logger.info(f"  [Docker] Found {len(compose_files)} Docker Compose files")

        services: dict[str, dict] = {}
        depends_on_edges: list[dict] = []

        for compose_file in compose_files:
            try:
                data = yaml.safe_load(compose_file.read_text())
            except Exception:
                continue

            if not data or "services" not in data:
                continue

            rel_path = self.relative_path(compose_file)

            for svc_name, svc_config in data["services"].items():
                if not isinstance(svc_config, dict):
                    continue

                services[svc_name] = {
                    "name": svc_name,
                    "composeFile": rel_path,
                    "image": svc_config.get("image", ""),
                }

                # depends_on
                deps = svc_config.get("depends_on", [])
                if isinstance(deps, list):
                    for dep in deps:
                        depends_on_edges.append({
                            "from_id": svc_name,
                            "to_id": dep,
                            "sourceFile": rel_path,
                        })
                elif isinstance(deps, dict):
                    for dep in deps.keys():
                        depends_on_edges.append({
                            "from_id": svc_name,
                            "to_id": dep,
                            "sourceFile": rel_path,
                        })

        # Create DockerService nodes
        service_nodes = list(services.values())
        if service_nodes:
            self.client.batch_create_nodes("DockerService", service_nodes, merge_key="name")

        # Create SERVICE_DEPENDS_ON edges
        known_services = set(services.keys())
        valid_deps = [e for e in depends_on_edges if e["to_id"] in known_services]
        if valid_deps:
            self.client.batch_create_relationships(
                "SERVICE_DEPENDS_ON", valid_deps,
                from_label="DockerService", to_label="DockerService",
                from_key="name", to_key="name",
            )
        logger.info(f"  [Docker] Created {len(service_nodes)} services, {len(valid_deps)} SERVICE_DEPENDS_ON edges")

    # ===== CODEOWNERS =====

    def _extract_codeowners(self):
        """Extract team ownership from CODEOWNERS."""
        codeowners_file = self.repo_root / ".github" / "CODEOWNERS"
        if not codeowners_file.exists():
            logger.info("  [CODEOWNERS] File not found, skipping")
            return

        content = codeowners_file.read_text()
        teams: dict[str, dict] = {}
        ownership_rules: list[tuple[str, str]] = []  # (pattern, team_handle)

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) >= 2:
                pattern = parts[0]
                for owner in parts[1:]:
                    if owner.startswith("@"):
                        team_name = owner.split("/")[-1] if "/" in owner else owner.lstrip("@")
                        teams[owner] = {
                            "name": team_name,
                            "handle": owner,
                        }
                        ownership_rules.append((pattern, owner))

        # Create Team nodes
        team_nodes = list(teams.values())
        if team_nodes:
            self.client.batch_create_nodes("Team", team_nodes, merge_key="handle")

        # Match patterns to modules
        module_map = {}
        results = self.client.run_query("MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath")
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        owns_edges = []
        for pattern, team_handle in ownership_rules:
            # Simple pattern matching (not full glob, but covers most cases)
            clean_pattern = pattern.rstrip("/").lstrip("/")
            for module_path, gradle_path in module_map.items():
                if self._path_matches_pattern(module_path, clean_pattern):
                    owns_edges.append({
                        "from_id": team_handle,
                        "to_id": gradle_path,
                        "pattern": pattern,
                    })

        # Deduplicate (one team may own a module via multiple patterns)
        seen = set()
        unique_owns = []
        for e in owns_edges:
            key = (e["from_id"], e["to_id"])
            if key not in seen:
                seen.add(key)
                unique_owns.append(e)

        if unique_owns:
            self.client.batch_create_relationships(
                "OWNS", unique_owns,
                from_label="Team", to_label="Module",
                from_key="handle", to_key="gradlePath",
            )
        logger.info(f"  [CODEOWNERS] Created {len(team_nodes)} teams, {len(unique_owns)} OWNS edges")

    def _path_matches_pattern(self, path: str, pattern: str) -> bool:
        """Simple CODEOWNERS pattern matching."""
        # Handle ** wildcard
        if pattern.startswith("**/"):
            suffix = pattern[3:]
            return suffix in path
        # Handle * glob at end
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return path.startswith(prefix)
        # Handle directory patterns
        if pattern.endswith("/"):
            return path.startswith(pattern.rstrip("/"))
        # Exact or prefix match
        return path.startswith(pattern) or path == pattern

    # ===== OpenAPI =====

    def _extract_openapi(self):
        """Extract API endpoints from OpenAPI specs."""
        api_dir = self.repo_root / "api-specs"
        if not api_dir.exists():
            logger.info("  [OpenAPI] api-specs directory not found, skipping")
            return

        yaml_files = list(api_dir.glob("*.yaml"))
        logger.info(f"  [OpenAPI] Found {len(yaml_files)} OpenAPI spec files")

        endpoints: list[dict] = []

        for yaml_file in yaml_files:
            try:
                data = yaml.safe_load(yaml_file.read_text())
            except Exception:
                continue

            if not data or "paths" not in data:
                continue

            rel_path = self.relative_path(yaml_file)

            for path, methods in data["paths"].items():
                if not isinstance(methods, dict):
                    continue
                for method in ("get", "post", "put", "delete", "patch"):
                    if method in methods:
                        endpoint_id = f"{method.upper()} {path}"
                        endpoints.append({
                            "path": endpoint_id,
                            "method": method.upper(),
                            "urlPath": path,
                            "specFile": rel_path,
                            "operationId": methods[method].get("operationId", "") if isinstance(methods[method], dict) else "",
                        })

        if endpoints:
            self.client.batch_create_nodes("ApiEndpoint", endpoints, merge_key="path")
        logger.info(f"  [OpenAPI] Created {len(endpoints)} ApiEndpoint nodes")
