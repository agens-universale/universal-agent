# Testing Anti-Patterns

Common ways tests fail to do their job. If your test smells like one of these,
fix the test before claiming GREEN.

## 1. Tests that pass without exercising the code

Symptom: comment out the implementation, test still passes.

Cause: the test asserts on input it constructed itself, or on a mock you set up,
without ever calling the function under test.

Fix: actually invoke the production code. Assert on its output.

## 2. Tests that pass on broken implementations

Symptom: a regression isn't caught.

Cause: assertions are too loose (e.g. `assert result is not None` for a function
that returns a dict — `None` was already impossible). Or the test mocks the
thing it's supposed to test.

Fix: assert on the SPECIFIC observable behavior. If the function returns
`{"status": "ok", "count": 5}`, assert on `count == 5`, not on truthiness.

## 3. Mocks that hide real bugs

Symptom: tests pass with mocks, production fails.

Cause: mock returns a shape the real dependency doesn't (e.g. dict where the
real thing returns an object), or mocks a function that has been renamed.

Fix: at boundaries, prefer integration tests that exercise real code paths.
Use mocks for external services (HTTP, databases) where running the real thing
is expensive — and verify the mock's shape matches reality periodically.

## 4. Brittle assertions

Symptom: test breaks on cosmetic changes (whitespace, log format, ordering).

Cause: asserting on exact text / total dict equality / ordered list when only
substring / specific field / unordered set matters.

Fix: assert on the minimal observable property. For text: substring or regex.
For dicts: specific keys. For lists where order is irrelevant: convert to set.

## 5. Tests with no RED phase

Symptom: written after the code, no proof the test would have caught the bug.

Cause: skipping the failing-first step "because it's obvious".

Fix: every new test goes through `pytest ... -v` BEFORE the
implementation exists. Verify the failure mode.

## 6. Sleeping instead of waiting

Symptom: flaky tests that pass on fast machines and fail on slow ones.

Cause: `time.sleep(1)` for async / network operations.

Fix: poll for the actual condition with a timeout. See condition-based-waiting
in the systematic-debugging references for the pattern.
