import argparse
import asyncio
import json

from app.db.session import engine
from app.jobs.registry import run_registered_job


async def main_async(job_name: str, payload: dict[str, str]) -> None:
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with engine.begin() as conn:
        result = await run_registered_job(job_name, conn, payload)
    print(json.dumps(result, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_name")
    parser.add_argument("--competition", help="Competition season slug configured in the canonical catalog.")
    args = parser.parse_args()
    payload = {"competition": args.competition} if args.competition else {}
    asyncio.run(main_async(args.job_name, payload))


if __name__ == "__main__":
    main()
