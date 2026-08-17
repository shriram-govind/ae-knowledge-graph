"""
Web XML Extractor — Extracts servlet mappings, URI templates, and TLD tag library bindings.

Extracts:
- Servlet nodes (name, urlPattern, class)
- SERVLET_HANDLES relationships (Servlet → JavaClass)
- TLD tag → JavaClass mappings (TLD_IMPLEMENTS)
- URI template → handler bindings
"""

import re
import logging
from pathlib import Path

from lxml import etree

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)


class WebXmlExtractor(BaseExtractor):
    """Extracts servlet mappings, URI templates, and TLD tag bindings."""

    @property
    def name(self) -> str:
        return "Web XML Extractor"

    def extract(self):
        self._extract_web_xml()
        self._extract_tld_files()
        self._extract_uri_templates()

    def _extract_web_xml(self):
        """Parse web.xml for servlet-class → url-pattern mappings."""
        web_xml = self.repo_root / "deployment" / "web.war" / "WEB-INF" / "web.xml"
        if not web_xml.exists():
            logger.info("  [web.xml] Not found, skipping")
            return

        try:
            tree = etree.parse(str(web_xml), etree.XMLParser(recover=True))
            root = tree.getroot()
        except Exception as e:
            logger.warning(f"  [web.xml] Failed to parse: {e}")
            return

        # Extract namespace (web.xml uses Jakarta or javax namespace)
        ns = {}
        if root.tag.startswith("{"):
            ns_uri = root.tag.split("}")[0].lstrip("{")
            ns = {"w": ns_uri}

        # Find servlet definitions: servlet-name → servlet-class
        servlets = {}
        for servlet in root.iter():
            try:
                tag = servlet.tag if isinstance(servlet.tag, str) else ""
            except (AttributeError, TypeError):
                continue
            if tag.endswith("servlet") and not tag.endswith("servlet-mapping"):
                name_elem = None
                class_elem = None
                for child in servlet:
                    try:
                        child_tag = child.tag if isinstance(child.tag, str) else ""
                    except (AttributeError, TypeError):
                        continue
                    if child_tag.endswith("servlet-name"):
                        name_elem = child
                    elif child_tag.endswith("servlet-class"):
                        class_elem = child
                if name_elem is not None and class_elem is not None and name_elem.text and class_elem.text:
                    servlets[name_elem.text.strip()] = class_elem.text.strip()

        # Find servlet mappings: servlet-name → url-pattern
        mappings = {}
        for mapping in root.iter():
            try:
                tag = mapping.tag if isinstance(mapping.tag, str) else ""
            except (AttributeError, TypeError):
                continue
            if tag.endswith("servlet-mapping"):
                name_elem = None
                pattern_elem = None
                for child in mapping:
                    try:
                        child_tag = child.tag if isinstance(child.tag, str) else ""
                    except (AttributeError, TypeError):
                        continue
                    if child_tag.endswith("servlet-name"):
                        name_elem = child
                    elif child_tag.endswith("url-pattern"):
                        pattern_elem = child
                if name_elem is not None and pattern_elem is not None and name_elem.text and pattern_elem.text:
                    name = name_elem.text.strip()
                    pattern = pattern_elem.text.strip()
                    if name not in mappings:
                        mappings[name] = []
                    mappings[name].append(pattern)

        # Create edges: JavaClass handles URL patterns
        edges = []
        known_fqns = set()
        results = self.client.run_query("MATCH (c:JavaClass) RETURN c.fqn AS fqn LIMIT 60000")
        known_fqns = {r["fqn"] for r in results}

        for servlet_name, servlet_class in servlets.items():
            if servlet_class in known_fqns:
                url_patterns = mappings.get(servlet_name, [])
                for pattern in url_patterns:
                    edges.append({
                        "from_id": servlet_class,
                        "to_id": pattern,
                    })

        logger.info(f"  [web.xml] Found {len(servlets)} servlets, {len(mappings)} URL mappings")

        # Store URL patterns as properties on the JavaClass nodes
        for servlet_name, servlet_class in servlets.items():
            if servlet_class in known_fqns:
                patterns = mappings.get(servlet_name, [])
                if patterns:
                    self.client.run_query(
                        "MATCH (c:JavaClass {fqn: $fqn}) SET c.servletUrlPatterns = $patterns",
                        fqn=servlet_class,
                        patterns=",".join(patterns),
                    )

        logger.info(f"  [web.xml] Annotated {len([s for s in servlets.values() if s in known_fqns])} servlet classes with URL patterns")

    def _extract_tld_files(self):
        """Parse TLD files for tag-class definitions → links to Java classes."""
        tld_files = list((self.repo_root / "deployment" / "web.war" / "WEB-INF").glob("*.tld"))
        logger.info(f"  [TLD] Found {len(tld_files)} TLD files")

        known_fqns = set()
        results = self.client.run_query("MATCH (c:JavaClass) RETURN c.fqn AS fqn LIMIT 60000")
        known_fqns = {r["fqn"] for r in results}

        tld_edges = []
        for tld_file in tld_files:
            try:
                tree = etree.parse(str(tld_file), etree.XMLParser(recover=True))
                root = tree.getroot()
            except Exception:
                continue

            # Find <tag-class> elements
            for elem in root.iter():
                try:
                    tag = elem.tag if isinstance(elem.tag, str) else ""
                except (AttributeError, TypeError):
                    continue
                if tag.endswith("tag-class") or tag.endswith("tei-class"):
                    if elem.text:
                        class_fqn = elem.text.strip()
                        if class_fqn in known_fqns:
                            tld_edges.append({
                                "tld_file": self.relative_path(tld_file),
                                "class_fqn": class_fqn,
                            })

        # Store TLD implementation info on Java classes
        for edge in tld_edges:
            self.client.run_query(
                "MATCH (c:JavaClass {fqn: $fqn}) SET c.isTldTag = true, c.tldFile = $tldFile",
                fqn=edge["class_fqn"],
                tldFile=edge["tld_file"],
            )

        logger.info(f"  [TLD] Annotated {len(tld_edges)} Java classes as TLD tag implementations")

    def _extract_uri_templates(self):
        """Parse URI template XML files for URL pattern → handler mappings."""
        uri_template_files = list(self.repo_root.rglob("*uri-template*xml"))
        uri_template_files = [f for f in uri_template_files if "node_modules" not in str(f) and "/build/" not in str(f)]
        logger.info(f"  [URI Templates] Found {len(uri_template_files)} URI template files")

        templates_found = 0
        for uri_file in uri_template_files:
            try:
                tree = etree.parse(str(uri_file), etree.XMLParser(recover=True))
                root = tree.getroot()
            except Exception:
                continue

            # Count templates in this file
            for elem in root.iter():
                try:
                    tag = elem.tag if isinstance(elem.tag, str) else ""
                except (AttributeError, TypeError):
                    continue
                if tag.endswith("template") or elem.get("pattern"):
                    templates_found += 1

        logger.info(f"  [URI Templates] Found {templates_found} URI template definitions")
