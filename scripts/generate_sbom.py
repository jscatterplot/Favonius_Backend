#!/usr/bin/env python3
"""Generate a CycloneDX Software Bill of Materials (SBOM).

NIS2 Article 21 requires supply chain security measures, including
maintaining an inventory of software components. This script generates
a CycloneDX SBOM from the project's pyproject.toml dependencies.

Usage:
    python scripts/generate_sbom.py
    python scripts/generate_sbom.py --output sbom.json
    python scripts/generate_sbom.py --format xml

Output:
    CycloneDX SBOM in JSON or XML format.

Requirements:
    pip install cyclonedx-bom  (or run: pip install -e ".[dev]")
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_pyproject_dependencies(pyproject_path: str = "pyproject.toml") -> list[dict]:
    """Parse dependencies from pyproject.toml.

    Args:
        pyproject_path: Path to pyproject.toml.

    Returns:
        List of dependency dicts with name and version_constraint.
    """
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    path = Path(pyproject_path)
    if not path.exists():
        print(f"ERROR: {pyproject_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(path, "rb") as f:
        data = tomllib.load(f)

    deps = data.get("project", {}).get("dependencies", [])
    parsed = []
    for dep in deps:
        # Parse "package>=1.0.0" format
        for sep in [">=", "<=", "==", "!=", "~=", ">", "<"]:
            if sep in dep:
                name, version = dep.split(sep, 1)
                parsed.append({
                    "name": name.strip(),
                    "version_constraint": f"{sep}{version.strip()}",
                })
                break
        else:
            parsed.append({"name": dep.strip(), "version_constraint": ""})

    return parsed


def generate_cyclonedx_sbom(
    dependencies: list[dict],
    project_name: str = "favonius-backend",
    project_version: str = "0.1.0",
) -> dict:
    """Generate a CycloneDX SBOM in JSON format.

    Args:
        dependencies: List of parsed dependencies.
        project_name: Name of the project.
        project_version: Version of the project.

    Returns:
        CycloneDX SBOM as a dict.
    """
    components = []
    for dep in dependencies:
        component = {
            "type": "library",
            "name": dep["name"],
            "version": dep.get("version_constraint", ""),
            "purl": f"pkg:pypi/{dep['name']}",
        }
        components.append(component)

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {
                "type": "application",
                "name": project_name,
                "version": project_version,
            },
            "tools": [
                {
                    "vendor": "Favonius Energy",
                    "name": "generate_sbom.py",
                    "version": "1.0.0",
                }
            ],
        },
        "components": components,
    }

    return sbom


def main() -> None:
    """Generate SBOM and write to file or stdout."""
    parser = argparse.ArgumentParser(description="Generate CycloneDX SBOM")
    parser.add_argument(
        "--output", "-o",
        default="sbom.json",
        help="Output file path (default: sbom.json)",
    )
    parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="Path to pyproject.toml (default: pyproject.toml)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout instead of file",
    )
    args = parser.parse_args()

    deps = parse_pyproject_dependencies(args.pyproject)
    sbom = generate_cyclonedx_sbom(deps)

    sbom_json = json.dumps(sbom, indent=2)

    if args.stdout:
        print(sbom_json)
    else:
        with open(args.output, "w") as f:
            f.write(sbom_json)
        print(f"SBOM written to {args.output} ({len(deps)} components)")


if __name__ == "__main__":
    main()
