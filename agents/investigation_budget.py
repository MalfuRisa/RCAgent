"""One monotonic delivery budget; callers never reset it per role or task."""
import math
import time
from contextvars import ContextVar


class PhaseExpired(TimeoutError):
    def __init__(self, phase, reason=None):
        self.phase = phase
        self.reason = reason or "budget_" + phase
        super().__init__("Run budget phase expired: " + phase)


class RunBudget:
    def __init__(self, deadline=None, *, total=300, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        wall = time.time()
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= wall):
            raise ValueError("deadline must be finite future Unix seconds, not bool")
        self.total = min(total, deadline - wall) if deadline is not None else total
        self.source = "user_deadline" if deadline is not None and deadline - wall < total else "default_300_seconds"

    def remaining(self, phase="delivery"):
        ratio = {"investigation": 2/3, "generation": .9, "delivery": 1}[phase]
        return max(0, self.total * ratio - (self.clock() - self.started))

    def check(self, phase):
        if self.remaining(phase) <= 0:
            raise PhaseExpired(phase, self.phase_reason(phase))

    def phase_reason(self, phase):
        if self.remaining("delivery") <= 0 and self.source == "user_deadline":
            return "deadline"
        return "budget_" + ("delivery" if self.remaining("delivery") <= 0 else phase)

    def card(self):
        return {"total_seconds": self.total, "source": self.source, "clock": "monotonic",
            "investigation_until_seconds": self.total*2/3, "generation_until_seconds": self.total*.9}


ENTRY_BUDGET = ContextVar("rcagent_entry_budget", default=None)
