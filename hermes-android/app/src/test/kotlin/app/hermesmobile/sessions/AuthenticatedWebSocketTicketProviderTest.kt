package app.hermesmobile.sessions

import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.ScopedWebSocketTicket
import app.hermesmobile.protocol.auth.ScopedWebSocketTicketRequest
import app.hermesmobile.protocol.auth.WebSocketTicket
import app.hermesmobile.protocol.gateway.GatewayConnectionRole
import app.hermesmobile.protocol.sessions.SessionKey
import kotlinx.coroutines.test.runTest
import org.junit.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull

class AuthenticatedWebSocketTicketProviderTest {
    private val endpoint = GatewayEndpoint.parse("https://gateway.example/base/").getOrThrow()
    private val observerClientInstanceId =
        ClientInstanceId("123e4567-e89b-12d3-a456-426614174000")

    @Test
    fun `cookie session mints observer and control tickets before bearer fallback`() = runTest {
        val requested = ScopedWebSocketTicketRequest(
            connectionRole = GatewayConnectionRole.CONTROL,
            clientInstanceId = observerClientInstanceId,
            sessionKey = SessionKey("session-1"),
            profile = "fox",
        )
        val scopedCookieCalls = mutableListOf<ScopedWebSocketTicketRequest>()
        val provider = AuthenticatedWebSocketTicketProvider(
            endpoint = endpoint,
            tokenVault = FakeVault(null),
            tokenRefresh = TokenRefresh { _, _ -> error("Bearer refresh must not run") },
            ticketMint = WebSocketTicketMint { _, _ -> error("Bearer mint must not run") },
            scopedTicketMint = ScopedWebSocketTicketMint { _, _, _ ->
                error("Bearer scoped mint must not run")
            },
            cookieTicketMint = CookieWebSocketTicketMint {
                error("Legacy empty-body observer mint must not run")
            },
            scopedCookieTicketMint = ScopedCookieWebSocketTicketMint { _, request ->
                scopedCookieCalls += request
                NativeAuthResult.Success(
                    ScopedWebSocketTicket(
                        if (request.connectionRole == GatewayConnectionRole.OBSERVER) {
                            "cookie-observer"
                        } else {
                            "cookie-control"
                        },
                        30,
                        request.connectionRole,
                        request.observerContractVersion,
                    ),
                )
            },
        )

        assertEquals(
            "cookie-observer",
            assertIs<WebSocketTicketResult.Ready>(
                provider.mint(observerClientInstanceId),
            ).ticket.value,
        )
        assertEquals(
            "cookie-control",
            assertIs<ScopedWebSocketTicketResult.Ready>(provider.mint(requested)).ticket.value,
        )
        assertEquals(
            listOf(
                ScopedWebSocketTicketRequest(
                    connectionRole = GatewayConnectionRole.OBSERVER,
                    clientInstanceId = observerClientInstanceId,
                ),
                requested,
            ),
            scopedCookieCalls,
        )
    }

    @Test
    fun `mints ticket with stored access token`() = runTest {
        val vault = FakeVault(tokens("access-1", "refresh-1", 1_000))
        val mintedWith = mutableListOf<String>()
        val provider = provider(
            vault = vault,
            mint = WebSocketTicketMint { _, accessToken ->
                mintedWith += accessToken
                NativeAuthResult.Success(WebSocketTicket("ticket-1", 30))
            },
        )

        val result = provider.mint(observerClientInstanceId)

        assertEquals("ticket-1", assertIs<WebSocketTicketResult.Ready>(result).ticket.value)
        assertEquals(listOf("access-1"), mintedWith)
    }

    @Test
    fun `ticket 401 refreshes credentials and retries exactly once`() = runTest {
        val current = tokens("access-1", "refresh-1", 1_000)
        val rotated = tokens("access-2", "refresh-2", 2_000)
        val vault = FakeVault(current)
        val mintedWith = mutableListOf<String>()
        val provider = provider(
            vault = vault,
            refresh = TokenRefresh { _, _ -> NativeAuthResult.Success(rotated) },
            mint = WebSocketTicketMint { _, accessToken ->
                mintedWith += accessToken
                if (accessToken == "access-1") {
                    NativeAuthResult.HttpFailure(401)
                } else {
                    NativeAuthResult.Success(WebSocketTicket("ticket-2", 30))
                }
            },
        )

        val result = provider.mint(observerClientInstanceId)

        assertIs<WebSocketTicketResult.Ready>(result)
        assertEquals(listOf("access-1", "access-2"), mintedWith)
        assertEquals(rotated, vault.value)
    }

    @Test
    fun `rejected rotated credential is cleared without retry loop`() = runTest {
        val vault = FakeVault(tokens("access-1", "refresh-1", 1_000))
        val mintedWith = mutableListOf<String>()
        val provider = provider(
            vault = vault,
            refresh = TokenRefresh { _, _ ->
                NativeAuthResult.Success(tokens("access-2", "refresh-2", 2_000))
            },
            mint = WebSocketTicketMint { _, accessToken ->
                mintedWith += accessToken
                NativeAuthResult.HttpFailure(401)
            },
        )

        val result = provider.mint(observerClientInstanceId)

        assertIs<WebSocketTicketResult.AuthenticationRequired>(result)
        assertEquals(listOf("access-1", "access-2"), mintedWith)
        assertNull(vault.value)
        assertEquals(1, vault.clearCount)
    }

