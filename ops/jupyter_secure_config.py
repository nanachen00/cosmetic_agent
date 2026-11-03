"""Security settings for the externally reachable JupyterLab server."""

from pathlib import Path


TOKEN_FILE = Path("/var/lib/jenkins/.jupyter_lab_token")
token = TOKEN_FILE.read_text(encoding="utf-8").strip()
if len(token) < 32:
    raise RuntimeError("Jupyter token is missing or too short")

c = get_config()  # noqa: F821 - provided by Jupyter at config load time
c.IdentityProvider.token = token
c.PasswordIdentityProvider.allow_password_change = False
c.ServerApp.allow_root = True
c.ServerApp.allow_remote_access = True
c.ServerApp.cookie_secret_file = "/var/lib/jenkins/.jupyter_cookie_secret_secure"
c.ServerApp.disable_check_xsrf = False
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.open_browser = False
c.ServerApp.port = 8889
c.ServerApp.port_retries = 0
c.ServerApp.root_dir = "/var/lib"
