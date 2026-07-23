"""Rebuild summary.json and report.md for an existing Experiment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.experiments.aggregate import aggregate_experiment  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--pass-threshold", type=float)
    parser.add_argument(
        "--experiments-root", type=Path, default=REPO_ROOT / "data" / "experiments"
    )
    args = parser.parse_args()
    experiment_dir = args.experiments_root / args.experiment_id
    summary = aggregate_experiment(
        experiment_dir, pass_threshold=args.pass_threshold
    )
    print(json.dumps({
        "experiment_id": summary["experiment_id"],
        "attempts": summary["attempts"],
        "summary": str(experiment_dir / "summary.json"),
        "report": str(experiment_dir / "report.md"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
