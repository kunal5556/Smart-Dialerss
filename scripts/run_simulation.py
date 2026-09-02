import argparse
import asyncio
import sys

from app.config import get_settings
from app.db import connect, disconnect, get_simulation_db
from app.db_indexes import ensure_indexes
from app.logging_config import configure_logging
from app.models.enums import DialingMode
from app.simulation.engine import SimulationEngine, SimulationReport
from app.simulation.report import comparison_table, write_report
from app.simulation.scenarios import SCENARIO_NAMES, build_scenario

MODE_CHOICES = ("progressive", "predictive", "both")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a SmartDialer simulation scenario.")
    parser.add_argument("--scenario", choices=SCENARIO_NAMES, default="A")
    parser.add_argument("--mode", choices=MODE_CHOICES, default="both")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--agents", type=int, default=20)
    parser.add_argument("--borrowers", type=int, default=500)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--time-scale", type=float, default=30.0)
    return parser.parse_args(argv)


def modes_for(choice: str) -> list[DialingMode]:
    if choice == "both":
        return [DialingMode.PROGRESSIVE, DialingMode.PREDICTIVE]
    return [DialingMode(choice.upper())]


async def run(args: argparse.Namespace) -> list[SimulationReport]:
    configure_logging()
    await connect()
    try:
        database = get_simulation_db()
        await ensure_indexes(database)
        engine = SimulationEngine(database, get_settings())

        reports = []
        for mode in modes_for(args.mode):
            config = build_scenario(
                scenario=args.scenario,
                mode=mode,
                agents=args.agents,
                borrowers=args.borrowers,
                duration_seconds=args.duration,
                seed=args.seed,
                time_scale=args.time_scale,
            )
            from dataclasses import replace

            config = replace(config, worker_count=args.workers)
            report = await engine.run(config)
            path = write_report(report)
            print(f"Wrote {path}")
            reports.append(report)
        return reports
    finally:
        await disconnect()


def main() -> int:
    args = parse_args()
    reports = asyncio.run(run(args))
    print()
    print(comparison_table(reports))
    return 0 if all(report.passed for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
