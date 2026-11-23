"""Playwright-only runner: real Web routes and isolated storage, deterministic engine."""

import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time


class FixtureEngine:
    def __init__(self, buffered):
        self.engine_id = "codex_sdk_fixture" if buffered else "streaming_fixture"
        self.buffered = buffered

    @staticmethod
    def health():
        return True

    @staticmethod
    def generate(messages, *, tools, **_options):
        assert not tools
        prompt = messages[-1].content
        if prompt == "Check the snapshot fallback":
            time.sleep(2)
        if "check prior context" in prompt:
            content = "Context found." if any("Thozha Alpha" in item.content for item in messages[:-1]) else "No prior context."
        else:
            content = "## Fixture response\n\nThis response uses the isolated **test engine**.\n\n```ts\nconst workspace = 'Thozha Alpha';\n```"
        return {"content": content}

    async def stream(self, messages, *, tools, **options):
        assert not tools
        if messages[-1].content == "Check cancellation behavior":
            await asyncio.sleep(3)
        if messages[-1].content == "Check generation failure":
            raise RuntimeError("deterministic fixture failure")
        answer = self.generate(messages, tools=tools, **options)["content"]
        if self.buffered:
            await asyncio.sleep(0.8)
            yield answer
        else:
            midpoint = answer.index("\n") if "\n" in answer else len(answer) // 2
            yield answer[:midpoint]
            await asyncio.sleep(0.8)
            yield answer[midpoint:]


def main():
    password = os.environ["THOZHA_E2E_PASSWORD"]
    origin = os.environ["THOZHA_E2E_ORIGIN"]
    buffered = os.environ.get("THOZHA_E2E_MODE") == "buffered"
    port = int(os.environ["THOZHA_E2E_API_PORT"])
    temporary = TemporaryDirectory(prefix="thozha-web-e2e-")
    with temporary as root:
        # Never inherit live providers, credentials, stores or the user's home.
        path = os.environ.get("PATH", os.defpath)
        os.environ.clear()
        os.environ.update({
            "PATH": path,
            "HOME": root,
            "OPENJARVIS_HOME": str(Path(root, "workspace")),
            "THOZHA_WEB_OWNER_ID": "web-e2e-owner",
            "THOZHA_WEB_ORIGIN": origin,
            "THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV": "1",
            "THOZHA_WEB_LOGIN_ATTEMPTS": "100",
        })
        os.umask(0o077)
        sys.dont_write_bytecode = True
        backend = Path(__file__).resolve().parents[2] / "openjarvis" / "src"
        sys.path.insert(0, str(backend))
        import uvicorn
        from openjarvis.server.app import create_app
        from openjarvis.server.thozha_web_auth import hash_password

        os.environ["THOZHA_WEB_PASSWORD_HASH"] = hash_password(password)
        app = create_app(FixtureEngine(buffered), "frontend-test-model")
        # Uvicorn can re-raise SIGTERM after ASGI shutdown.
        app.router.add_event_handler("shutdown", temporary.cleanup)
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, proxy_headers=False, log_level="warning")


if __name__ == "__main__":
    main()
