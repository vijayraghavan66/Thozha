"""FCM HTTP v1 wake-up sender for the Mac host.

Messages are DATA-ONLY wake-ups: {"type": "link_wakeup"}. They never contain approval content, operation
text, keys, or commands. The phone fetches the authenticated, encrypted request through Jarvis Link.

Credentials: application-default credentials (Google's supported local discovery: GOOGLE_APPLICATION_CREDENTIALS
or `gcloud auth application-default login`). Access tokens are short-lived and minted per process; no service
account key is embedded in this repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"


class PushCredentialsUnavailable(RuntimeError):
    """No application-default credentials are configured. The message says what to do."""


@dataclass(frozen=True)
class WakeResult:
    outcome: str  # "sent" | "invalid_token" | "failed"
    http_status: int | None = None


class FcmSender:
    def __init__(self, project_id: str, *, credentials: Any | None = None, session: Any | None = None) -> None:
        self.project_id = project_id
        self._session = session
        if credentials is None:
            credentials = _default_credentials()
        self._credentials = credentials

    def _http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _access_token(self) -> str:
        import google.auth.transport.requests as ga_requests
        if not self._credentials.valid:
            self._credentials.refresh(ga_requests.Request())
        return self._credentials.token

    def wake(self, token: str) -> WakeResult:
        body = {"message": {"token": token, "data": {"type": "link_wakeup"},
                            "android": {"priority": "HIGH", "ttl": "300s"}}}
        response = self._http().post(
            FCM_URL.format(project=self.project_id), json=body, timeout=15,
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"},
        )
        if response.status_code == 200:
            return WakeResult("sent", 200)
        if response.status_code in (404, 400) and "UNREGISTERED" in response.text:
            return WakeResult("invalid_token", response.status_code)  # the phone must register again
        return WakeResult("failed", response.status_code)


def _default_credentials():
    import google.auth
    try:
        credentials, _project = google.auth.default(scopes=[FCM_SCOPE])
    except Exception as failure:  # DefaultCredentialsError and friends
        raise PushCredentialsUnavailable(
            "No Google application-default credentials. Run once: gcloud auth application-default login "
            "--scopes=https://www.googleapis.com/auth/firebase.messaging,https://www.googleapis.com/auth/cloud-platform"
        ) from failure
    return credentials
