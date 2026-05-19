# Root Cause Tracing

Walk from symptom to root by asking "what caused THIS?" at each step.

## The technique

1. Start at the symptom (the observed wrong behavior).
2. Ask: "What is the immediate cause of THIS state?" The answer is one
   step up the chain.
3. Repeat for the new state. Each "why" peels back one layer.
4. Stop when you reach:
   - Code under your control that you can change, OR
   - A documented contract / spec that's being violated, OR
   - A boundary you've actually verified (not assumed) is outside your control.

## Example

Symptom: "API returns 500 on POST /items".

- What caused the 500? → Server raised `KeyError('user_id')` while building the
  response.
- What caused the KeyError? → The `user` dict didn't have a `user_id` field.
- What caused the missing field? → The `get_user_from_session()` function
  returned a partial object when the session was anonymous.
- What caused the partial object? → The session middleware was written
  assuming all requests have authenticated users; the new public endpoint
  doesn't authenticate.

**Root cause:** middleware contract mismatch with the new endpoint, NOT the
KeyError. Fix: either require authentication on the endpoint, or change the
middleware to return a fully-typed anonymous user.

**Wrong fix (symptom-level):** `user.get('user_id', 'anon')` — hides the
contract violation, will resurface elsewhere.

## When to stop walking

- "It's the framework's bug" — only after you've actually read the framework's
  source and confirmed.
- "The OS / hardware caused it" — almost never the root cause for normal app bugs.
- "User input was malformed" — fine, but then the root cause is "we didn't
  validate at the boundary" — fix the validation.

## Anti-patterns

- Stopping at the first layer ("the function returned None, so I added a None
  check"). You've fixed a symptom, not the cause.
- Skipping layers ("I don't need to understand why, I'll just try-except").
  Buries the bug for someone else to find later.
- Going too far ("the universe being entropic causes everything"). Stop at
  the level you can change.
