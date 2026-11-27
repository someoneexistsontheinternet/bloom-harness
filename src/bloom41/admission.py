"""Bounded asynchronous admission with priority and periodic oldest-work service."""

import asyncio
from contextlib import asynccontextmanager


def choose(waiting, admissions, *, priority, age):
    best = min(waiting, key=lambda item: (priority(item), age(item)))
    if admissions % 4 == 3 and priority(best) > 0:
        return min(waiting, key=age)
    return best


class Admission:
    def __init__(self, concurrency):
        if concurrency < 1:
            raise ValueError("Model concurrency must be positive")
        self.limit = concurrency
        self.active = 0
        self.waiting = []
        self.admissions = 0
        self.sequence = 0

    def pump(self):
        while self.waiting and self.active < self.limit:
            ticket = choose(
                self.waiting,
                self.admissions,
                priority=lambda t: t["priority"],
                age=lambda t: t["sequence"],
            )
            self.waiting.remove(ticket)
            if ticket["ready"].cancelled():
                continue
            ticket["granted"] = True
            self.active += 1
            self.admissions += 1
            ticket["ready"].set_result(None)

    @asynccontextmanager
    async def slot(self, priority=1):
        self.sequence += 1
        ticket = {
            "priority": priority,
            "sequence": self.sequence,
            "ready": asyncio.get_running_loop().create_future(),
            "granted": False,
        }
        self.waiting.append(ticket)
        self.pump()
        try:
            await ticket["ready"]
            yield
        finally:
            if ticket["granted"]:
                self.active -= 1
            elif ticket in self.waiting:
                self.waiting.remove(ticket)
            self.pump()
