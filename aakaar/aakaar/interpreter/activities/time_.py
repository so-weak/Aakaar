"""time.* — clock primitives for the planner.

`time.now` lets a workflow reference *today* without the planner baking
a literal date into the DAG. Resolution happens at run time so a saved
workflow keeps working tomorrow.

Output keys are deliberately verbose so a planner-generated reference
like `${now.ist_date}` reads obvious in saved DAGs without needing
documentation lookups.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext

_IST = timezone(timedelta(hours=5, minutes=30))


async def now(_ctx: ActivityContext, _inputs: dict[str, Any]) -> dict[str, Any]:
    utc = datetime.now(UTC)
    ist = utc.astimezone(_IST)
    return {
        "ist_date": ist.strftime("%Y-%m-%d"),
        "ist_datetime": ist.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "utc_date": utc.strftime("%Y-%m-%d"),
        "utc_datetime": utc.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def register_into(reg: ActivityRegistry) -> None:
    reg.register("time.now", now)
