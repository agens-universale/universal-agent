"""Short timeout test clock; no retry policy or production behavior changes."""

from dataclasses import replace
from datetime import timedelta

from temporalio.worker import (
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)


class ShortActivityTimeout(WorkflowOutboundInterceptor):
    def start_activity(self, input):
        return self.next.start_activity(
            replace(input, start_to_close_timeout=timedelta(seconds=3))
        )


class RestartWorkflowInbound(WorkflowInboundInterceptor):
    def init(self, outbound):
        self.next.init(ShortActivityTimeout(outbound))


class RestartInterceptor(Interceptor):
    def workflow_interceptor_class(self, input):
        return RestartWorkflowInbound
