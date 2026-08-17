"""
Neo4j client wrapper providing connection management and batch write operations.

All writes use the UNWIND pattern for performance — batching thousands of
nodes/relationships into single transactions.
"""

import os
import time
import logging
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase, Driver, Session

logger = logging.getLogger(__name__)

# Default batch size for UNWIND operations
DEFAULT_BATCH_SIZE = 5000


class GraphClient:
    """
    Neo4j connection manager with batch write helpers.

    Usage:
        client = GraphClient()
        client.ping()  # verify connection
        client.batch_create_nodes("JavaClass", [{"fqn": "com.X.Y", "name": "Y", ...}, ...])
        client.batch_create_relationships("IMPORTS", [...])
        client.close()
    """

    def __init__(
        self,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        load_dotenv()
        self._uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self._username = username or os.getenv("NEO4J_USERNAME", "neo4j")
        self._password = password or os.getenv("NEO4J_PASSWORD", "knowledge")
        self._driver: Driver | None = None

    @property
    def driver(self) -> Driver:
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._username, self._password)
            )
        return self._driver

    def ping(self) -> bool:
        """Verify Neo4j connectivity. Returns True if successful."""
        try:
            with self.driver.session() as session:
                result = session.run("RETURN 1 AS ping")
                record = result.single()
                return record is not None and record["ping"] == 1
        except Exception as e:
            logger.error(f"Failed to connect to Neo4j at {self._uri}: {e}")
            return False

    def close(self):
        """Close the driver connection."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def clear_graph(self):
        """
        Delete all nodes and relationships. Used for idempotent full rebuild.
        Deletes in batches to avoid memory issues on large graphs.
        """
        logger.info("Clearing entire graph...")
        with self.driver.session() as session:
            # Delete in batches to handle large graphs
            while True:
                result = session.run(
                    "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(*) AS deleted"
                )
                deleted = result.single()["deleted"]
                if deleted == 0:
                    break
                logger.debug(f"  Deleted {deleted} nodes...")
        logger.info("Graph cleared.")

    def batch_create_nodes(
        self,
        label: str,
        nodes: list[dict[str, Any]],
        merge_key: str = "fqn",
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> int:
        """
        Create or merge nodes in batches using UNWIND.

        Args:
            label: Node label (e.g., "JavaClass", "Module")
            nodes: List of property dicts for each node
            merge_key: Property to MERGE on (must be unique per node)
            batch_size: Number of nodes per transaction

        Returns:
            Total number of nodes created/merged.
        """
        if not nodes:
            return 0

        total = 0
        start = time.time()

        # Build the SET clause dynamically from the first node's keys
        # (all nodes in a batch should have the same keys)
        set_keys = [k for k in nodes[0].keys() if k != merge_key]
        set_clause = ", ".join([f"n.{k} = row.{k}" for k in set_keys])

        query = f"""
        UNWIND $batch AS row
        MERGE (n:{label} {{{merge_key}: row.{merge_key}}})
        SET {set_clause}
        RETURN count(n) AS created
        """

        with self.driver.session() as session:
            for i in range(0, len(nodes), batch_size):
                batch = nodes[i : i + batch_size]
                result = session.run(query, batch=batch)
                total += result.single()["created"]

        elapsed = time.time() - start
        logger.info(
            f"Created/merged {total} :{label} nodes in {elapsed:.2f}s "
            f"({total / elapsed:.0f} nodes/sec)"
        )
        return total

    def batch_create_relationships(
        self,
        rel_type: str,
        relationships: list[dict[str, Any]],
        from_label: str = "JavaClass",
        to_label: str = "JavaClass",
        from_key: str = "fqn",
        to_key: str = "fqn",
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> int:
        """
        Create relationships in batches using UNWIND.

        Each relationship dict must have:
            - "from_id": value matching from_key on the source node
            - "to_id": value matching to_key on the target node
            - Any additional properties for the relationship

        Args:
            rel_type: Relationship type (e.g., "IMPORTS", "DEPENDS_ON")
            relationships: List of dicts with from_id, to_id, and optional properties
            from_label: Label of source nodes
            to_label: Label of target nodes
            from_key: Property to match source nodes on
            to_key: Property to match target nodes on
            batch_size: Number of relationships per transaction

        Returns:
            Total number of relationships created.
        """
        if not relationships:
            return 0

        total = 0
        start = time.time()

        # Extract extra properties (beyond from_id and to_id)
        sample = relationships[0]
        extra_keys = [k for k in sample.keys() if k not in ("from_id", "to_id")]
        if extra_keys:
            props_clause = " {" + ", ".join([f"{k}: row.{k}" for k in extra_keys]) + "}"
        else:
            props_clause = ""

        query = f"""
        UNWIND $batch AS row
        MATCH (from:{from_label} {{{from_key}: row.from_id}})
        MATCH (to:{to_label} {{{to_key}: row.to_id}})
        MERGE (from)-[r:{rel_type}{props_clause}]->(to)
        RETURN count(r) AS created
        """

        with self.driver.session() as session:
            for i in range(0, len(relationships), batch_size):
                batch = relationships[i : i + batch_size]
                result = session.run(query, batch=batch)
                total += result.single()["created"]

        elapsed = time.time() - start
        logger.info(
            f"Created {total} -[:{rel_type}]-> relationships in {elapsed:.2f}s "
            f"({total / elapsed:.0f} rels/sec)"
        )
        return total

    def run_query(self, query: str, **params) -> list[dict]:
        """
        Run an arbitrary Cypher query and return results as list of dicts.
        For ad-hoc queries during development/testing.
        """
        with self.driver.session() as session:
            result = session.run(query, **params)
            return [record.data() for record in result]

    def get_node_count(self, label: str | None = None) -> int:
        """Get count of nodes, optionally filtered by label."""
        if label:
            query = f"MATCH (n:{label}) RETURN count(n) AS count"
        else:
            query = "MATCH (n) RETURN count(n) AS count"
        result = self.run_query(query)
        return result[0]["count"] if result else 0

    def get_relationship_count(self, rel_type: str | None = None) -> int:
        """Get count of relationships, optionally filtered by type."""
        if rel_type:
            query = f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS count"
        else:
            query = "MATCH ()-[r]->() RETURN count(r) AS count"
        result = self.run_query(query)
        return result[0]["count"] if result else 0

    def get_stats(self) -> dict[str, int]:
        """Get a summary of graph contents by label and relationship type."""
        stats = {}

        # Node counts by label
        labels_result = self.run_query(
            "CALL db.labels() YIELD label RETURN label"
        )
        for row in labels_result:
            label = row["label"]
            stats[f"nodes:{label}"] = self.get_node_count(label)

        # Relationship counts by type
        types_result = self.run_query(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
        )
        for row in types_result:
            rel_type = row["relationshipType"]
            stats[f"rels:{rel_type}"] = self.get_relationship_count(rel_type)

        stats["total_nodes"] = self.get_node_count()
        stats["total_relationships"] = self.get_relationship_count()

        return stats

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
