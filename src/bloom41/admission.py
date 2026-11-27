"""Actor scheduling with priority and periodic oldest-work service."""


def choose(waiting, admissions, *, priority, age):
    best = min(waiting, key=lambda item: (priority(item), age(item)))
    if admissions % 4 == 3 and priority(best) > 0:
        return min(waiting, key=age)
    return best
