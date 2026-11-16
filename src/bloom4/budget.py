"""Durable admission budgets across all owners, helpers, failures and compaction."""

from .context import estimate
from .store import Store, encoded


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, store: Store):
        self.store = store

    def grant(self, calls=24, output_tokens=131072, input_tokens=8_000_000):
        if min(calls, output_tokens, input_tokens) < 1:
            raise ValueError("Budgets must be positive")
        return self.store.append("budget.granted", {"calls": calls, "output_tokens": output_tokens,
            "input_tokens": input_tokens}, actor="owner")

    def status(self):
        grants = self.store.events("budget.granted")
        if not grants:
            return {"grant": None, "remaining_calls": 0}
        grant = grants[-1]
        calls = [e for e in self.store.events("model.started") if e["data"]["budget"] == grant["id"]]
        complete = {e["data"]["call"]: e for e in self.store.events("model.completed")}
        output, inputs, unknown = 0, 0, 0
        for call in calls:
            usage = complete.get(call["id"], {}).get("data", {}).get("usage", {}) or {}
            actual_output, actual_input = usage.get("output_tokens"), usage.get("input_tokens")
            valid_output = type(actual_output) is int and actual_output >= 0
            valid_input = type(actual_input) is int and actual_input >= 0
            unknown += int(not valid_output or not valid_input)
            output += actual_output if valid_output else call["data"]["output_reserve"]
            inputs += actual_input if valid_input else call["data"]["input_estimate"]
        return {"grant": grant["id"], "limits": grant["data"], "calls": len(calls),
                "remaining_calls": grant["data"]["calls"] - len(calls),
                "accounted_output": output, "accounted_input": inputs, "unknown_usage_calls": unknown}

    def reserve(self, agent, task, request, purpose="turn", output_reserve=32768, *, wire_request=None):
        with self.store.locked():
            status = self.status()
            tokens = estimate(request)
            if (not status["grant"] or status["remaining_calls"] <= 0 or
                status["accounted_output"] + output_reserve > status["limits"]["output_tokens"] or
                status["accounted_input"] + tokens > status["limits"]["input_tokens"]):
                raise BudgetExceeded("Explicit model-call budget exhausted; grant a new allowance to continue")
            ref = self.store.put(encoded(request if wire_request is None else wire_request), "application/json")
            return self.store.append("model.started", {"request": ref, "purpose": purpose,
                "budget": status["grant"], "output_reserve": output_reserve, "input_estimate": tokens,
                "model": request["model"]}, target=agent, task=task)
