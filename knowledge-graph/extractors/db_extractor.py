"""
DB Schema Extractor — Parses Liquibase YAML changelogs for database table relationships.

Extracts:
- Table nodes (name, changelogFile, migrationId)
- FK_TO relationships (foreign key constraints between tables)
- Entity → Table MAPS_TO relationships (linking JPA entities to their tables)
- Index and Sequence metadata on Table nodes
"""

import logging
from pathlib import Path

import yaml

from extractors import BaseExtractor
from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)


class DbExtractor(BaseExtractor):
    """Extracts database schema from Liquibase YAML changelogs."""

    @property
    def name(self) -> str:
        return "DB Schema Extractor"

    def extract(self):
        # Find Liquibase changelog files
        changelog_dir = self.repo_root / "deployment" / "web.war" / "WEB-INF" / "resources" / "appian" / "db" / "changelog"
        if not changelog_dir.exists():
            logger.warning(f"  Changelog directory not found: {changelog_dir}")
            return

        yaml_files = list(changelog_dir.glob("*.yaml"))
        logger.info(f"  Found {len(yaml_files)} Liquibase YAML changelogs")

        tables: dict[str, dict] = {}  # table_name → metadata
        fk_relationships: list[dict] = []
        indexes: list[dict] = []

        for yaml_file in yaml_files:
            try:
                content = yaml_file.read_text(encoding="utf-8")
                data = yaml.safe_load(content)
            except Exception as e:
                logger.debug(f"  Failed to parse {yaml_file.name}: {e}")
                continue

            if not data or "databaseChangeLog" not in data:
                continue

            changelog = data["databaseChangeLog"]
            if not isinstance(changelog, list):
                continue

            rel_path = self.relative_path(yaml_file)

            for entry in changelog:
                if not isinstance(entry, dict):
                    continue

                changeset = entry.get("changeSet")
                if not changeset:
                    continue

                changeset_id = changeset.get("id", "")
                changes = changeset.get("changes", [])
                if not isinstance(changes, list):
                    # Single change directly in changeset
                    changes = [changeset]

                for change in changes:
                    if not isinstance(change, dict):
                        continue

                    # createTable
                    if "createTable" in change:
                        ct = change["createTable"]
                        table_name = ct.get("tableName")
                        if table_name:
                            tables[table_name] = {
                                "name": table_name,
                                "changelogFile": rel_path,
                                "migrationId": changeset_id,
                            }

                    # addForeignKeyConstraint
                    if "addForeignKeyConstraint" in change:
                        fk = change["addForeignKeyConstraint"]
                        base_table = fk.get("baseTableName")
                        ref_table = fk.get("referencedTableName")
                        constraint_name = fk.get("constraintName", "")
                        base_column = fk.get("baseColumnNames", "")
                        ref_column = fk.get("referencedColumnNames", "")
                        on_delete = fk.get("onDelete", "")

                        if base_table and ref_table:
                            # Ensure both tables are tracked
                            if base_table not in tables:
                                tables[base_table] = {"name": base_table, "changelogFile": rel_path, "migrationId": ""}
                            if ref_table not in tables:
                                tables[ref_table] = {"name": ref_table, "changelogFile": rel_path, "migrationId": ""}

                            fk_relationships.append({
                                "from_id": base_table,
                                "to_id": ref_table,
                                "constraintName": constraint_name,
                                "baseColumn": base_column,
                                "referencedColumn": ref_column,
                                "onDelete": on_delete,
                                "sourceFile": rel_path,
                            })

                    # Also handle top-level addForeignKeyConstraint (not nested in changes)
                if "addForeignKeyConstraint" in changeset:
                    fk = changeset["addForeignKeyConstraint"]
                    base_table = fk.get("baseTableName")
                    ref_table = fk.get("referencedTableName")
                    constraint_name = fk.get("constraintName", "")
                    base_column = fk.get("baseColumnNames", "")
                    ref_column = fk.get("referencedColumnNames", "")
                    on_delete = fk.get("onDelete", "")

                    if base_table and ref_table:
                        if base_table not in tables:
                            tables[base_table] = {"name": base_table, "changelogFile": rel_path, "migrationId": ""}
                        if ref_table not in tables:
                            tables[ref_table] = {"name": ref_table, "changelogFile": rel_path, "migrationId": ""}

                        fk_relationships.append({
                            "from_id": base_table,
                            "to_id": ref_table,
                            "constraintName": constraint_name,
                            "baseColumn": base_column,
                            "referencedColumn": ref_column,
                            "onDelete": on_delete,
                            "sourceFile": rel_path,
                        })

                    # createIndex
                    if "createIndex" in change:
                        idx = change["createIndex"]
                        table_name = idx.get("tableName")
                        index_name = idx.get("indexName", "")
                        if table_name:
                            indexes.append({"table": table_name, "index": index_name})

        logger.info(f"  Found {len(tables)} tables, {len(fk_relationships)} FK relationships, {len(indexes)} indexes")

        # Create Table nodes
        table_nodes = list(tables.values())
        if table_nodes:
            self.client.batch_create_nodes("Table", table_nodes, merge_key="name")
            logger.info(f"  Created {len(table_nodes)} Table nodes")

        # Create FK_TO relationships
        # Deduplicate (same FK can appear in multiple changelog versions)
        seen = set()
        unique_fks = []
        for fk in fk_relationships:
            key = (fk["from_id"], fk["to_id"], fk["constraintName"])
            if key not in seen:
                seen.add(key)
                unique_fks.append(fk)

        if unique_fks:
            self.client.batch_create_relationships(
                "FK_TO", unique_fks,
                from_label="Table", to_label="Table",
                from_key="name", to_key="name",
            )
        logger.info(f"  Created {len(unique_fks)} FK_TO relationships")

        # Create Entity → Table MAPS_TO relationships (link existing Entity nodes to Table nodes)
        self._link_entities_to_tables(tables)

    def _link_entities_to_tables(self, tables: dict):
        """Link JPA Entity nodes (from Java extractor) to their Table nodes."""
        results = self.client.run_query(
            "MATCH (e:Entity) WHERE e.tableName IS NOT NULL RETURN e.fqn AS fqn, e.tableName AS tableName"
        )

        maps_to_edges = []
        for row in results:
            table_name = row["tableName"]
            if table_name in tables:
                maps_to_edges.append({
                    "from_id": row["fqn"],
                    "to_id": table_name,
                })

        if maps_to_edges:
            self.client.batch_create_relationships(
                "MAPS_TO", maps_to_edges,
                from_label="Entity", to_label="Table",
                from_key="fqn", to_key="name",
            )
        logger.info(f"  Created {len(maps_to_edges)} Entity→Table MAPS_TO edges")
