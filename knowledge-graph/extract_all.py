#!/usr/bin/env python3
"""
AE Knowledge Graph — Main extraction orchestrator.

Runs all extractors in sequence to build the complete knowledge graph.

Usage:
    python extract_all.py --repo-root ~/repo/ae
    python extract_all.py --repo-root ~/repo/ae --only gradle,java
    python extract_all.py --repo-root ~/repo/ae --stats
    python extract_all.py --repo-root ~/repo/ae --validate
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from graph.neo4j_client import GraphClient
from graph.schema import create_indexes_and_constraints

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("extract_all")


# Registry of all extractors (populated as they're implemented)
# Each entry: (key, module_path, class_name)
EXTRACTOR_REGISTRY = [
    ("gradle", "extractors.gradle_extractor", "GradleExtractor"),
    ("java", "extractors.java_extractor", "JavaExtractor"),
    ("sail", "extractors.sail_extractor", "SailExtractor"),
    ("cross_language", "extractors.cross_language_binder", "CrossLanguageBinder"),
    ("spring_di", "extractors.spring_di_extractor", "SpringDIExtractor"),
    ("external_libs", "extractors.external_lib_extractor", "ExternalLibraryExtractor"),
    ("lib_usage", "extractors.library_usage_mapper", "LibraryUsageMapper"),
    ("lib_transitivity", "extractors.lib_transitivity_extractor", "LibTransitivityExtractor"),
    ("typescript", "extractors.typescript_extractor", "TypeScriptExtractor"),
    ("npm", "extractors.npm_extractor", "NpmExtractor"),
    ("k", "extractors.k_extractor", "KExtractor"),
    ("db", "extractors.db_extractor", "DbExtractor"),
    ("toggle", "extractors.toggle_extractor", "ToggleExtractor"),
    ("infra", "extractors.infra_extractor", "InfraExtractor"),
    ("expression_tests", "extractors.expression_test_extractor", "ExpressionTestExtractor"),
    ("resource_bundles", "extractors.resource_bundle_extractor", "ResourceBundleExtractor"),
    ("xsd_cdt", "extractors.xsd_cdt_extractor", "XsdCdtExtractor"),
    ("gitlab_ci", "extractors.gitlab_ci_extractor", "GitlabCiExtractor"),
    ("freemarker", "extractors.freemarker_extractor", "FreemarkerExtractor"),
    ("web_xml", "extractors.web_xml_extractor", "WebXmlExtractor"),
    ("groovy_tests", "extractors.groovy_test_extractor", "GroovyTestExtractor"),
]


def load_extractor(module_path: str, class_name: str, client: GraphClient, repo_root: Path):
    """Dynamically import and instantiate an extractor class."""
    import importlib
    module = importlib.import_module(module_path)
    extractor_class = getattr(module, class_name)
    return extractor_class(client=client, repo_root=repo_root)


def main():
    parser = argparse.ArgumentParser(
        description="AE Knowledge Graph — Extract dependencies from the ae monorepo into Neo4j"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="Path to the ae repository root (e.g., ~/repo/ae)",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Comma-separated list of extractor keys to run (e.g., 'gradle,java')",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print graph statistics after extraction",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run validation checks after extraction",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Skip clearing the graph before extraction (incremental mode)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate repo root
    repo_root = args.repo_root.expanduser().resolve()
    if not (repo_root / "settings.gradle").exists():
        logger.error(f"Not a valid ae repo root: {repo_root} (settings.gradle not found)")
        sys.exit(1)

    logger.info(f"Repository root: {repo_root}")
    logger.info(f"{'=' * 60}")
    logger.info("AE KNOWLEDGE GRAPH — EXTRACTION PIPELINE")
    logger.info(f"{'=' * 60}")

    # Connect to Neo4j
    client = GraphClient()
    if not client.ping():
        logger.error("Cannot connect to Neo4j. Is it running? (docker compose up -d)")
        sys.exit(1)
    logger.info("✓ Connected to Neo4j")

    # Determine which extractors to run
    if args.only:
        selected_keys = set(args.only.split(","))
    else:
        selected_keys = None  # Run all

    extractors_to_run = []
    for key, module_path, class_name in EXTRACTOR_REGISTRY:
        if selected_keys is None or key in selected_keys:
            extractors_to_run.append((key, module_path, class_name))

    if not extractors_to_run:
        logger.warning("No extractors to run. Check --only flag or EXTRACTOR_REGISTRY.")
        logger.info("Available extractors: " + ", ".join(k for k, _, _ in EXTRACTOR_REGISTRY))
        sys.exit(0)

    # Clear graph (unless --no-clear)
    if not args.no_clear:
        client.clear_graph()

    # Create schema
    create_indexes_and_constraints(client)

    # Run extractors
    total_start = time.time()
    results = []

    for key, module_path, class_name in extractors_to_run:
        try:
            extractor = load_extractor(module_path, class_name, client, repo_root)
            stats = extractor.run()
            results.append(stats)
        except Exception as e:
            logger.error(f"Failed to load extractor '{key}': {e}")
            results.append({
                "extractor": key,
                "status": "load_failed",
                "elapsed_seconds": 0,
                "error": str(e),
            })

    total_elapsed = time.time() - total_start

    # Summary
    logger.info(f"\n{'=' * 60}")
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"{'=' * 60}")
    logger.info(f"Total time: {total_elapsed:.2f}s")
    logger.info(f"Extractors run: {len(results)}")
    logger.info(f"  Succeeded: {sum(1 for r in results if r['status'] == 'success')}")
    logger.info(f"  Failed: {sum(1 for r in results if r['status'] != 'success')}")

    for r in results:
        status_icon = "✓" if r["status"] == "success" else "✗"
        logger.info(f"  {status_icon} {r['extractor']}: {r['elapsed_seconds']}s")

    # Print stats if requested
    if args.stats:
        logger.info(f"\n{'=' * 60}")
        logger.info("GRAPH STATISTICS")
        logger.info(f"{'=' * 60}")
        stats = client.get_stats()
        for key, value in sorted(stats.items()):
            logger.info(f"  {key}: {value:,}")

    # Validation if requested
    if args.validate:
        logger.info(f"\n{'=' * 60}")
        logger.info("VALIDATION")
        logger.info(f"{'=' * 60}")
        run_validation(client, repo_root)

    client.close()

    # Exit with error if any extractor failed
    if any(r["status"] != "success" for r in results):
        sys.exit(1)


def run_validation(client: GraphClient, repo_root: Path):
    """
    Validate graph integrity by sampling random edges and checking sourceFile exists.
    """
    import random

    # Sample nodes and check their path property points to a real file
    sample_query = """
    MATCH (n)
    WHERE n.path IS NOT NULL
    RETURN n.path AS path, labels(n) AS labels
    ORDER BY rand()
    LIMIT 50
    """
    results = client.run_query(sample_query)

    valid = 0
    invalid = 0
    for row in results:
        file_path = repo_root / row["path"]
        if file_path.exists():
            valid += 1
        else:
            invalid += 1
            logger.warning(f"  Invalid path: {row['path']} (labels: {row['labels']})")

    logger.info(f"  Path validation: {valid}/{valid + invalid} nodes point to existing files")
    if invalid > 0:
        logger.warning(f"  {invalid} nodes have stale/invalid paths")


if __name__ == "__main__":
    main()
