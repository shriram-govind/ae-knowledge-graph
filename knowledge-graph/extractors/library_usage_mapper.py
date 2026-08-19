"""
Library Usage Mapper — Maps Java import statements to external libraries.

This extractor creates:
- USES_LIBRARY relationships (JavaClass → ExternalLibrary) based on import package prefixes
- MODULE_USES_LIBRARY relationships (Module → ExternalLibrary) aggregated from class-level usage

The proof: if a class imports `com.fasterxml.jackson.databind.ObjectMapper`, it provably
uses the `com.fasterxml.jackson.core:jackson-databind` library. The package prefix IS the evidence.

Also computes:
- Usage counts per library (how heavily used across the codebase)
- Per-module usage (which modules would be affected by a library upgrade)
"""

import os
import re
import logging
import zipfile
from pathlib import Path
from collections import defaultdict

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Known mapping: package prefix → library coordinate (group:artifact)
# This is the static, provable mapping between Java package namespaces and Maven coordinates.
# Built from: common Java libraries + what's in the ae lockfile.
PACKAGE_TO_LIBRARY = {
    # Spring Framework
    "org.springframework.context": "org.springframework:spring-context",
    "org.springframework.beans": "org.springframework:spring-beans",
    "org.springframework.core": "org.springframework:spring-core",
    "org.springframework.web": "org.springframework:spring-web",
    "org.springframework.util": "org.springframework:spring-core",
    "org.springframework.transaction": "org.springframework:spring-tx",
    "org.springframework.jdbc": "org.springframework:spring-jdbc",
    "org.springframework.aop": "org.springframework:spring-aop",
    "org.springframework.security": "org.springframework.security:spring-security-core",
    "org.springframework.ldap": "org.springframework.ldap:spring-ldap-core",
    "org.springframework.retry": "org.springframework.retry:spring-retry",
    # Jackson
    "com.fasterxml.jackson.databind": "com.fasterxml.jackson.core:jackson-databind",
    "com.fasterxml.jackson.core": "com.fasterxml.jackson.core:jackson-core",
    "com.fasterxml.jackson.annotation": "com.fasterxml.jackson.core:jackson-annotations",
    "com.fasterxml.jackson.datatype": "com.fasterxml.jackson.datatype:jackson-datatype-jsr310",
    "com.fasterxml.jackson.dataformat": "com.fasterxml.jackson.dataformat:jackson-dataformat-yaml",
    # Google
    "com.google.common": "com.google.guava:guava",
    "com.google.gson": "com.google.code.gson:gson",
    "com.google.protobuf": "com.google.protobuf:protobuf-java",
    # Apache Commons
    "org.apache.commons.lang3": "org.apache.commons:commons-lang3",
    "org.apache.commons.lang": "commons-lang:commons-lang",
    "org.apache.commons.collections4": "org.apache.commons:commons-collections4",
    "org.apache.commons.io": "commons-io:commons-io",
    "org.apache.commons.codec": "commons-codec:commons-codec",
    "org.apache.commons.text": "org.apache.commons:commons-text",
    "org.apache.commons.csv": "org.apache.commons:commons-csv",
    "org.apache.commons.pool2": "org.apache.commons:commons-pool2",
    # Logging
    "org.slf4j": "org.slf4j:slf4j-api",
    "org.apache.log4j": "ch.qos.reload4j:reload4j",
    "org.apache.logging.log4j": "org.apache.logging.log4j:log4j-core",
    "ch.qos.logback": "ch.qos.logback:logback-classic",
    # HTTP
    "org.apache.http": "org.apache.httpcomponents:httpclient",
    "org.apache.hc.client5": "org.apache.httpcomponents.client5:httpclient5",
    "org.apache.hc.core5": "org.apache.httpcomponents.core5:httpcore5",
    "okhttp3": "com.squareup.okhttp3:okhttp",
    # Jetty
    "org.eclipse.jetty": "org.eclipse.jetty:jetty-server",
    "org.eclipse.jetty.http": "org.eclipse.jetty:jetty-http",
    "org.eclipse.jetty.server": "org.eclipse.jetty:jetty-server",
    "org.eclipse.jetty.servlet": "org.eclipse.jetty:jetty-servlet",
    "org.eclipse.jetty.util": "org.eclipse.jetty:jetty-util",
    "org.eclipse.jetty.io": "org.eclipse.jetty:jetty-io",
    "org.eclipse.jetty.websocket": "org.eclipse.jetty.websocket:websocket-api",
    # Database/Persistence
    "org.hibernate": "org.hibernate:hibernate-core-jakarta",
    "jakarta.persistence": "jakarta.persistence:jakarta.persistence-api",
    "javax.persistence": "javax.persistence:javax.persistence-api",
    # AWS
    "com.amazonaws": "com.amazonaws:aws-java-sdk-core",
    "software.amazon.awssdk": "software.amazon.awssdk:aws-core",
    # Kafka
    "org.apache.kafka": "org.apache.kafka:kafka-clients",
    # Quartz
    "org.quartz": "org.quartz-scheduler:quartz",
    # POI (Excel)
    "org.apache.poi": "org.apache.poi:poi",
    # Prometheus
    "io.prometheus": "io.prometheus:simpleclient",
    "io.micrometer": "io.micrometer:micrometer-core",
    # JSON/XML
    "org.json": "org.json:json",
    "org.w3c.dom": "xml-apis:xml-apis",
    "org.xml.sax": "xml-apis:xml-apis",
    "javax.xml": "xml-apis:xml-apis",
    # Jakarta
    "jakarta.ws.rs": "jakarta.ws.rs:jakarta.ws.rs-api",
    "jakarta.servlet": "jakarta.servlet:jakarta.servlet-api",
    "jakarta.inject": "jakarta.inject:jakarta.inject-api",
    "jakarta.annotation": "jakarta.annotation:jakarta.annotation-api",
    # javax (legacy)
    "javax.inject": "javax.inject:javax.inject",
    "javax.servlet": "javax.servlet:javax.servlet-api",
    "javax.ws.rs": "javax.ws.rs:javax.ws.rs-api",
    # Testing
    "org.junit.jupiter": "org.junit.jupiter:junit-jupiter-api",
    "org.junit": "junit:junit",
    "org.mockito": "org.mockito:mockito-core",
    "org.hamcrest": "org.hamcrest:hamcrest",
    "org.assertj": "org.assertj:assertj-core",
    # Misc
    "io.opentelemetry": "io.opentelemetry:opentelemetry-api",
    "com.cognitect.transit": "com.cognitect:transit-java",
    "org.eclipse.emf": "org.eclipse.emf:org.eclipse.emf.ecore",
    "edu.umd.cs.findbugs": "com.github.spotbugs:spotbugs-annotations",
    "org.jsoup": "org.jsoup:jsoup",
    "com.auth0.jwt": "com.auth0:java-jwt",
    "io.jsonwebtoken": "io.jsonwebtoken:jjwt-api",
    "org.bouncycastle": "org.bouncycastle:bcprov-jdk18on",
    "com.nimbusds": "com.nimbusds:nimbus-jose-jwt",
    "org.yaml.snakeyaml": "org.yaml:snakeyaml",
    "redis.clients.jedis": "redis.clients:jedis",
    # Netty
    "io.netty": "io.netty:netty-common",
    "io.netty.channel": "io.netty:netty-transport",
    "io.netty.handler": "io.netty:netty-handler",
    "io.netty.buffer": "io.netty:netty-buffer",
    # Elasticsearch
    "co.elastic.clients": "co.elastic.clients:elasticsearch-java",
    "org.elasticsearch": "org.elasticsearch.client:elasticsearch-rest-client",
    # Redis
    "org.redisson": "org.redisson:redisson",
    "io.lettuce": "io.lettuce:lettuce-core",
    # gRPC
    "io.grpc": "io.grpc:grpc-core",
    # Caffeine cache
    "com.github.benmanes.caffeine": "com.github.ben-manes.caffeine:caffeine",
    # Apache Arrow (parquet)
    "org.apache.arrow": "org.apache.arrow:arrow-vector",
    "org.apache.parquet": "org.apache.parquet:parquet-common",
    # Atlassian
    "com.atlassian.plugins": "com.atlassian.plugins:atlassian-plugins-api",
    "com.atlassian.seraph": "com.atlassian.seraph:atlassian-seraph",
    # Felix (OSGi)
    "org.apache.felix": "org.apache.felix:org.apache.felix.framework",
    "org.osgi": "org.osgi:org.osgi.core",
}


