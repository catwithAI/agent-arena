"""Run or resume a batch Experiment through the public Agent Arena API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.experiments.aggregate import aggregate_experiment  # noqa: E402
from backend.experiments.runner import ExperimentRunner, load_config  # noqa: E402


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "experiment.yaml")
    parser.add_argument("--resume", metavar="EXPERIMENT_ID")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--experiments-root", type=Path, default=REPO_ROOT / "data" / "experiments"
    )
    args = parser.parse_args()
    config = load_config(args.config)
    runner = ExperimentRunner(
        config,
        experiments_root=args.experiments_root,
        experiment_id=args.resume,
        retry_failed=args.retry_failed,
    )
    result = await runner.run()
    summary = aggregate_experiment(
        runner.experiment_dir, pass_threshold=config.pass_threshold
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"summary: {runner.experiment_dir / 'summary.json'}")
    print(f"report:  {runner.experiment_dir / 'report.md'}")
    if any(
        status not in {"completed"}
        for status in result.get("statuses", {})
    ):
        print("experiment completed with non-successful jobs; use --retry-failed to rerun them")
    print(f"attempts aggregated: {summary['attempts']}")


if __name__ == "__main__":
    asyncio.run(_main())
