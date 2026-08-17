"""
AE Knowledge Graph — Extractors.

Each extractor is responsible for parsing one category of source code
and producing nodes + relationships for the knowledge graph.

All extractors implement the BaseExtractor interface.
"""

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from graph.neo4j_client import GraphClient

logger = logging.getLogger(__name__)


class BaseExtractor(ABC):
    """
    Base class for all extractors.

    Each extractor:
    1. Scans a specific set of files in the repo
    2. Parses them to extract nodes and relationships
    3. Writes results to Neo4j via the GraphClient

    Subclasses must implement:
        - name: Human-readable name for logging
        - extract(): The main extraction logic
    """

    def __init__(self, client: GraphClient, repo_root: Path):
        self.client = client
        self.repo_root = repo_root
        self._nodes_created = 0
        self._rels_created = 0

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable extractor name (e.g., 'Gradle Extractor')."""
        ...

    @abstractmethod
    def extract(self):
        """
        Run the extraction.

        Implementations should:
        1. Find relevant files in self.repo_root
        2. Parse them
        3. Call self.client.batch_create_nodes() and self.client.batch_create_relationships()
        """
        ...

    def run(self) -> dict:
        """
        Execute the extractor with timing and error handling.
        Returns a stats dict.
        """
        logger.info(f"{'=' * 60}")
        logger.info(f"Running: {self.name}")
        logger.info(f"{'=' * 60}")

        start = time.time()
        try:
            self.extract()
            elapsed = time.time() - start
            status = "success"
            error = None
        except Exception as e:
            elapsed = time.time() - start
            status = "failed"
            error = str(e)
            logger.error(f"{self.name} failed after {elapsed:.2f}s: {e}", exc_info=True)

        stats = {
            "extractor": self.name,
            "status": status,
            "elapsed_seconds": round(elapsed, 2),
            "error": error,
        }

        if status == "success":
            logger.info(f"✓ {self.name} completed in {elapsed:.2f}s")
        else:
            logger.error(f"✗ {self.name} failed in {elapsed:.2f}s: {error}")

        return stats

    def relative_path(self, absolute_path: Path) -> str:
        """Convert absolute path to repo-relative path string."""
        try:
            return str(absolute_path.relative_to(self.repo_root))
        except ValueError:
            return str(absolute_path)
