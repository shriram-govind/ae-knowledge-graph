"""
AE Knowledge Graph — Graph client and schema management.

This package provides the Neo4j connection layer and schema definitions
for the AE monorepo knowledge graph.
"""

from graph.neo4j_client import GraphClient
from graph.schema import create_indexes_and_constraints

__all__ = ["GraphClient", "create_indexes_and_constraints"]
