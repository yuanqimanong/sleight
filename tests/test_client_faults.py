import httpx
import pytest

from sleight.client import ServiceClient
from sleight.core import errors


@pytest.mark.parametrize("error,closed", [(errors.LeaseLost, True), (errors.SessionLost, True),
                                         (errors.ElementError, False)])
def test_remote_fault_updates_session_liveness_without_replaying_action(error, closed):
    actions = []

    def respond(request):
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"api_version": 1})
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(200, json={"id": "fixture", "state": "running"})
        if request.url.path.endswith("/call"):
            actions.append(request)
            return httpx.Response(409, json={"detail": {"error": error.__name__, "message": "fixture fault"}})
        return httpx.Response(200, json={"ok": True})

    with ServiceClient("http://fixture", "test-token", transport=httpx.MockTransport(respond)) as client:
        session = client.session("profile")
        try:
            with pytest.raises(error, match="fixture fault"):
                session.open("https://example.com/")
            assert session.closed is closed
            assert session.transport.closed is closed
            if closed:
                with pytest.raises(errors.SessionLost):
                    session.open("https://example.com/")
            assert len(actions) == 1
        finally:
            session.close()
