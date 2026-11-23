"""Start the reviewed OpenJarvis app in a separate, loopback-only Web profile."""

import os
from pathlib import Path
import secrets
import stat
import sys


def main():
    if len(sys.argv) != 5:
        raise SystemExit("Usage: serve-local.py PROFILE WEB_PORT API_PORT OLLAMA_PORT")
    profile = Path(sys.argv[1]).resolve()
    web, api, ollama = (int(port) for port in sys.argv[2:])
    if len({web, api, ollama}) != 3 or not all(1024 <= p <= 65535 for p in (web, api, ollama)):
        raise SystemExit("Provide three distinct unprivileged ports.")
    os.umask(0o077)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    if profile.stat().st_uid != os.getuid() or stat.S_IMODE(profile.stat().st_mode) != 0o700:
        raise SystemExit("The dedicated Web profile must be owned by you and mode 0700.")

    checkout = Path(__file__).resolve().parents[2] / "openjarvis" / "src"
    sys.path.insert(0, str(checkout))
    os.environ.clear()
    os.environ.update({
        "PATH": os.defpath,
        "HOME": str(profile),
        "OPENJARVIS_HOME": str(profile / "openjarvis"),
        "THOZHA_WEB_OWNER_ID": "thozha-local",
        "THOZHA_WEB_ORIGIN": f"http://127.0.0.1:{web}",
        "THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV": "1",
    })

    import uvicorn
    from openjarvis.engine.ollama import OllamaEngine
    from openjarvis.server.app import create_app
    from openjarvis.server.thozha_web_auth import hash_password

    credential = profile / "owner-password"
    try:
        with credential.open("x") as file:
            file.write(secrets.token_urlsafe(24) + "\n")
    except FileExistsError:
        info = credential.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("The existing Web password file must be a regular owner-only file (0600).")
    os.environ["THOZHA_WEB_PASSWORD_HASH"] = hash_password(credential.read_text().strip())
    engine = OllamaEngine(host=f"http://127.0.0.1:{ollama}", timeout=120)
    app = create_app(engine, "qwen3.5:4b")
    uvicorn.run(app, host="127.0.0.1", port=api, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
