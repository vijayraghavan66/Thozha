"""FCM HTTP v1 sender: payload shape, token handling, and credential failures (no network)."""
import pytest

from jarvis_link_host.push import FCM_URL, FcmSender, PushCredentialsUnavailable


class FakeCredentials:
    valid = True
    token = "ya29.test-short-lived"

    def refresh(self, _request):  # pragma: no cover - not reached while valid
        self.valid = True


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, json, timeout, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


def test_wake_is_data_only_and_carries_no_approval_content():
    session = FakeSession(FakeResponse(200, "{}"))
    result = FcmSender("jarvis-companion-fe01e", credentials=FakeCredentials(), session=session).wake("t" * 150)
    assert result.outcome == "sent"
    call = session.calls[0]
    assert call["url"] == FCM_URL.format(project="jarvis-companion-fe01e")
    assert call["json"]["message"]["data"] == {"type": "link_wakeup"}
    assert "notification" not in call["json"]["message"]
    assert call["headers"]["Authorization"] == "Bearer ya29.test-short-lived"


@pytest.mark.parametrize("status,text,expected", [
    (404, '{"error":{"details":[{"errorCode":"UNREGISTERED"}]}}', "invalid_token"),
    (400, "INVALID_ARGUMENT", "failed"),
    (500, "", "failed"),
])
def test_token_and_failure_outcomes(status, text, expected):
    session = FakeSession(FakeResponse(status, text))
    assert FcmSender("p", credentials=FakeCredentials(), session=session).wake("t" * 150).outcome == expected


def test_missing_application_default_credentials_explain_the_setup_step(monkeypatch):
    import jarvis_link_host.push as push
    monkeypatch.setattr(push, "_default_credentials", lambda: (_ for _ in ()).throw(PushCredentialsUnavailable("setup")))
    with pytest.raises(PushCredentialsUnavailable):
        FcmSender("p")
