"""
Graph schema — index and constraint definitions for the AE Knowledge Graph.

Run this after clearing the graph and before populating with extractors.
"""

import logging

from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)

# Unique constraints (also create indexes automatically)
CONSTRAINTS = [
    ("Module", "gradlePath"),
    ("JavaClass", "fqn"),
    ("SailRule", "uuid"),
    ("Table", "name"),
    ("FeatureToggle", "name"),
    ("CdtType", "name"),
]

# Additional indexes for fast lookup (beyond constraints)
INDEXES = [
    ("Module", "name"),
    ("JavaClass", "name"),
    ("JavaClass", "path"),
    ("SailRule", "name"),
    ("SailRule", "path"),
    ("TsFile", "path"),
    ("KFile", "path"),
    ("JspPage", "path"),
    ("Package", "name"),
    ("ApiEndpoint", "path"),
    ("Entity", "tableName"),
    ("Team", "handle"),
    ("DockerService", "name"),
    ("CiPipeline", "name"),
]


def create_indexes_and_constraints(client: GraphClient):
    """
    Create all indexes and constraints for the knowledge graph schema.
    Safe to run multiple times (CREATE ... IF NOT EXISTS).
    """
    logger.info("Creating schema constraints and indexes...")

    with client.driver.session() as session:
        # Create unique constraints
        for label, prop in CONSTRAINTS:
            query = (
                f"CREATE CONSTRAINT constraint_{label.lower()}_{prop} "
                f"IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            try:
                session.run(query)
                logger.debug(f"  Constraint: {label}.{prop} (unique)")
            except Exception as e:
                logger.warning(f"  Failed to create constraint {label}.{prop}: {e}")

        # Create indexes
        for label, prop in INDEXES:
            query = (
                f"CREATE INDEX index_{label.lower()}_{prop} "
                f"IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.{prop})"
            )
            try:
                session.run(query)
                logger.debug(f"  Index: {label}.{prop}")
            except Exception as e:
                logger.warning(f"  Failed to create index {label}.{prop}: {e}")

    logger.info(
        f"Schema ready: {len(CONSTRAINTS)} constraints, {len(INDEXES)} indexes."
    )


def drop_all_constraints_and_indexes(client: GraphClient):
    """Drop all constraints and indexes. Used before full schema recreation."""
    logger.info("Dropping all constraints and indexes...")

    with client.driver.session() as session:
        # Drop constraints
        constraints = session.run("SHOW CONSTRAINTS YIELD name RETURN name")
        for record in constraints:
            session.run(f"DROP CONSTRAINT {record['name']} IF EXISTS")

        # Drop indexes
        indexes = session.run(
            "SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name"
        )
        for record in indexes:
            session.run(f"DROP INDEX {record['name']} IF EXISTS")

    logger.info("All constraints and indexes dropped.")
