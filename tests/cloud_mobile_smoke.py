"""Opt-in smoke test. Only a newly created synthetic user's records are touched."""

import argparse
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = "https://wakemate-api-4-0-qtgs.onrender.com"


def call(path, payload=None, method=None):
    request = Request(BASE + path, method=method or ("POST" if payload is not None else "GET"),
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-cloud", action="store_true")
    if not parser.parse_args().run_cloud:
        parser.error("Explicit --run-cloud is required")
    token = uuid4().hex[:16]
    status, user = call("/users/", {"name": "codex_mobile_regression_" + token,
        "email": "codex_mobile_" + token + "@example.com", "password": secrets.token_urlsafe(24)})
    assert status == 200, "Synthetic user creation failed"
    user_id = user["user_id"]
    results = []
    zone = timezone(timedelta(hours=8))
    day = datetime.now(zone).date() + timedelta(days=2)

    def time(hour, offset=0):
        return datetime.combine(day + timedelta(days=offset), datetime.min.time(), zone).replace(hour=hour).isoformat()

    def payload(*periods):
        return {"user_id": user_id, "day": day.isoformat(),
                "periods": [{"start": start, "end": end} for start, end in periods]}

    def passed(name):
        results.append(name)
        print(json.dumps({"passed": name}), flush=True)

    try:
        sleep = payload((time(23, -1), time(7)))
        status, first = call("/users_sleep/replace_day", sleep)
        assert status == 200 and len(first["ids"]) == 1
        status, retry = call("/users_sleep/replace_day", sleep)
        assert status == 200 and retry["ids"] == first["ids"] and not retry["changed"]
        passed("sleep_retry_is_idempotent")

        wake = payload((time(9), time(10)), (time(18), time(19)))
        status, first = call("/users_wake/replace_day", wake)
        assert status == 200 and len(first["ids"]) == 2
        status, retry = call("/users_wake/replace_day", wake)
        assert status == 200 and retry["ids"] == first["ids"]
        passed("multi_slot_wake_retry_is_idempotent")

        status, _ = call("/users_wake/replace_day", payload((time(6), time(8)), (time(20), time(21))))
        assert status == 400
        status, active = call("/users_wake/?user_id=" + user_id)
        assert status == 200 and sorted(row["id"] for row in active) == sorted(first["ids"])
        passed("rejected_wake_replacement_preserves_all_originals")

        status, _ = call("/users_sleep/replace_day", payload((time(23, -1), time(11))))
        assert status == 400
        status, active = call("/users_sleep/?user_id=" + user_id)
        assert status == 200 and len(active) == 1
        assert datetime.fromisoformat(active[0]["sleep_end_time"].replace("Z", "+00:00")).astimezone(zone).date() == day
        passed("rejected_sleep_replacement_preserves_original")

        pvt = {"user_id": user_id, "mean_rt": 320, "lapses": 1, "false_starts": 0,
               "kss_level": 3, "device": "Mobile PVT-B 3min",
               "test_at": datetime.now(timezone.utc).isoformat()}
        status, first = call("/users_pvt/", pvt)
        assert status == 200
        status, retry = call("/users_pvt/", pvt)
        assert status == 200 and first["id"] == retry["id"]
        passed("pvt_retry_is_idempotent")
    finally:
        remaining = 0
        for endpoint in ("users_wake", "users_sleep"):
            status, records = call(f"/{endpoint}/?user_id=" + user_id)
            assert status == 200
            for record in records:
                status, _ = call(f"/{endpoint}/{record['id']}", method="DELETE")
                assert status == 200
            status, records = call(f"/{endpoint}/?user_id=" + user_id)
            assert status == 200
            remaining += len(records)
        assert remaining == 0
        passed("synthetic_source_records_cleaned")
    print(json.dumps({"passed_count": len(results), "results": results}))


if __name__ == "__main__":
    main()