class LibraryUsageMapper(BaseExtractor):
    """Maps Java class imports to external libraries and creates usage relationships."""

    # Bump whenever the JAR scanning / conflict-resolution logic changes so that
    # previously written caches (produced by older, buggier logic) are discarded.
    #   v2: deterministic version selection + zipfile + package validation
    #   v3: union packages across ALL cached versions of an artifact
    SCANNER_VERSION = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_roots: list[Path] = []
        self._scan_diagnostics: dict = {}
        self._unresolved_imports: dict[str, int] = defaultdict(int)

    @property
    def name(self) -> str:
        return "Library Usage Mapper"

    def extract(self):
        # Get all Java class imports from the graph
        logger.info("  Loading Java class import data from graph...")
        class_imports = self._load_class_imports()
        logger.info(f"  Loaded imports for {len(class_imports)} classes")

        # Load known FQNs for class-level edge validation
        results = self.client.run_query("MATCH (c:JavaClass) RETURN c.fqn AS fqn")
        self._known_fqns = {r["fqn"] for r in results}

        # Load known library coordinates
        results = self.client.run_query("MATCH (lib:ExternalLibrary) RETURN lib.coordinate AS coord")
        known_lib_coords = {r["coord"] for r in results}

        # Map imports to libraries
        class_to_libs: dict[str, set[str]] = {}  # class FQN → set of library coordinates
        module_to_libs: dict[str, set[str]] = defaultdict(set)  # module gradlePath → set of libs

        # Get class → module mapping
        class_modules = self._load_class_modules()

        for class_fqn, imports in class_imports.items():
            libs_used = set()
            for imp in imports:
                lib = self._import_to_library(imp)
                if lib:
                    libs_used.add(lib)

            if libs_used:
                class_to_libs[class_fqn] = libs_used
                # Aggregate to module level
                module = class_modules.get(class_fqn)
                if module:
                    module_to_libs[module].update(libs_used)

        logger.info(f"  {len(class_to_libs)} classes use external libraries")
        logger.info(f"  {len(module_to_libs)} modules use external libraries")

        # Create MODULE_USES_LIBRARY edges (for high-level impact analysis)
        module_lib_edges = []
        for module_path, libs in module_to_libs.items():
            for lib_coord in libs:
                module_lib_edges.append({
                    "from_id": module_path,
                    "to_id": lib_coord,
                })

        if module_lib_edges:
            self.client.batch_create_relationships(
                "USES_LIBRARY", module_lib_edges,
                from_label="Module", to_label="ExternalLibrary",
                from_key="gradlePath", to_key="coordinate",
            )
        logger.info(f"  Created {len(module_lib_edges)} Module-[:USES_LIBRARY]->ExternalLibrary edges")

        # Create CLASS-LEVEL USES_LIBRARY edges (for granular impact analysis)
        class_lib_edges = []
        for class_fqn, libs in class_to_libs.items():
            if class_fqn in self._known_fqns:
                for lib_coord in libs:
                    if lib_coord in known_lib_coords:
                        class_lib_edges.append({
                            "from_id": class_fqn,
                            "to_id": lib_coord,
                        })

        if class_lib_edges:
            self.client.batch_create_relationships(
                "USES_LIBRARY", class_lib_edges,
                from_label="JavaClass", to_label="ExternalLibrary",
                from_key="fqn", to_key="coordinate",
            )
        logger.info(f"  Created {len(class_lib_edges)} JavaClass-[:USES_LIBRARY]->ExternalLibrary edges")

        # Compute and log library usage stats
        lib_usage_count = defaultdict(int)
        for libs in module_to_libs.values():
            for lib in libs:
                lib_usage_count[lib] += 1

        # Store usage count on ExternalLibrary nodes
        for lib_coord, count in lib_usage_count.items():
            self.client.run_query(
                "MATCH (lib:ExternalLibrary {coordinate: $coord}) SET lib.moduleUsageCount = $count",
                coord=lib_coord,
                count=count,
            )

        top_libs = sorted(lib_usage_count.items(), key=lambda x: x[1], reverse=True)[:15]
        logger.info("  Top 15 most-used external libraries (by module count):")
        for lib, count in top_libs:
            logger.info(f"    {lib}: {count} modules")

        self._report_mapping_coverage()

    def _report_mapping_coverage(self):
        """
        Emit a verification report for the package→library mapping.

        This exists so that a misconfigured or empty Gradle cache is loud rather
        than silent: without it, a wrong cache path simply yields far fewer
        USES_LIBRARY edges and nothing in the log says why.
        """
        diag = self._scan_diagnostics
        logger.info("  " + "-" * 58)
        logger.info("  Package→library mapping verification")
        logger.info("  " + "-" * 58)

        if not self._cache_roots:
            logger.warning(
                "    Gradle artifact cache: NOT FOUND — mapping used the "
                f"{len(PACKAGE_TO_LIBRARY)} hand-maintained entries only."
            )
        else:
            for root in self._cache_roots:
                logger.info(f"    Gradle artifact cache: {root}")

        mapping_size = len(getattr(self, "_dynamic_package_map", {}) or {})
        logger.info(f"    JAR-derived package→library entries: {mapping_size:,}")
        logger.info(f"    Hand-maintained fallback entries:    {len(PACKAGE_TO_LIBRARY):,}")

        if diag:
            total = diag.get("coordinates_total", 0)
            resolved = diag.get("coordinates_resolved", 0)
            pct = (resolved / total * 100) if total else 0.0
            logger.info(
                f"    Coordinates resolved to a cached JAR:  {resolved:,}/{total:,} ({pct:.1f}%)"
            )
            logger.info(f"    JARs read (all cached versions):        {diag.get('jars_scanned', 0):,}")
            logger.info(f"    Not present in Gradle cache:           {diag.get('coordinates_not_in_cache', 0):,}")
            logger.info(f"    Unreadable JARs:                       {diag.get('jars_unreadable', 0):,}")
            logger.info(f"    Packages claimed by >1 coordinate:     {diag.get('packages_contested', 0):,}")
        else:
            logger.info("    (mapping loaded from cache — rerun after deleting "
                        "data/package-to-library-cache.json for a full scan report)")

        if self._unresolved_imports:
            unresolved_total = sum(self._unresolved_imports.values())
            distinct = len(self._unresolved_imports)
            logger.info(
                f"    Third-party imports with NO library match: {unresolved_total:,} "
                f"across {distinct:,} namespaces"
            )
            top = sorted(self._unresolved_imports.items(), key=lambda kv: kv[1], reverse=True)[:10]
            for ns, cnt in top:
                logger.info(f"      {ns}.* : {cnt:,} imports unmapped")
        else:
            logger.info("    Third-party imports with NO library match: 0")
        logger.info("  " + "-" * 58)

    def _load_class_imports(self) -> dict[str, list[str]]:
        """Load all IMPORTS relationships to get import targets per class."""
        # We need the target FQNs that are NOT internal classes (external library imports)
        # These are imports that don't resolve to a JavaClass node in our graph
        results = self.client.run_query("""
            MATCH (c:JavaClass)
            WHERE c.path IS NOT NULL
            RETURN c.fqn AS fqn, c.path AS path
        """)
        known_fqns = {r["fqn"] for r in results}

        # Re-read Java files to get ALL imports (including external ones that didn't become IMPORTS edges)
        class_imports = defaultdict(list)

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

            # Extract package (to derive FQN)
            package_match = re.search(r'^package\s+([\w.]+)\s*;', content, re.MULTILINE)
            if not package_match:
                continue
            package = package_match.group(1)

            # Extract class name from file name
            class_name = java_file.stem
            class_fqn = f"{package}.{class_name}"

            # Extract imports
            imports = re.findall(r'^import\s+([\w.]+)\s*;', content, re.MULTILINE)
            # Filter to only external imports (not in our known set)
            external_imports = [imp for imp in imports if imp not in known_fqns]
            if external_imports:
                class_imports[class_fqn] = external_imports

        return dict(class_imports)

    def _load_class_modules(self) -> dict[str, str]:
        """Load class FQN → module gradlePath mapping from graph."""
        results = self.client.run_query("""
            MATCH (m:Module)-[:CONTAINS]->(:Package)-[:CONTAINS]->(c:JavaClass)
            RETURN c.fqn AS fqn, m.gradlePath AS module
        """)
        return {r["fqn"]: r["module"] for r in results}

    # Namespaces that are part of the JDK or of this repo — a miss here is expected
    # and must not be counted against library-mapping coverage.
    _NON_LIBRARY_PREFIXES = (
        "java.", "javax.swing.", "javax.tools.", "javax.lang.", "javax.management.",
        "javax.naming.", "javax.crypto.", "javax.security.", "javax.net.",
        "javax.imageio.", "javax.sound.", "javax.print.", "javax.script.",
        "javax.sql.", "javax.accessibility.", "javax.rmi.", "javax.transaction.xa.",
        "jdk.", "sun.", "com.sun.", "org.w3c.", "org.xml.sax", "org.ietf.",
        "com.appiancorp.", "com.appian.", "appian.",
    )

    def _import_to_library(self, import_fqn: str) -> str | None:
        """
        Map an import FQN to its external library coordinate.

        Resolution order:
          1. Gradle-cache JAR scan (authoritative — the coordinate that actually
             ships the package), longest package prefix wins.
          2. Hand-maintained PACKAGE_TO_LIBRARY, longest prefix wins.

        Unmapped third-party imports are counted for the coverage report.
        """
        # Build dynamic mapping on first call (scans the Gradle artifact cache)
        if not hasattr(self, "_dynamic_package_map"):
            self._dynamic_package_map = self._build_jar_scanned_mappings()

        # Try dynamic JAR-scanned mapping (longest prefix match)
        parts = import_fqn.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self._dynamic_package_map:
                return self._dynamic_package_map[prefix]

        # Fallback: manual mappings (for edge cases not in the Gradle cache)
        best_match = None
        best_length = 0
        for prefix, library in PACKAGE_TO_LIBRARY.items():
            if import_fqn.startswith(prefix) and len(prefix) > best_length:
                best_match = library
                best_length = len(prefix)

        if best_match is None and not import_fqn.startswith(self._NON_LIBRARY_PREFIXES):
            # Record at package granularity to keep the report readable.
            self._unresolved_imports[".".join(parts[:3])] += 1

        return best_match

    # ------------------------------------------------------------------
    # Gradle cache discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_gradle_cache_roots() -> list[Path]:
        """
        Locate every Gradle module cache root on this machine, in priority order.

        The canonical layout Gradle uses for resolved artifacts is:

            <GRADLE_USER_HOME>/caches/modules-2/files-2.1/
                <group>/<artifact>/<version>/<sha1>/<artifact>-<version>[-<classifier>].jar

        `files-2.1` is the artifact store; `metadata-2.x` alongside it holds only
        descriptors, not JARs. We therefore anchor on `modules-2/files-2.1`.

        Candidate Gradle homes, highest priority first:
          1. $GRADLE_USER_HOME             (explicit override — wins if set)
          2. ~/.gradle                     (default)
          3. $GRADLE_HOME/caches           (rare, but some CI images use it)

        Returns only roots that actually exist on disk.
        """
        candidates: list[Path] = []

        env_home = os.environ.get("GRADLE_USER_HOME")
        if env_home:
            candidates.append(Path(env_home).expanduser())

        candidates.append(Path.home() / ".gradle")

        gradle_home = os.environ.get("GRADLE_HOME")
        if gradle_home:
            candidates.append(Path(gradle_home).expanduser())

        roots: list[Path] = []
        seen: set[Path] = set()
        for base in candidates:
            root = base / "caches" / "modules-2" / "files-2.1"
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if root.is_dir():
                roots.append(root)

        return roots

    @staticmethod
    def _version_sort_key(version: str) -> tuple:
        """
        Build a deterministic, semver-aware sort key for a Gradle cache version dir.

        `Path.iterdir()` returns entries in arbitrary filesystem order, so picking
        `version_dirs[-1]` is non-deterministic: on this machine guava resolves to
        33.3.0-jre even though 33.6.0-jre is cached. That silently produces a
        different package set from run to run. Sorting fixes that.

        Ordering rules:
          - numeric components compare numerically (so 33.10 > 33.9)
          - a plain release sorts above any qualified build of the same number,
            EXCEPT that `-jre` outranks `-android` (JVM code wants the -jre variant)
          - vendor rebuilds (e.g. `1.7.32.redhat-00001`) sort below upstream
        """
        # -jre / -android are Guava-style variant flavours, not pre-release markers.
        flavour = 0
        v = version
        if v.endswith("-jre"):
            flavour, v = 2, v[: -len("-jre")]
        elif v.endswith("-android"):
            flavour, v = 1, v[: -len("-android")]

        # Split off any remaining qualifier (-SNAPSHOT, -RC1, .redhat-00001, ...)
        qualifier = ""
        for sep in ("-", "_"):
            if sep in v:
                v, qualifier = v.split(sep, 1)
                break

        numeric: list[int] = []
        for part in v.split("."):
            if part.isdigit():
                numeric.append(int(part))
            else:
                # Non-numeric tail — treat as qualifier and stop numeric parsing.
                qualifier = f"{part}.{qualifier}" if qualifier else part
                break

        # Pad so shorter versions compare correctly (1.7 vs 1.7.36)
        numeric += [0] * (6 - len(numeric[:6]))

        # No qualifier ⇒ final release ⇒ ranks above pre-release/vendor builds.
        is_release = 1 if not qualifier else 0

        return (tuple(numeric[:6]), is_release, flavour, qualifier, version)

    def _resolve_artifact_jars(self, group: str, artifact: str, want_version: str | None):
        """
        Resolve every cached main JAR for a group:artifact, best version first.

        Returns a list of (jar_path, version, is_preferred).

        Scanning ALL cached versions — not just one — is deliberate. This mapping
        answers "which library owns this package", which is a version-independent
        question. Picking a single version made the result depend on which version
        happened to be cached, and for artifacts versioned by opaque git hashes
        (e.g. com.appian.mining:pm-query-lib has 2aeea0d1 / 33838893 / 89c6b3ea /
        f90f3c71, whose package sets differ) there is no meaningful "latest" at all.
        Unioning the packages makes the mapping stable and complete; the preferred
        version is still flagged so conflict resolution can favour it.

        Ordering:
          1. the exact version the build resolved (from the lockfile / graph)
          2. remaining versions, highest first by semver-aware sort
        """
        out: list[tuple[Path, str, bool]] = []
        for cache_root in self._cache_roots:
            jar_base = cache_root / group / artifact
            if not jar_base.is_dir():
                continue
            try:
                version_dirs = [d for d in jar_base.iterdir() if d.is_dir()]
            except OSError:
                continue
            if not version_dirs:
                continue

            exact_dir = None
            if want_version and want_version not in ("managed", "unknown", ""):
                candidate = jar_base / want_version
                if candidate.is_dir():
                    exact_dir = candidate

            ordered: list[Path] = []
            if exact_dir is not None:
                ordered.append(exact_dir)
            for d in sorted(version_dirs, key=lambda p: self._version_sort_key(p.name), reverse=True):
                if d not in ordered:
                    ordered.append(d)

            for idx, version_dir in enumerate(ordered):
                jar = self._pick_main_jar(version_dir, artifact)
                if jar is None:
                    continue
                # Preferred = the exact resolved version if we found one, else the
                # highest-sorting version.
                is_preferred = (version_dir is exact_dir) if exact_dir is not None else (idx == 0)
                out.append((jar, version_dir.name, is_preferred))
        return out

    @staticmethod
    def _pick_main_jar(version_dir: Path, artifact: str) -> Path | None:
        """
        Choose the primary code JAR inside a `<version>/` dir.

        Gradle nests each file under its own sha1 dir, so we rglob. A version dir
        can legitimately contain several JARs (main + classifiers); we must not
        pick arbitrarily, or the package→coordinate mapping becomes unstable.
        """
        try:
            jars = [f for f in version_dir.rglob("*.jar") if f.is_file()]
        except OSError:
            return None
        if not jars:
            return None

        # Classifiers that never carry the library's real production classes.
        EXCLUDED = ("-sources.jar", "-javadoc.jar", "-tests.jar",
                    "-test-sources.jar", "-test-fixtures.jar")
        candidates = [j for j in jars if not j.name.endswith(EXCLUDED)]
        if not candidates:
            return None

        preferred_prefix = f"{artifact}-"

        def rank(j: Path) -> tuple:
            stem = j.name[: -len(".jar")]
            # Unclassified main artifact: name is exactly "<artifact>-<version>"
            classifier = ""
            if stem.startswith(preferred_prefix):
                remainder = stem[len(preferred_prefix):]
                # Version may itself contain '-', so treat "looks like a classifier"
                # as an alphabetic trailing segment.
                bits = remainder.rsplit("-", 1)
                if len(bits) == 2 and bits[1] and not bits[1][0].isdigit():
                    classifier = bits[1]
            has_classifier = 1 if classifier else 0
            return (has_classifier, len(j.name), j.name)  # sort ascending

        candidates.sort(key=rank)
        return candidates[0]

    # ------------------------------------------------------------------
    # Package → library mapping
    # ------------------------------------------------------------------

    def _build_jar_scanned_mappings(self) -> dict[str, str]:
        """
        Scan Gradle cache JARs to build a complete package→library mapping.

        Results are cached to data/package-to-library-cache.json and regenerated
        when the lockfile changes, the cache roots change, or the scanner version
        changes (so a scanner fix invalidates stale output).
        """
        import json

        cache_file = Path(__file__).parent.parent / "data" / "package-to-library-cache.json"
        lockfile = self._find_lockfile()

        self._cache_roots = self._discover_gradle_cache_roots()

        cache_signature = self._cache_signature(lockfile)

        # ---- try cache
        if cache_file.exists():
            try:
                cache_data = json.loads(cache_file.read_text())
                meta = cache_data.pop("_meta", None)
                # Legacy caches stored only "_lockfile_hash" and were produced by the
                # old non-deterministic scanner — treat those as invalid.
                cache_data.pop("_lockfile_hash", None)
                if meta and meta.get("signature") == cache_signature and len(cache_data) > 100:
                    logger.info(
                        f"  Loaded {len(cache_data):,} package→library mappings from cache "
                        f"(scanner v{meta.get('scanner_version')})"
                    )
                    return cache_data
                logger.info("  Package→library cache is stale or from an older scanner — rebuilding")
            except Exception as e:
                logger.info(f"  Package→library cache unreadable ({e}) — rebuilding")

        # ---- verify we actually found the cache before scanning
        if not self._cache_roots:
            searched = [
                os.environ.get("GRADLE_USER_HOME") or "(GRADLE_USER_HOME unset)",
                str(Path.home() / ".gradle"),
                os.environ.get("GRADLE_HOME") or "(GRADLE_HOME unset)",
            ]
            logger.error(
                "  Gradle artifact cache NOT FOUND. Expected "
                "<gradle-home>/caches/modules-2/files-2.1/. Searched: %s. "
                "Falling back to the %d hand-maintained PACKAGE_TO_LIBRARY entries only — "
                "USES_LIBRARY coverage will be badly degraded. "
                "Run a Gradle build to populate the cache, or set GRADLE_USER_HOME.",
                searched, len(PACKAGE_TO_LIBRARY),
            )
            return {}

        for root in self._cache_roots:
            try:
                groups = sum(1 for _ in root.iterdir())
            except OSError:
                groups = -1
            logger.info(f"  Gradle artifact cache: {root} ({groups} group dirs)")

        logger.info("  Building package→library map from Gradle cache JARs (result will be cached)...")

        # Get all known library coordinates from the graph
        results = self.client.run_query(
            "MATCH (lib:ExternalLibrary) RETURN lib.coordinate AS coord, lib.latestVersion AS version"
        )
        # Sort for deterministic scan order — this matters because a package present
        # in more than one JAR is resolved by the tie-break below, and stable input
        # order keeps the output reproducible.
        known_libs = sorted(
            ((r["coord"], r["version"]) for r in results if r["coord"] and ":" in r["coord"]),
            key=lambda kv: kv[0],
        )
        logger.info(f"  {len(known_libs):,} ExternalLibrary coordinates to resolve")

        # pkg -> list of (coordinate, from_preferred_version)
        owners: dict[str, list[tuple[str, bool]]] = defaultdict(list)
        resolved_coords = 0
        jars_scanned = 0
        not_in_cache: list[str] = []
        unreadable = 0
        no_exact_version = 0

        for coord, want_version in known_libs:
            group, artifact = coord.split(":", 1)

            jars = self._resolve_artifact_jars(group, artifact, want_version)
            if not jars:
                not_in_cache.append(coord)
                continue
            resolved_coords += 1
            if not any(preferred for _j, _v, preferred in jars):
                no_exact_version += 1
            elif not (want_version and want_version not in ("managed", "unknown", "")):
                no_exact_version += 1

            for jar_path, _version, is_preferred in jars:
                packages = self._packages_in_jar(jar_path)
                if packages is None:
                    unreadable += 1
                    continue
                jars_scanned += 1
                for pkg in packages:
                    owners[pkg].append((coord, is_preferred))

        # ---- deterministic conflict resolution
        # A package can appear in several JARs (shaded/fat jars, split packages,
        # relocated bundles). The old code took whichever JAR it happened to scan
        # first, which depended on Neo4j result ordering and filesystem ordering.
        # Instead: prefer a package seen in the resolved version, then the coordinate
        # whose group is the longest prefix of the package (the natural owner), then
        # the shortest coordinate, then alphabetical.
        package_map: dict[str, str] = {}
        contested = 0
        for pkg, cands in owners.items():
            distinct = {c for c, _ in cands}
            if len(distinct) == 1:
                package_map[pkg] = next(iter(distinct))
                continue
            contested += 1

            # Collapse to one entry per coordinate, remembering whether it was ever
            # seen in that coordinate's preferred version.
            best_per_coord: dict[str, bool] = {}
            for c, pref in cands:
                best_per_coord[c] = best_per_coord.get(c, False) or pref

            def owner_rank(item: tuple[str, bool]) -> tuple:
                coord, from_preferred = item
                group = coord.split(":", 1)[0]
                # How well does the library's group id match the package namespace?
                if pkg == group or pkg.startswith(group + "."):
                    group_affinity = len(group)
                else:
                    group_affinity = 0
                return (
                    0 if from_preferred else 1,  # package present in resolved version
                    -group_affinity,             # natural namespace owner
                    len(coord),                  # prefer the plainer artifact
                    coord,                       # final deterministic tie-break
                )

            package_map[pkg] = min(best_per_coord.items(), key=owner_rank)[0]

        logger.info(
            f"  JAR scan complete: {resolved_coords:,}/{len(known_libs):,} coordinates resolved, "
            f"{jars_scanned:,} JARs read (all cached versions), "
            f"{len(not_in_cache):,} not in cache, {unreadable:,} unreadable"
        )
        logger.info(
            f"  {len(package_map):,} package→library mappings "
            f"({contested:,} packages were claimed by >1 coordinate and resolved deterministically)"
        )
        if not_in_cache:
            sample = ", ".join(not_in_cache[:8])
            logger.info(f"  Not in Gradle cache (sample): {sample}")

        self._scan_diagnostics = {
            "cache_roots": [str(r) for r in self._cache_roots],
            "coordinates_total": len(known_libs),
            "coordinates_resolved": resolved_coords,
            "jars_scanned": jars_scanned,
            "coordinates_not_in_cache": len(not_in_cache),
            "jars_unreadable": unreadable,
            "no_exact_version": no_exact_version,
            "packages_mapped": len(package_map),
            "packages_contested": contested,
        }

        # ---- save cache
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(package_map)
            payload["_meta"] = {
                "signature": cache_signature,
                "scanner_version": self.SCANNER_VERSION,
                "cache_roots": [str(r) for r in self._cache_roots],
                "lockfile": str(lockfile) if lockfile else None,
                "diagnostics": self._scan_diagnostics,
            }
            cache_file.write_text(json.dumps(payload))
            logger.info(f"  Cached mappings to {cache_file}")
        except Exception as e:
            logger.warning(f"  Failed to write cache: {e}")

        return package_map

    @staticmethod
    def _packages_in_jar(jar_path: Path) -> set[str] | None:
        """
        Return the set of packages that contain at least one .class entry.

        Uses zipfile rather than shelling out to `jar tf`: no dependency on a JDK
        being on PATH, no per-JAR process spawn (the old approach cost ~90s), and
        no 10s timeout that silently dropped large JARs.
        """
        try:
            with zipfile.ZipFile(jar_path) as zf:
                packages: set[str] = set()
                for name in zf.namelist():
                    if not name.endswith(".class") or "/" not in name:
                        continue
                    if name.startswith("META-INF/"):
                        # Multi-release jars: META-INF/versions/<n>/<real/path>.class
                        parts = name.split("/", 3)
                        if len(parts) == 4 and parts[1] == "versions":
                            name = parts[3]
                            if "/" not in name:
                                continue
                        else:
                            continue
                    pkg = name.rsplit("/", 1)[0].replace("/", ".")
                    # Skip synthetic/invalid package names
                    if not pkg or any(not seg.isidentifier() for seg in pkg.split(".")):
                        continue
                    packages.add(pkg)
                return packages
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return None

    def _find_lockfile(self) -> Path | None:
        """Locate the Gradle dependency lockfile used for cache invalidation."""
        candidate = self.repo_root / "deployment" / "gradle.lockfile"
        if candidate.exists():
            return candidate
        # Fall back to any lockfile in the repo (first by sorted path, deterministic)
        for found in sorted(self.repo_root.glob("**/gradle.lockfile")):
            if "/build/" not in str(found):
                return found
        return None

    def _cache_signature(self, lockfile: Path | None) -> str:
        """
        Signature that must change whenever the mapping could change:
        lockfile identity + which cache roots exist + scanner version.
        """
        lock_part = self._file_hash(lockfile) if lockfile and lockfile.exists() else "no-lockfile"
        roots_part = "|".join(sorted(str(r) for r in self._cache_roots)) or "no-cache-roots"
        return f"v{self.SCANNER_VERSION}::{lock_part}::{roots_part}"

    @staticmethod
    def _file_hash(filepath: Path) -> str:
        """Quick hash of a file for change detection (uses size + mtime)."""
        stat = filepath.stat()
        return f"{stat.st_size}_{int(stat.st_mtime)}"
