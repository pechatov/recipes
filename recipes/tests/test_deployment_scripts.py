from importlib import import_module
from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[2]


class MigrationDeploymentContractTests(SimpleTestCase):
    def test_single_store_data_migration_commits_before_partial_index(self):
        migration = import_module(
            "recipes.migrations.0015_alter_storepreference_options_and_more"
        )

        self.assertFalse(migration.Migration.atomic)


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

    def test_uncertain_cart_operation_is_fenced_before_profile_release(self):
        server = (ROOT / "scripts/browser-login-server.mjs").read_text()
        script = (ROOT / "scripts/connect-hermes-cart-pi.sh").read_text()

        begin = server.index("async function recoverAutomationScope")
        end = server.index("async function recoverInterruptedSession", begin)
        recovery = server[begin:end]
        stop = recovery.index('systemctlRecoveryUnits("stop")')
        close = recovery.index("closeCamofoxUser(identity.user_id)")
        start = recovery.index('systemctlRecoveryUnits("start")')
        self.assertLess(stop, close)
        self.assertLess(close, start)
        self.assertIn('url.pathname === "/v1/recoveries"', server)
        self.assertIn(
            "BROWSER_RECOVERY_UNITS=recipes-cart-adapter.service,"
            "hermes-gateway-{profile}.service",
            script,
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
        self.assertIn("CART_ADAPTER_TLS_CERT", script)
        self.assertIn("CART_ADAPTER_TLS_KEY", script)
        self.assertIn("CART_ADAPTER_CA_CERT_B64", script)
        self.assertIn("CART_ADAPTER_URL='https://$PI_ADDRESS:$PI_ADAPTER_PORT'", script)
        self.assertIn('--cacert "$ADAPTER_TLS_CURRENT/server.crt"', script)
        self.assertIn("cmp -s", script)
        self.assertIn("openssl pkey -pubout", script)
        self.assertIn('mv -Tf "$staged_link" "$ADAPTER_TLS_CURRENT"', script)
        self.assertIn("tls/current/server.crt", script)
        self.assertIn("tls/current/server.key", script)
        self.assertNotIn(
            "CART_ADAPTER_URL='http://$PI_ADDRESS:$PI_ADAPTER_PORT'",
            script,
        )
        self.assertIn("CART_ADAPTER_FALLBACK_TO_HERMES", script)

        client = (ROOT / "recipes/carting/client.py").read_text()
        self.assertIn("def _adapter_tls_context()", client)
        self.assertIn("ssl.create_default_context(cadata=certificate)", client)
        self.assertIn("verify=_adapter_tls_context()", client)

        server = (ROOT / "scripts/cart-adapter-server.mjs").read_text()
        self.assertIn("https.createServer(", server)
        self.assertIn("cert: readFileSync(tlsCertPath)", server)
        self.assertIn("key: readFileSync(tlsKeyPath)", server)
        self.assertIn("(!tlsEnabled && !loopbackHosts.has(bindHost))", server)

    def test_adapter_exposes_only_bounded_cart_operations(self):
        server = (ROOT / "scripts/cart-adapter-server.mjs").read_text()

        self.assertIn('"/v1/search"', server)
        self.assertIn('"/v1/cart-state"', server)
        self.assertIn('"/v1/apply"', server)
        self.assertIn('"/v1/cleanup"', server)
        self.assertIn("/api/v1/cart", server)
        self.assertNotIn("/api/v1/cart/add_bulk", server)
        self.assertIn("for (const item of items)", server)
        self.assertNotIn("Promise.all(items.map", server)
        self.assertIn(
            "if (result.status < 200 || result.status >= 300) return result",
            server,
        )
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
        self.assertIn("markProfileUncertain(operationError || closeError)", server)
        self.assertIn("Boolean(error?.mutationPossible)", server)
        self.assertIn(
            'status: "incomplete", summary: error.message, '
            "mutation_possible: mutationPossible",
            server,
        )
        self.assertIn("A lost create response may still have opened", server)
        safety_test = (ROOT / "scripts/cart-adapter-server.test.mjs").read_text()
        self.assertIn('new Error("failed after dispatch")', safety_test)
        self.assertIn("mutation_possible: true", safety_test)
        self.assertIn("const activeScopes = new Set()", server)
        self.assertIn("activeScopes.has(scope)", server)
        self.assertIn('"scope_busy"', server)
        self.assertIn("await runExclusiveOperation(", server)
        self.assertIn("text(body?.scope, 80)", server)
        self.assertIn("const operationBudgetMs = 160_000", server)
        self.assertIn("boundedOperationTimeout(options.timeout || 30_000)", server)
        self.assertIn("timeout: boundedOperationTimeout(10_000)", server)
        self.assertIn("() => runWithOperationDeadline(operation)", server)
        self.assertIn("function sameLocation", server)
        self.assertIn("if (!sameLocation(state, context))", server)
        self.assertIn("latitude: currentContext.latitude", server)
        self.assertIn("cart-adapter-operations.json", server)
        self.assertIn("completedOperationRetentionMs = 30 * 24", server)
        self.assertIn("startedOperationRetentionMs = 90 * 24", server)
        self.assertIn("maximumOperationRecords = 2_000", server)
        self.assertIn("maximumOperationStateBytes = 16 * 1024 * 1024", server)
        self.assertIn("pruneOperationRecords(records)", server)
        self.assertIn('status: "started"', server)
        self.assertIn('status: "completed"', server)
        self.assertIn("await storeOperationRecord(recordKey", server)
        self.assertIn("await file.writeFile(serialized)", server)
        self.assertIn("await file.sync()", server)
        self.assertIn("await rename(temporary, target)", server)
        self.assertIn("await directory.sync()", server)
        self.assertIn("operationRecordsPromise = Promise.resolve(records)", server)
        self.assertIn("quarantinedScopesPromise = Promise.resolve(scopes)", server)
        self.assertIn("cart-adapter-quarantine.json", server)
        self.assertIn("function markProfileUncertain", server)
        self.assertIn("await recoverQuarantinedScope(scope)", server)
        self.assertIn("await quarantineScope(scope)", server)
        self.assertIn("await deferScopeRecovery(scope)", server)
        self.assertIn("deferredBrowserCreateSettlementMs = 2 * 60 * 1000", server)
        self.assertIn("if (Date.now() < recoverAfter)", server)
        self.assertIn("const result = await operation()", server)
        self.assertIn("await releaseScopeQuarantine(scope)", server)
        self.assertIn("await closeBrowser(identity.user_id)", server)
        self.assertIn("itemsToAdd.push({ ...item, delta: item.quantity })", server)
        self.assertIn("const expected = existing + item.quantity", server)
        self.assertNotIn("Math.max(existing, item.quantity)", server)
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
