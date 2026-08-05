package app.hermesmobile.connection

import app.hermesmobile.auth.NativeSignIn
import app.hermesmobile.auth.NativeSignInResult
import app.hermesmobile.auth.PasswordSignIn
import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.DiscoveryResult
import app.hermesmobile.protocol.GatewayDiscovery
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.GatewayStatus
import app.hermesmobile.protocol.auth.NativeTokens
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Rule
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

@OptIn(ExperimentalCoroutinesApi::class)
class ConnectionViewModelTest {
    @get:Rule
    val mainDispatcherRule = MainDispatcherRule()

    @Test
    fun `new installation starts with the production gateway address filled`() {
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery { error("Discovery should not run before confirmation.") },
        )

        assertEquals(
            "https://api.seaotter.wiki/hermes/",
            viewModel.state.value.endpointInput,
        )
        assertEquals(ConnectionPhase.IDLE, viewModel.state.value.phase)
    }

    @Test
    fun `invalid endpoint is rejected before network discovery`() = runTest {
        var discoveryCalls = 0
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery {
                discoveryCalls += 1
                DiscoveryResult.NetworkFailure()
            },
        )

        viewModel.onEndpointChanged("ftp://example.com")
        viewModel.connect()
        advanceUntilIdle()

        assertEquals(0, discoveryCalls)
        assertEquals(ConnectionPhase.INVALID_ENDPOINT, viewModel.state.value.phase)
        assertTrue(viewModel.state.value.message.orEmpty().isNotBlank())
        assertFalse(viewModel.state.value.isChecking)
    }

    @Test
    fun `reachable protected gateway transitions to authentication required`() = runTest {
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery {
                DiscoveryResult.Reachable(
                    GatewayStatus(
                        version = "0.14.0",
                        releaseDate = "2026-07-01",
                        gatewayRunning = true,
                        gatewayState = "running",
                        authRequired = true,
                        authFlows = setOf("cookie", "native_pkce"),
                        overall = "ok",
                    ),
                )
            },
        )

        viewModel.onEndpointChanged("hermes.example.com")
        viewModel.connect()
        advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals(ConnectionPhase.AUTHENTICATION_REQUIRED, state.phase)
        assertEquals("https://hermes.example.com/", state.canonicalEndpoint)
        assertEquals("0.14.0", state.hermesVersion)
        assertTrue(state.supportsNativePkce)
        assertTrue(state.gatewayRunning)
        assertNull(state.message)
    }

    @Test
    fun `basic provider advertises password sign in without native PKCE`() = runTest {
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery {
                DiscoveryResult.Reachable(
                    GatewayStatus(
                        version = "0.19.0",
                        releaseDate = "2026-07-20",
                        gatewayRunning = true,
                        gatewayState = "running",
                        authRequired = true,
                        authProviders = setOf("basic"),
                        authFlows = setOf("cookie"),
                        overall = "ok",
                    ),
                )
            },
        )

        viewModel.onEndpointChanged("https://api.seaotter.wiki/hermes/")
        viewModel.connect()
        advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals(ConnectionPhase.AUTHENTICATION_REQUIRED, state.phase)
        assertTrue(state.supportsPassword)
        assertTrue(state.canPasswordSignIn)
        assertFalse(state.supportsNativePkce)
    }

    @Test
    fun `stored credentials open a protected gateway without another browser sign-in`() = runTest {
        val vault = RecordingTokenVault().apply {
            savedTokens = NativeTokens(
                accessToken = "stored-access",
                refreshToken = "stored-refresh",
                tokenType = "Bearer",
                expiresAtEpochSeconds = 1_900_000_000,
                provider = "nous",
                userId = "user-1",
            )
        }
        val viewModel = ConnectionViewModel(
            discovery = protectedGatewayDiscovery(),
            tokenVault = vault,
        )

        viewModel.onEndpointChanged("hermes.example.com")
        viewModel.connect()
        advanceUntilIdle()

        assertEquals(ConnectionPhase.READY, viewModel.state.value.phase)
        assertTrue(viewModel.state.value.requiresAuthentication)
    }

    @Test
    fun `stored endpoint is discovered on cold start and reuses encrypted credentials`() = runTest {
        var discoveredEndpoint: String? = null
        val vault = RecordingTokenVault().apply {
            savedTokens = NativeTokens(
                accessToken = "stored-access",
                refreshToken = "stored-refresh",
                tokenType = "Bearer",
                expiresAtEpochSeconds = 1_900_000_000,
                provider = "basic",
                userId = "mobile-user",
            )
        }
        val endpointStore = RecordingEndpointStore(
            restoredEndpoint = "https://api.seaotter.wiki/hermes/",
        )
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery { endpoint ->
                discoveredEndpoint = endpoint.baseUrl.toString()
                basicGatewayDiscovery().discover(endpoint)
            },
            tokenVault = vault,
            endpointStore = endpointStore,
        )

        advanceUntilIdle()

        assertEquals("https://api.seaotter.wiki/hermes/", discoveredEndpoint)
        assertEquals("https://api.seaotter.wiki/hermes/", viewModel.state.value.endpointInput)
        assertEquals(ConnectionPhase.READY, viewModel.state.value.phase)
    }

    @Test
    fun `reachable gateway persists its canonical endpoint for the next cold start`() = runTest {
        val endpointStore = RecordingEndpointStore()
        val viewModel = ConnectionViewModel(
            discovery = basicGatewayDiscovery(),
            endpointStore = endpointStore,
        )

        viewModel.onEndpointChanged("https://api.seaotter.wiki/hermes")
        viewModel.connect()
        advanceUntilIdle()

        assertEquals(
            "https://api.seaotter.wiki/hermes/",
            endpointStore.savedEndpoint,
        )
    }

    @Test
    fun `editing endpoint cancels stale discovery result`() = runTest {
        val firstResult = CompletableDeferred<DiscoveryResult>()
        val secondResult = CompletableDeferred<DiscoveryResult>()
        val discovery = GatewayDiscovery { endpoint: GatewayEndpoint ->
            if (endpoint.baseUrl.host == "first.example.com") firstResult.await() else secondResult.await()
        }
        val viewModel = ConnectionViewModel(discovery)

        viewModel.onEndpointChanged("first.example.com")
        viewModel.connect()
        runCurrent()
        viewModel.onEndpointChanged("second.example.com")
        viewModel.connect()
        runCurrent()

        secondResult.complete(
            DiscoveryResult.Reachable(
                GatewayStatus(
                    version = "second",
                    releaseDate = null,
                    gatewayRunning = true,
                    gatewayState = "running",
                    authRequired = false,
                    authFlows = emptySet(),
                    overall = "ok",
                ),
            ),
        )
        advanceUntilIdle()
        firstResult.complete(DiscoveryResult.NetworkFailure())
        advanceUntilIdle()

        assertEquals(ConnectionPhase.READY, viewModel.state.value.phase)
        assertEquals("second", viewModel.state.value.hermesVersion)
        assertEquals("https://second.example.com/", viewModel.state.value.canonicalEndpoint)
    }

    @Test
    fun `network failure is presented without internal diagnostics`() = runTest {
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery {
                DiscoveryResult.NetworkFailure("Hermes could not be reached.")
            },
        )

        viewModel.onEndpointChanged("hermes.example.com")
        viewModel.connect()
        advanceUntilIdle()

        assertEquals(ConnectionPhase.UNAVAILABLE, viewModel.state.value.phase)
        assertEquals("Hermes could not be reached.", viewModel.state.value.message)
        assertFalse(viewModel.state.value.isChecking)
    }

    @Test
    fun `native sign-in stores tokens then marks endpoint ready`() = runTest {
        val tokens = NativeTokens(
            accessToken = "access",
            refreshToken = "refresh",
            tokenType = "Bearer",
            expiresAtEpochSeconds = 1_900_000_000,
            provider = "nous",
            userId = "user-1",
        )
        val vault = RecordingTokenVault()
        val viewModel = ConnectionViewModel(
            discovery = protectedGatewayDiscovery(),
            nativeSignIn = NativeSignIn { NativeSignInResult.Authenticated(tokens) },
            tokenVault = vault,
        )
        viewModel.onEndpointChanged("hermes.example.com")
        viewModel.connect()
        advanceUntilIdle()

        viewModel.signIn()
        advanceUntilIdle()

        assertEquals(ConnectionPhase.READY, viewModel.state.value.phase)
        assertEquals("https://hermes.example.com/", vault.savedEndpoint)
        assertEquals("access", vault.savedTokens?.accessToken)
        assertNull(viewModel.state.value.message)
    }

    @Test
    fun `password sign-in stores tokens before marking endpoint ready`() = runTest {
        val tokens = NativeTokens(
            accessToken = "password-access",
            refreshToken = "password-refresh",
            tokenType = "Bearer",
            expiresAtEpochSeconds = 1_900_000_000,
            provider = "basic",
            userId = "mobile-user",
        )
        var submittedUsername: String? = null
        var submittedPassword: String? = null
        val vault = RecordingTokenVault()
        val viewModel = ConnectionViewModel(
            discovery = basicGatewayDiscovery(),
            passwordSignIn = PasswordSignIn { _, username, password ->
                submittedUsername = username
                submittedPassword = password
                NativeSignInResult.Authenticated(tokens)
            },
            tokenVault = vault,
        )
        viewModel.onEndpointChanged("https://api.seaotter.wiki/hermes/")
        viewModel.connect()
        advanceUntilIdle()

        viewModel.signInWithPassword("mobile-user", "temporary-password")
        advanceUntilIdle()

        assertEquals("mobile-user", submittedUsername)
        assertEquals("temporary-password", submittedPassword)
        assertEquals(ConnectionPhase.READY, viewModel.state.value.phase)
        assertEquals("https://api.seaotter.wiki/hermes/", vault.savedEndpoint)
        assertEquals("password-access", vault.savedTokens?.accessToken)
        assertNull(viewModel.state.value.message)
    }

    @Test
    fun `failed native sign-in remains recoverable without storing tokens`() = runTest {
        val vault = RecordingTokenVault()
        val viewModel = ConnectionViewModel(
            discovery = protectedGatewayDiscovery(),
            nativeSignIn = NativeSignIn {
                NativeSignInResult.Failed("Secure sign-in timed out. Try again.")
            },
            tokenVault = vault,
        )
        viewModel.onEndpointChanged("hermes.example.com")
        viewModel.connect()
        advanceUntilIdle()

        viewModel.signIn()
        advanceUntilIdle()

        assertEquals(ConnectionPhase.AUTHENTICATION_REQUIRED, viewModel.state.value.phase)
        assertEquals("Secure sign-in timed out. Try again.", viewModel.state.value.message)
        assertNull(vault.savedTokens)
        assertTrue(viewModel.state.value.canSignIn)
    }

    @Test
    fun `returning from sessions resets connection result but preserves input`() = runTest {
        val viewModel = ConnectionViewModel(
            discovery = GatewayDiscovery {
                DiscoveryResult.Reachable(
                    GatewayStatus(
                        version = "0.14.0",
                        releaseDate = null,
                        gatewayRunning = true,
                        gatewayState = "running",
                        authRequired = false,
                        authFlows = emptySet(),
                        overall = "ok",
                    ),
                )
            },
        )
        viewModel.onEndpointChanged("hermes.example.com")
        viewModel.connect()
        advanceUntilIdle()

        viewModel.returnToConnection()

        assertEquals(ConnectionPhase.IDLE, viewModel.state.value.phase)
        assertEquals("hermes.example.com", viewModel.state.value.endpointInput)
        assertNull(viewModel.state.value.canonicalEndpoint)
    }

    private fun protectedGatewayDiscovery(): GatewayDiscovery = GatewayDiscovery {
        DiscoveryResult.Reachable(
            GatewayStatus(
                version = "0.14.0",
                releaseDate = null,
                gatewayRunning = true,
                gatewayState = "running",
                authRequired = true,
                authFlows = setOf("native_pkce"),
                overall = "ok",
            ),
        )
    }

    private fun basicGatewayDiscovery(): GatewayDiscovery = GatewayDiscovery {
        DiscoveryResult.Reachable(
            GatewayStatus(
                version = "0.19.0",
                releaseDate = null,
                gatewayRunning = true,
                gatewayState = "running",
                authRequired = true,
                authProviders = setOf("basic"),
                authFlows = setOf("cookie"),
                overall = "ok",
            ),
        )
    }

    private class RecordingTokenVault : TokenVault {
        var savedEndpoint: String? = null
        var savedTokens: NativeTokens? = null

        override fun save(endpointId: String, tokens: NativeTokens) {
            savedEndpoint = endpointId
            savedTokens = tokens
        }

        override fun load(endpointId: String): NativeTokens? = savedTokens

        override fun clear(endpointId: String) = Unit
    }

    private class RecordingEndpointStore(
        private val restoredEndpoint: String? = null,
    ) : EndpointStore {
        var savedEndpoint: String? = null

        override fun load(): String? = restoredEndpoint

        override fun save(canonicalEndpoint: String) {
            savedEndpoint = canonicalEndpoint
        }

        override fun clear() = Unit
    }
}
