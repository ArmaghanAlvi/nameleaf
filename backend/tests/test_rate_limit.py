"""Stage 3c: the in-process sliding-window limiter."""
from app.middleware.rate_limit import RateRule, SlidingWindowLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


RULE = RateRule("/explore-v2", 3, 60)


def test_allows_up_to_limit_then_blocks():
    lim = SlidingWindowLimiter(Clock())
    assert [lim.check(RULE, "1.1.1.1") for _ in range(3)] == [None, None, None]
    retry = lim.check(RULE, "1.1.1.1")
    assert retry is not None and 0 < retry <= 60


def test_window_slides_open_again():
    clock = Clock()
    lim = SlidingWindowLimiter(clock)
    for _ in range(3):
        lim.check(RULE, "1.1.1.1")
    clock.t += 30
    assert lim.check(RULE, "1.1.1.1") is not None   # still inside the window
    clock.t = 1060.0
    assert lim.check(RULE, "1.1.1.1") is None       # oldest hits aged out


def test_clients_and_rules_are_independent():
    lim = SlidingWindowLimiter(Clock())
    for _ in range(3):
        lim.check(RULE, "1.1.1.1")
    assert lim.check(RULE, "2.2.2.2") is None
    assert lim.check(RateRule("/senses/lookup", 3, 60), "1.1.1.1") is None