    @Test
    fun `scoped ticket forwards immutable control claims with stored access token`() = runTest {
        val vault = FakeVault(tokens("access-1", "refresh-1", 1_000))
        val requested = ScopedWebSocketTicketRequest(
            connectionRole = GatewayConnectionRole.CONTROL,
            clientInstanceId = ClientInstanceId("123e4567-e89b-12d3-a456-426614174000"),
            sessionKey = SessionKey("session-1"),
            profile = "fox",
        )
        val mintedWith = mutableListOf<Pair<String, ScopedWebSocketTicketRequest>>()
        val provider = provider(
            vault = vault,
            mint = WebSocketTicketMint { _, _ -> error("Unexpected legacy mint") },
            scopedMint = ScopedWebSocketTicketMint { _, accessToken, request ->
                mintedWith += accessToken to request
                NativeAuthResult.Success(
                    ScopedWebSocketTicket("ticket-1", 30, GatewayConnectionRole.CONTROL),
                )
            },
        )

        val result = provider.mint(requested)

        assertEquals(
            GatewayConnectionRole.CONTROL,
            assertIs<ScopedWebSocketTicketResult.Ready>(result).ticket.connectionRole,
        )
        assertEquals(listOf("access-1" to requested), mintedWith)
    }

    @Test
    fun `scoped observer ticket fails closed when returned contract differs`() = runTest {
        val vault = FakeVault(tokens("access-1", "refresh-1", 1_000))
        val requested = ScopedWebSocketTicketRequest(
            connectionRole = GatewayConnectionRole.OBSERVER,
            clientInstanceId = observerClientInstanceId,
            observerContractVersion = 2,
        )
        val provider = provider(
            vault = vault,
            mint = WebSocketTicketMint { _, _ -> error("Unexpected legacy mint") },
            scopedMint = ScopedWebSocketTicketMint { _, _, _ ->
                NativeAuthResult.Success(
                    ScopedWebSocketTicket(
                        value = "wrong-contract",
                        ttlSeconds = 30,
                        connectionRole = GatewayConnectionRole.OBSERVER,
                        observerContractVersion = 1,
                    ),
                )
            },
        )

        assertIs<ScopedWebSocketTicketResult.Unavailable>(provider.mint(requested))
    }

    @Test
    fun `scoped ticket 401 refreshes and retries the same claims exactly once`() = runTest {
        val current = tokens("access-1", "refresh-1", 1_000)
        val rotated = tokens("access-2", "refresh-2", 2_000)
        val vault = FakeVault(current)
        val requested = ScopedWebSocketTicketRequest(
            connectionRole = GatewayConnectionRole.CONTROL,
            clientInstanceId = ClientInstanceId("123e4567-e89b-12d3-a456-426614174000"),
            sessionKey = SessionKey("session-1"),
            profile = "fox",
        )
        val mintedWith = mutableListOf<Pair<String, ScopedWebSocketTicketRequest>>()
        val provider = provider(
            vault = vault,
            refresh = TokenRefresh { _, _ -> NativeAuthResult.Success(rotated) },
            mint = WebSocketTicketMint { _, _ -> error("Unexpected legacy mint") },
            scopedMint = ScopedWebSocketTicketMint { _, accessToken, request ->
                mintedWith += accessToken to request
                if (accessToken == "access-1") {
                    NativeAuthResult.HttpFailure(401)
                } else {
                    NativeAuthResult.Success(
                        ScopedWebSocketTicket("ticket-2", 30, GatewayConnectionRole.CONTROL),
                    )
                }
            },
        )

        assertIs<ScopedWebSocketTicketResult.Ready>(provider.mint(requested))
        assertEquals(listOf("access-1" to requested, "access-2" to requested), mintedWith)
        assertEquals(rotated, vault.value)
    }

    private fun provider(
        vault: FakeVault,
        refresh: TokenRefresh = TokenRefresh { _, _ -> error("Unexpected refresh") },
        mint: WebSocketTicketMint,
        scopedMint: ScopedWebSocketTicketMint? = null,
    ) = AuthenticatedWebSocketTicketProvider(
        endpoint = endpoint,
        tokenVault = vault,
        tokenRefresh = refresh,
        ticketMint = mint,
        scopedTicketMint = scopedMint,
        clockEpochSeconds = { 100 },
    )

    private fun tokens(access: String, refresh: String, expiresAt: Long) = NativeTokens(
        accessToken = access,
        refreshToken = refresh,
        tokenType = "Bearer",
        expiresAtEpochSeconds = expiresAt,
        provider = "github",
        userId = "user-1",
    )

    private class FakeVault(
        var value: NativeTokens?,
    ) : TokenVault {
        var clearCount = 0

        override fun save(endpointId: String, tokens: NativeTokens) {
            value = tokens
        }

        override fun load(endpointId: String): NativeTokens? = value

        override fun clear(endpointId: String) {
            value = null
            clearCount += 1
        }
    }
}
