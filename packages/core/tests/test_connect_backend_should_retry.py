import requests

from gc_core.utils.connect_backend import _default_should_retry_backend_exception


def _http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(response=resp)


def test_transient_statuses_are_retried():
    # 404: the cluster LLM proxy answers 404 while a model route is missing after a
    # tunnel flap — transient, the route comes back within the retry budget.
    for code in (404, 429, 500, 503):
        assert _default_should_retry_backend_exception(_http_error(code)) is True


def test_client_errors_are_not_retried():
    for code in (400, 401, 403, 422):
        assert _default_should_retry_backend_exception(_http_error(code)) is False
