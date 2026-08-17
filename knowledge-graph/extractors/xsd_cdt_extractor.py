"""
XSD/CDT Schema Extractor — Maps Custom Data Type definitions from XSD to their usage in SAIL and Java.

Extracts:
- CdtType nodes (name, namespace, isHidden, path)
- CdtType → SailRule REFERENCED_BY relationships (from SAIL 'type!{ns}Name' references)
- CdtType field definitions as metadata

Parses the main system-record-types.xsd and other XSD files for type definitions.
"""

import re
import logging
from pathlib import Path

from lxml import etree

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

XS_NS = "http://www.w3.org/2001/XMLSchema"
APPIAN_NS = "http://www.appian.com/ae/types/2009"


class XsdCdtExtractor(BaseExtractor):
    """Extracts CDT type definitions from XSD files."""

    @property
    def name(self) -> str:
        return "XSD/CDT Schema Extractor"

    def extract(self):
        # Find the main CDT schema file
        main_xsd = self.repo_root / "appian-libraries" / "appian-system-cdt" / "src" / "resources" / "appian" / "cdt" / "system-record-types.xsd"

        if not main_xsd.exists():
            logger.warning(f"  Main XSD not found: {main_xsd}")
            return

        # Parse XSD for type definitions
        cdt_types = self._parse_xsd(main_xsd)
        logger.info(f"  Found {len(cdt_types)} CDT type definitions in system-record-types.xsd")

        # Create CdtType nodes
        if cdt_types:
            self.client.batch_create_nodes("CdtType", cdt_types, merge_key="name")
            logger.info(f"  Created {len(cdt_types)} CdtType nodes")

        # Link existing SAIL REFERENCES_CDT edges (already extracted by SAIL extractor via regex)
        # The SAIL extractor found 'type!{ns}Name' patterns — let's create proper edges
        # from SailRules to CdtType nodes
        known_types = {t["name"] for t in cdt_types}
        self._link_sail_references(known_types)

    def _parse_xsd(self, xsd_file: Path) -> list[dict]:
        """Parse XSD for complexType and simpleType definitions."""
        try:
            tree = etree.parse(str(xsd_file), etree.XMLParser(recover=True, huge_tree=True))
            root = tree.getroot()
        except Exception as e:
            logger.warning(f"  Failed to parse XSD: {e}")
            return []

        ns = {"xs": XS_NS, "a": APPIAN_NS}
        rel_path = self.relative_path(xsd_file)
        types = []

        # Find all complexType definitions
        for complex_type in root.findall(f".//{{{XS_NS}}}complexType[@name]"):
            type_name = complex_type.get("name")
            if not type_name:
                continue

            # Check for HIDDEN flag
            is_hidden = False
            appinfo = complex_type.find(f".//{{{XS_NS}}}appinfo")
            if appinfo is not None and appinfo.text and "HIDDEN" in (appinfo.text or ""):
                is_hidden = True

            # Count fields
            elements = complex_type.findall(f".//{{{XS_NS}}}element")
            field_count = len(elements)

            types.append({
                "name": type_name,
                "namespace": APPIAN_NS,
                "isHidden": is_hidden,
                "kind": "complexType",
                "fieldCount": field_count,
                "path": rel_path,
            })

        # Find all simpleType definitions (enums)
        for simple_type in root.findall(f".//{{{XS_NS}}}simpleType[@name]"):
            type_name = simple_type.get("name")
            if not type_name:
                continue

            # Count enum values
            enumerations = simple_type.findall(f".//{{{XS_NS}}}enumeration")
            enum_count = len(enumerations)

            types.append({
                "name": type_name,
                "namespace": APPIAN_NS,
                "isHidden": False,
                "kind": "simpleType",
                "fieldCount": enum_count,
                "path": rel_path,
            })

        return types

    def _link_sail_references(self, known_types: set):
        """Create REFERENCES_CDT edges from SailRules that use 'type!{ns}TypeName' to CdtType nodes."""
        # The SAIL extractor already found CDT references in rule definitions
        # We need to query SAIL rules and match their type references to our CdtType nodes
        # The SAIL extractor stored CDT refs in the definition parsing — let's scan for them

        results = self.client.run_query("""
            MATCH (r:SailRule)
            WHERE r._fnCalls IS NOT NULL
            RETURN r.uuid AS uuid, r.path AS path
            LIMIT 1
        """)

        # Instead of re-parsing all SAIL files, let's create edges based on a name scan
        # For efficiency, scan SAIL files for type!{...}TypeName patterns
        cdt_ref_pattern = re.compile(r"'type!\{[^}]+\}(\w+)'")

        edges = []
        for sail_file in self.repo_root.rglob("SYSTEM_SYSRULES_*.xml"):
            if "FOLDER" in sail_file.name:
                continue
            rel = str(sail_file.relative_to(self.repo_root))
            if "/test/" in rel:
                continue

            try:
                content = sail_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            uuid = sail_file.stem  # SYSTEM_SYSRULES_ruleName

            type_refs = set(cdt_ref_pattern.findall(content))
            for type_name in type_refs:
                if type_name in known_types:
                    edges.append({
                        "from_id": uuid,
                        "to_id": type_name,
                    })

        # Deduplicate
        seen = set()
        unique = []
        for e in edges:
            key = (e["from_id"], e["to_id"])
            if key not in seen:
                seen.add(key)
                unique.append(e)

        if unique:
            self.client.batch_create_relationships(
                "REFERENCES_CDT", unique,
                from_label="SailRule", to_label="CdtType",
                from_key="uuid", to_key="name",
            )
        logger.info(f"  Created {len(unique)} REFERENCES_CDT edges (SailRule→CdtType)")
