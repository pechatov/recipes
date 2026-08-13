from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[2]


class BrowserLoginDeploymentContractTests(SimpleTestCase):
    def test_gateway_is_restarted_after_browser_controller_recovery(self):
        script = (ROOT / "scripts/connect-hermes-cart-pi.sh").read_text()

        dependency = "BindsTo=recipes-browser-login.service"
        recovery_health = (
            "ExecStartPost=/usr/bin/curl --fail --silent --show-error --retry 30 "
            "--retry-delay 1 --retry-connrefused"
        )
        restart_gateway = (
            "ExecStartPost=/usr/bin/systemctl --user --no-block start "
            "hermes-gateway-{profile}.service"
        )

        self.assertIn(dependency, script)
        self.assertLess(script.index(recovery_health), script.index(restart_gateway))

    def test_browser_close_revokes_existing_websockets_before_remote_close(self):
        server = (ROOT / "scripts/browser-login-server.mjs").read_text()

        begin = server.index("async function closeActiveSession")
        end = server.index("function issueAccess", begin)
        close_function = server[begin:end]

        remote_close = close_function.index("await closeCamofoxUser")
        self.assertLess(close_function.index("session.closing = true"), remote_close)
        self.assertLess(close_function.index("socket.destroy()"), remote_close)

    def test_only_the_latest_unused_access_token_is_retained(self):
        server = (ROOT / "scripts/browser-login-server.mjs").read_text()

        begin = server.index("function issueAccess")
        end = server.index("async function handleControl", begin)
        issue_access = server[begin:end]

        self.assertLess(
            issue_access.index("session.accessDigests.clear()"),
            issue_access.index("session.accessDigests.add"),
        )
