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

    def test_novnc_websocket_path_is_origin_absolute(self):
        server = (ROOT / "scripts/browser-login-server.mjs").read_text()

        self.assertIn(
            "path=/browser-login/session/${session.id}/websockify",
            server,
        )
        self.assertNotIn(
            "path=browser-login/session/${session.id}/websockify",
            server,
        )

    def test_browser_proxy_forwards_only_its_dedicated_cookie(self):
        script = (ROOT / "scripts/deploy/configure-npm-proxy.sh").read_text()

        begin = script.index("location /browser-login/ {")
        end = script.index("location / {", begin)
        browser_location = script[begin:end]

        self.assertIn(
            'proxy_set_header Cookie "recipes_browser_login='
            '\\$cookie_recipes_browser_login";',
            browser_location,
        )
        self.assertIn('proxy_set_header Connection "upgrade";', browser_location)
        self.assertNotIn(r'Connection \"upgrade\"', browser_location)
        self.assertNotIn("$http_cookie", browser_location)


class CartAdapterDeploymentContractTests(SimpleTestCase):
    def test_adapter_is_installed_as_a_camofox_dependent_service(self):
        script = (ROOT / "scripts/connect-hermes-cart-pi.sh").read_text()

        self.assertIn("recipes-cart-adapter.service", script)
        self.assertIn(
            "Requires=recipes-camofox.service recipes-browser-login.service",
            script,
        )
        self.assertIn("CART_ADAPTER_CONTROL_KEY", script)
        self.assertIn("CART_ADAPTER_BASE_URL", script)
        self.assertIn("CART_ADAPTER_FALLBACK_TO_HERMES", script)

    def test_adapter_exposes_only_bounded_cart_operations(self):
        server = (ROOT / "scripts/cart-adapter-server.mjs").read_text()

        self.assertIn('"/v1/search"', server)
        self.assertIn('"/v1/cart-state"', server)
        self.assertIn('"/v1/apply"', server)
        self.assertIn('"/v1/cleanup"', server)
        self.assertIn("/api/v1/cart", server)
        self.assertNotIn("/api/v1/cart/add_bulk", server)
        self.assertIn("Promise.all(items.map", server)
        self.assertIn("item_id:", server)
        self.assertIn("place_business: context.place_business", server)
        self.assertIn('soft_multi: "true"', server)
        self.assertIn("placeSlug: context.place_slug", server)
        self.assertNotIn("method: 'PUT'", server)
        self.assertNotIn("method: 'DELETE'", server)
        self.assertIn("performs no downward mutation", server)
        self.assertIn("dispatchCartMutation", server)
        self.assertIn('{ mutationPossible: true }', server)
        self.assertIn("function preserveMutationUncertainty", server)
        self.assertIn("function finalBrowserError", server)
        self.assertIn("operationError || closeError, true", server)
        self.assertIn("Boolean(error?.mutationPossible)", server)
        self.assertIn("A lost create response may still have opened", server)
        safety_test = (ROOT / "scripts/cart-adapter-server.test.mjs").read_text()
        self.assertIn('new Error("failed after dispatch")', safety_test)
        self.assertIn("mutation_possible: true", safety_test)
        self.assertIn("const activeScopes = new Set()", server)
        self.assertIn("activeScopes.has(scope)", server)
        self.assertIn('"scope_busy"', server)
        self.assertIn("runExclusiveOperation(text(body?.scope, 80), operation)", server)
        self.assertIn("function sameLocation", server)
        self.assertIn("if (!sameLocation(state, context))", server)
        self.assertIn("latitude: currentContext.latitude", server)
        self.assertNotIn("/eats/v1/sku-cart", server)
        self.assertNotIn("mutationState.possible = mutationWasPossible", server)
        self.assertIn("sealSelection", server)
        self.assertIn("openSelection", server)
        self.assertIn("sealCleanup", server)
        self.assertIn("openCleanup", server)
        self.assertIn("cleanup_token:", server)
        self.assertIn("before_quantity", server)
        self.assertIn("after_quantity", server)
        self.assertIn("sku_id: item.sku_id", server)
        self.assertIn("candidate.place_slug === context.place_slug", server)
        self.assertIn("data.cart_places_list.length === 0", server)
        self.assertIn("existing > item.before_quantity", server)
        self.assertIn('createCipheriv("aes-256-gcm"', server)
        self.assertIn("rawLatitude === null || rawLongitude === null", server)
        self.assertNotIn('url.pathname === "/evaluate"', server)

    def test_confirmation_deadline_is_shorter_than_cleanup_token_lifetime(self):
        settings = (ROOT / "config/settings.py").read_text()
        server = (ROOT / "scripts/cart-adapter-server.mjs").read_text()

        self.assertIn("7 * 24 * 60", settings)
        self.assertIn("8 * 24 * 60 * 60 * 1000", server)
        self.assertIn("max(210, CART_ADAPTER_TIMEOUT_SECONDS)", settings)
