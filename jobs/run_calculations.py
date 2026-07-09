"""Run WAKEMATE recommendation and alertness calculations once.

This file is intended for Render Cron Job execution.
"""

from core.alertness_data import run_alertness_data
from core.caffeine_recommendation import run_caffeine_recommendation
from core.database import get_db_connection


def main() -> None:
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Could not connect to database")

    try:
        print("[cron] Running caffeine recommendation")
        run_caffeine_recommendation(conn)

        print("[cron] Running alertness data calculation")
        run_alertness_data(conn)

        print("[cron] Calculation batch finished")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
