"""Serializable interruptions of the existing worker-side guard chain."""

import hashlib
from dataclasses import asdict

from gc_core.execution.human import HumanRequest

from gc_ua_temporalio.storage.sqlite import encode


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class HumanWait(BaseException):
    def __init__(self, request, request_digest):
        self.request = request
        self.digest = request_digest


class JudgeWait(BaseException):
    def __init__(self, request, request_digest):
        self.request = request
        self.digest = request_digest


class EffectPlanned(BaseException):
    def __init__(self, call):
        self.call = call


class Decisions:
    def __init__(self, fingerprint, decisions, judges=()):
        self.fingerprint = fingerprint
        self.answers = {d.digest: d.answer for d in decisions}
        self.judges = {d.digest: d.result for d in judges}
        self.sequence = 0
        self.model_sequence = 0

    def request(self, request):
        self.sequence += 1
        key = digest([self.fingerprint, self.sequence, asdict(request)])
        if key not in self.answers:
            raise HumanWait(request, key)
        return self.answers[key]

    def ask(self, question):
        return self.request(HumanRequest(kind="question", question=question))

    def next_operation_id(self, kind):
        self.model_sequence += 1
        return f"{kind}-{self.model_sequence}"

    async def model(self, request):
        key = digest([self.fingerprint, asdict(request)])
        if key not in self.judges:
            raise JudgeWait(request, key)
        return self.judges[key]


class PlanningCommandBackend:
    """Capture the exact exec port call, after the original guards and validation."""

    def __init__(self, backend, expected=None):
        self.backend = backend
        self.expected = expected

    def __getattr__(self, name):
        return getattr(self.backend, name)

    async def _exec(self, method, payload, options):
        call = {"method": method, "payload": payload, "options": options}
        if self.expected is None:
            raise EffectPlanned(call)
        if call != self.expected:
            raise EffectPlanned(call)
        # No Exception-to-string conversion is allowed after starting a process.
        from gc_ua_temporalio.storage.guarded_file import FileEffectUnknown

        try:
            return await getattr(self.backend, method)(payload, **options)
        except Exception as exc:
            raise FileEffectUnknown(type(exc).__name__) from None

    async def aexec(self, command, **options):
        return await self._exec("aexec", command, options)

    async def arun_python(self, code, **options):
        return await self._exec("arun_python", code, options)

    async def aexec_argv(self, argv, **options):
        return await self._exec("aexec_argv", argv, options)
