# Defense in Depth — where to validate

Multi-layer correctness without burying bugs.

## The principle

- **Validate at boundaries.** User input, external API responses, file contents,
  IPC messages. The boundary is where untrusted data enters trusted code.
- **Trust internal contracts.** Once data has been validated at the boundary,
  internal functions can rely on its shape. Re-validating everywhere adds noise
  and hides bugs (you can't tell which layer is supposed to catch what).
- **Fail fast at the boundary.** Reject malformed input with a clear error,
  don't try to "be helpful" by guessing the missing field.

## Anti-pattern: try-except everywhere

```python
def deep_function(x):
    try:
        return x["important_field"]
    except (KeyError, TypeError, AttributeError):
        return None  # "be defensive"
```

Problems:
- Hides the fact that `x` was malformed.
- Forces every caller to handle `None` as a valid return.
- Real bugs (e.g. typo in field name) get silently swallowed.

## Pattern: validate at boundary, trust internally

```python
def handle_request(raw):
    # boundary — explicit validation
    data = MyRequestSchema.model_validate(raw)
    # internal — data is now guaranteed shape
    return deep_function(data.important_field)

def deep_function(value: str):
    # value is str — no defensive checks needed
    return value.upper()
```

If the boundary validation rejects bad input early, internal code stays clean
and bugs surface at a known location.

## Where to put the boundary

- HTTP/CLI entry points: validate request schema with Pydantic / dataclasses.
- File loading: validate after parse (e.g. after `yaml.safe_load`).
- External API calls: validate response shape before passing further.
- Database read: validate row shape if the schema can drift (migrations,
  legacy data).

## Where NOT to add defense

- Between two functions in the same module — they share a trust boundary.
- After a Pydantic model has already validated — don't re-check the same fields.
- For impossible states ("what if this list is suddenly a dict") — let the
  exception fire and reveal the real bug.
