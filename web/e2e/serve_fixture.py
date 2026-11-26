"""Playwright loopback: real Web routes and isolated storage. No live device credentials."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time


class FixtureEngine:
    def __init__(self, buffered, control):
        self.engine_id = "codex_sdk" if control else "codex_sdk_fixture" if buffered else "streaming_fixture"
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


def pair_test_device(store, name):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())

    def sign(challenge):
        return base64.b64encode(key.sign(challenge.encode(), ec.ECDSA(hashes.SHA256()))).decode()

    invitation, challenge, _ = store.begin_device_pairing()
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    store.complete_device_pairing(invitation, base64.b64encode(public).decode(), sign(challenge), device_name=name)
    if store.decide_device_pairing(invitation, "approve") != "APPROVED":
        raise RuntimeError("Pairing the isolated test identity failed")
    claim = store.device_pairing_status(invitation)["claim_challenge"]
    # Never retain or return the issued companion bearer.
    return store.claim_device_token(invitation, sign(claim), ttl_days=1)["device_id"]


def seed_archive(store, device_id):
    from openjarvis.server.supervisor_routes import _artifact_store

    scope, key = ["development"], "web-fixture-archive-key"
    run = store.create_link_task("Publish a synthetic test archive.", capability_scope=scope,
                                 idempotency_key=key, owner_actor=f"device:{device_id}")
    task_id = run["task_id"]
    store.start_link_task(task_id, objective_digest=run["objective_digest"],
                          objective_text_digest=run["objective_digest"], capability_scope=scope,
                          idempotency_key=key)
    files = {"fixture.txt": b"Thozha Web artifact fixture; not a live build.\n"}
    manifest = [{"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for path, data in files.items()]
    manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _artifact_store().create_bundle(task_id=task_id, device_id=device_id, export={
        "bytes": sum(map(len, files.values())), "manifest_sha256": manifest_hash,
        "file_manifest": manifest, "payloads": files,
    })
    store.finish_link_task(task_id, result_text="Synthetic archive fixture")
    store.transition(task_id, "VERIFYING", expected="RUNNING")
    store.transition(task_id, "COMPLETED", expected="VERIFYING")


class FixtureAgent:
    """The real worker runs this inert agent; no Mac actions or provider calls."""

    _model = "frontend-test-model"

    def run(self, text, *, context=None):
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.tasks.supervisor import current_task_context, get_supervisor_store

        task = current_task_context()
        if task is None:
            raise RuntimeError("Fixture agent requires a supervisor task")
        if text.startswith("Fixture approval:"):
            get_supervisor_store().create_approval(
                task.task_id, "mcp:jarvis_mac:click", "screen-coordinate", {"x": 1, "y": 1},
                "Inert browser test; no Mac action will run.",
            )
            return AgentResult(content="Waiting for fixture approval.")
        if text == "Fixture supervisor failure":
            raise RuntimeError("Deterministic fixture supervisor failure")
        if text == "Fixture slow task":
            time.sleep(5)
        else:
            time.sleep(0.8)
        return AgentResult(content=f"Test engine only: {text}")


def main():
    password = os.environ["THOZHA_E2E_PASSWORD"]
    origin = os.environ["THOZHA_E2E_ORIGIN"]
    buffered = os.environ.get("THOZHA_E2E_MODE") == "buffered"
    control = os.environ.get("THOZHA_E2E_CONTROL") == "1"
    ollama_port = os.environ.get("THOZHA_E2E_OLLAMA_PORT")
    if control and ollama_port:
        raise SystemExit("Fixture supervisor execution and Ollama-only verification are mutually exclusive.")
    port = int(os.environ["THOZHA_E2E_API_PORT"])
    backend_root = Path(os.environ.get("THOZHA_E2E_BACKEND", Path(__file__).resolve().parents[2])).resolve()
    backend = backend_root / "openjarvis" / "src"
    if not (backend / "openjarvis" / "server" / "app.py").is_file():
        raise SystemExit("THOZHA_E2E_BACKEND must point to the Thozha backend checkout root")
    temporary = TemporaryDirectory(prefix="thozha-web-e2e-")
    with temporary as root:
        root = str(Path(root).resolve())
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
        sys.path.insert(0, str(backend))
        import uvicorn
        from openjarvis.server.app import create_app
        from openjarvis.server.thozha_web_auth import hash_password

        os.environ["THOZHA_WEB_PASSWORD_HASH"] = hash_password(password)
        if control or ollama_port:
            from openjarvis.tasks.supervisor import get_supervisor_store

            store = get_supervisor_store()
            primary = pair_test_device(store, "Isolated test pairing")
            mapped = [primary]
            if control:
                from openjarvis.engine import codex_sdk

                mapped.append(pair_test_device(store, "Revocation test pairing"))
                seed_archive(store, primary)

                def inert_mcp(tool, arguments, mode, **kwargs):
                    if (tool, arguments, mode) != ("click", {"x": 1, "y": 1}, "control"):
                        raise RuntimeError("Unexpected fixture operation; no real executor is enabled")
                    return {"isError": False, "content": [{"type": "text", "text": "Inert approval fixture (no Mac operation)"}]}

                codex_sdk._call_local_mcp = inert_mcp
            os.environ["THOZHA_WEB_ALLOWED_DEVICE_IDS"] = ",".join(mapped)
            os.environ["THOZHA_WEB_AUTOMATION_ENABLED"] = "1"

        if ollama_port:
            from openjarvis.engine.ollama import OllamaEngine

            engine = OllamaEngine(host=f"http://127.0.0.1:{int(ollama_port)}", timeout=120)
            app = create_app(engine, "qwen3.5:4b")
        else:
            app = create_app(
                FixtureEngine(buffered, control), "frontend-test-model",
                agent=FixtureAgent() if control else None,
            )
        # Uvicorn can re-raise SIGTERM after ASGI shutdown.
        app.router.add_event_handler("shutdown", temporary.cleanup)
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, proxy_headers=False, log_level="warning")


if __name__ == "__main__":
    main()
