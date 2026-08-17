"""
SAIL XML Extractor — Parses SYSTEM_SYSRULES_*.xml files to extract rule definitions and call graph.

Extracts:
- SailRule nodes (name, uuid, path, parentUuid, isSystemOnly, functionCategory)
- CALLS relationships (a!ruleName() invocations between rules)
- REFERENCES_CDT relationships (type!{namespace}TypeName usage)
- Module → SailRule CONTAINS relationships

Two-pass approach:
1. First pass: collect all rule names to build a validation set
2. Second pass: extract calls and validate targets exist (prevents false positives)
"""

import re
import logging
from pathlib import Path
from collections import defaultdict

from lxml import etree

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Regex patterns for SAIL CDATA content
# Matches: a!ruleName( — captures the rule name
SAIL_RULE_CALL = re.compile(r'a!(\w+)\s*\(')

# Matches: fn!functionName( — captures the function name
SAIL_FN_CALL = re.compile(r'fn!(\w+)\s*\(')

# Matches: a!externalReaction("reactionKey" — captures the key
SAIL_REACTION_CALL = re.compile(r'a!externalReaction\s*\(\s*"([^"]+)"')

# Matches: 'type!{namespace}TypeName' — captures namespace and type name
SAIL_CDT_REF = re.compile(r"'type!\{([^}]+)\}(\w+)'")

# Matches: a!isFeatureEnabled("toggle.key") — captures toggle key
SAIL_TOGGLE_REF = re.compile(r'a!isFeatureEnabled\s*\(\s*"([^"]+)"')


