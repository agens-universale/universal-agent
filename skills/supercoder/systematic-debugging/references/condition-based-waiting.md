# Condition-Based Waiting — fixing flaky async bugs

The wrong fix for "sometimes it fails" is `time.sleep(2)`. Sleeps mask timing
bugs; they don't fix them. They also make tests slow.

## The principle

Wait for a CONDITION, not a duration. Poll the actual state at a short
interval, with a maximum total timeout.

## Pattern

```python
import time

def wait_for(predicate, *, timeout=5.0, interval=0.05, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {message}")

# usage
wait_for(lambda: order_status(order_id) == "completed",
         timeout=10.0, message="order completion")
```

## Why not just `sleep(N)`?

- **Flaky:** N too small → test fails on slow machine. N too large → slow.
- **Hides bug:** if the system NEVER reaches the state, the sleep returns
  anyway and the next assertion fails with a confusing error.
- **No diagnostic:** failure looks like "the function returned wrong value",
  not "the function timed out waiting for state X".

## When sleep IS acceptable

- Throttling outgoing requests to a rate-limited API.
- Demoing a feature with a deliberate pause for visibility.
- NOT for "wait until ready" — always use a condition.

## Test version

```python
def test_order_completes():
    place_order(order_id)
    wait_for(lambda: order_status(order_id) == "completed",
             timeout=2.0, message="order completion")
    assert get_order(order_id).total > 0
```

If the test times out, the message tells you exactly what wasn't happening. If
the order completes immediately, the test passes immediately. No fixed sleep.

## In production code

Same pattern. Wait for downstream readiness, retry with exponential backoff
for transient failures, give up after a known timeout. Never block on a fixed
sleep that you guessed at.