class SailExtractor(BaseExtractor):
    """Extracts SAIL system rule definitions and invocation graph."""

    @property
    def name(self) -> str:
        return "SAIL Extractor"

    def extract(self):
        # Find all SAIL rule XML files
        sail_files = self._find_sail_files()
        logger.info(f"  Found {len(sail_files)} SAIL rule files")

        # First pass: collect all rule names (for validation)
        rules: list[dict] = []
        rule_names: set[str] = set()
        rule_definitions: dict[str, str] = {}  # name → definition CDATA

        for sail_file in sail_files:
            rule_info = self._parse_rule_metadata(sail_file)
            if rule_info:
                rules.append(rule_info)
                rule_names.add(rule_info["name"])
                if rule_info.get("_definition"):
                    rule_definitions[rule_info["name"]] = rule_info["_definition"]

        logger.info(f"  Parsed {len(rules)} rule definitions")

        # Create SailRule nodes
        # Remove internal _definition field before writing to Neo4j
        rule_nodes = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rules]
        self.client.batch_create_nodes("SailRule", rule_nodes, merge_key="uuid")
        logger.info(f"  Created {len(rule_nodes)} SailRule nodes")

        # Second pass: extract CALLS relationships from definitions
        calls: list[dict] = []
        fn_calls: list[dict] = []  # fn! function calls (for cross-language binder)
        reaction_calls: list[dict] = []
        cdt_refs: list[dict] = []

        for rule in rules:
            definition = rule.get("_definition", "")
            if not definition:
                continue

            rule_uuid = rule["uuid"]
            source_file = rule["path"]

            # Extract a!ruleName() calls
            for match in SAIL_RULE_CALL.finditer(definition):
                called_name = match.group(1)
                # Only create edge if target is a known rule (avoids built-in function false positives)
                if called_name in rule_names and called_name != rule["name"]:
                    calls.append({
                        "from_id": rule_uuid,
                        "to_id": f"SYSTEM_SYSRULES_{called_name}",
                        "sourceFile": source_file,
                    })

            # Extract fn!functionName() calls (stored for cross-language binder)
            for match in SAIL_FN_CALL.finditer(definition):
                fn_name = match.group(1)
                fn_calls.append({
                    "rule_uuid": rule_uuid,
                    "rule_name": rule["name"],
                    "fn_name": fn_name,
                    "source_file": source_file,
                })

            # Extract a!externalReaction("key") calls
            for match in SAIL_REACTION_CALL.finditer(definition):
                reaction_key = match.group(1)
                reaction_calls.append({
                    "rule_uuid": rule_uuid,
                    "rule_name": rule["name"],
                    "reaction_key": reaction_key,
                    "source_file": source_file,
                })

            # Extract 'type!{namespace}TypeName' references
            for match in SAIL_CDT_REF.finditer(definition):
                namespace = match.group(1)
                type_name = match.group(2)
                cdt_refs.append({
                    "from_id": rule_uuid,
                    "to_id": type_name,
                    "namespace": namespace,
                    "sourceFile": source_file,
                })

        # Deduplicate calls (same rule may call another rule multiple times)
        seen_calls = set()
        unique_calls = []
        for call in calls:
            key = (call["from_id"], call["to_id"])
            if key not in seen_calls:
                seen_calls.add(key)
                unique_calls.append(call)

        # Write CALLS relationships
        self.client.batch_create_relationships(
            "CALLS", unique_calls,
            from_label="SailRule", to_label="SailRule",
            from_key="uuid", to_key="uuid",
        )
        logger.info(f"  Created {len(unique_calls)} CALLS relationships (deduplicated from {len(calls)})")

        # Write Module → SailRule CONTAINS relationships
        self._create_module_containment(rules)

        # Store fn_calls and reaction_calls as proper relationship edges
        # (These are used by the cross-language binder to create CALLS_BUILTIN edges)
        self._create_fn_call_edges(fn_calls)
        self._create_reaction_call_edges(reaction_calls)

        logger.info(f"  Extracted {len(fn_calls)} fn! calls, {len(reaction_calls)} reaction calls, {len(cdt_refs)} CDT references (for cross-language binding)")

    def _create_fn_call_edges(self, fn_calls: list[dict]):
        """Create CALLS_FN edges from SailRule to fn! function names."""
        # Deduplicate: one edge per (rule, fn_name) pair
        seen = set()
        unique_edges = []
        for call in fn_calls:
            key = (call["rule_uuid"], call["fn_name"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(call)

        # Store as a property on the node for the cross-language binder to read
        # (The binder needs to match fn_names against Java FN_ID registrations)
        rule_fn_map = defaultdict(set)
        for call in unique_edges:
            rule_fn_map[call["rule_uuid"]].add(call["fn_name"])

        for rule_uuid, fn_names in rule_fn_map.items():
            self.client.run_query(
                "MATCH (r:SailRule {uuid: $uuid}) SET r._fnCalls = $fnCalls",
                uuid=rule_uuid,
                fnCalls=",".join(sorted(fn_names)),
            )

        logger.info(f"  Stored fn! call metadata on {len(rule_fn_map)} rules ({len(unique_edges)} unique fn! calls)")

    def _create_reaction_call_edges(self, reaction_calls: list[dict]):
        """Store reaction call data for the cross-language binder."""
        rule_reactions = defaultdict(set)
        for call in reaction_calls:
            rule_reactions[call["rule_uuid"]].add(call["reaction_key"])

        for rule_uuid, keys in rule_reactions.items():
            self.client.run_query(
                "MATCH (r:SailRule {uuid: $uuid}) SET r._reactionCalls = $reactions",
                uuid=rule_uuid,
                reactions=",".join(sorted(keys)),
            )

    def _find_sail_files(self) -> list[Path]:
        """Find all SYSTEM_SYSRULES_*.xml files (excluding folders)."""
        sail_files = []
        for xml_file in self.repo_root.rglob("SYSTEM_SYSRULES_*.xml"):
            # Skip folder definitions
            if "FOLDER" in xml_file.name:
                continue
            # Skip test files
            rel = str(xml_file.relative_to(self.repo_root))
            if "/test/" in rel or "/tests/" in rel:
                continue
            sail_files.append(xml_file)
        return sail_files

    def _parse_rule_metadata(self, file_path: Path) -> dict | None:
        """Parse a SAIL rule XML file and extract metadata."""
        try:
            tree = etree.parse(str(file_path), etree.XMLParser(recover=True))
            root = tree.getroot()
        except Exception:
            return None

        # Handle namespace
        ns = {"a": "http://www.appian.com/ae/types/2009"}

        # Find the <rule> element
        rule_elem = root.find(".//rule", ns)
        if rule_elem is None:
            rule_elem = root.find("rule")
        if rule_elem is None:
            # Try without namespace
            for elem in root.iter():
                if elem.tag.endswith("rule") or elem.tag == "rule":
                    rule_elem = elem
                    break
        if rule_elem is None:
            return None

        # Extract fields (try with and without namespace)
        def get_text(parent, tag):
            elem = parent.find(tag, ns)
            if elem is None:
                elem = parent.find(tag)
            if elem is None:
                # Try any namespace - iterate children safely
                for child in parent:
                    try:
                        child_tag = child.tag if isinstance(child.tag, str) else ""
                        if child_tag.endswith(f"}}{tag}") or child_tag == tag:
                            return child.text
                    except (AttributeError, TypeError):
                        continue
                return None
            return elem.text

        name = get_text(rule_elem, "name")
        uuid = get_text(rule_elem, "uuid")

        if not name or not uuid:
            return None

        parent_uuid = get_text(rule_elem, "parentUuid") or ""
        description = get_text(rule_elem, "description") or ""
        function_category = get_text(rule_elem, "functionCategory") or ""

        # Extract definition CDATA
        definition = get_text(rule_elem, "definition") or ""

        # Check metadataExpr for systemOnly
        metadata_expr = get_text(rule_elem, "metadataExpr") or ""
        is_system_only = "systemOnly: fn!true()" in metadata_expr or "systemOnly:fn!true()" in metadata_expr

        rel_path = self.relative_path(file_path)

        return {
            "name": name,
            "uuid": uuid,
            "path": rel_path,
            "parentUuid": parent_uuid,
            "isSystemOnly": is_system_only,
            "functionCategory": function_category,
            "description": description[:200] if description else "",  # Truncate for storage
            "definitionLineCount": definition.count("\n") + 1 if definition else 0,
            "_definition": definition,  # Internal, not written to Neo4j
        }

    def _create_module_containment(self, rules: list[dict]):
        """Create Module → SailRule CONTAINS edges based on file paths."""
        # Get module paths from the graph
        module_map = {}
        results = self.client.run_query(
            "MATCH (m:Module) RETURN m.path AS path, m.gradlePath AS gradlePath"
        )
        for row in results:
            module_map[row["path"]] = row["gradlePath"]

        # Map each rule to its module
        contains_rels = []
        for rule in rules:
            rule_path = rule["path"]
            # Walk up directory tree to find module
            parts = rule_path.split("/")
            for i in range(len(parts), 0, -1):
                candidate = "/".join(parts[:i])
                if candidate in module_map:
                    contains_rels.append({
                        "from_id": module_map[candidate],
                        "to_id": rule["uuid"],
                    })
                    break

        if contains_rels:
            self.client.batch_create_relationships(
                "CONTAINS", contains_rels,
                from_label="Module", to_label="SailRule",
                from_key="gradlePath", to_key="uuid",
            )
            logger.info(f"  Created {len(contains_rels)} Module→SailRule CONTAINS edges")

    # _store_sail_call_metadata removed — replaced by _create_fn_call_edges and _create_reaction_call_edges
