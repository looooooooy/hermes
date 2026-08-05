package app.hermesmobile.sessions

import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.sessions.SessionPage
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.SessionsApi
import app.hermesmobile.protocol.sessions.SessionsResult
import kotlinx.coroutines.test.runTest
import org.junit.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull

class AuthenticatedSessionsRepositoryTest {
    private val endpoint = GatewayEndpoint.parse("https://gateway.example/base/").getOrThrow()

    @Test
    fun `missing token requires authentication without contacting sessions API`() = runTest {
        val vault = FakeTokenVault()
        val api = FakeSessionsApi()
        val repository = repository(vault = vault, api = api)

        val result = repository.loadSessions()

        assertIs<SessionRepositoryResult.AuthenticationRequired>(result)
        assertEquals(emptyList(), api.listAccessTokens)
    }

    @Test
    fun `unprotected endpoint can preload without stored credentials`() = runTest {
        val vault = FakeTokenVault()
        val api = FakeSessionsApi()
        val repository = repository(
            vault = vault,
            api = api,
            authenticationRequired = false,
        )

        val result = repository.loadSessions()

        assertIs<SessionRepositoryResult.Data<SessionPage>>(result)
        assertEquals(listOf(""), api.listAccessTokens)
    }

    @Test
    fun `access token nearing expiry is refreshed and rotated before preload`() = runTest {
        val current = tokens(access = "old", refresh = "refresh-1", expiresAt = 120)
        val rotated = tokens(access = "new", refresh = "refresh-2", expiresAt = 1000)
        val vault = FakeTokenVault(current)
        val api = FakeSessionsApi()
        var refreshCount = 0
        val repository = repository(
            vault = vault,
            api = api,
            refresh = TokenRefresh { _, token ->
                refreshCount += 1
                assertEquals(current, token)
                NativeAuthResult.Success(rotated)
            },
        )

        val result = repository.loadSessions()

        assertIs<SessionRepositoryResult.Data<SessionPage>>(result)
        assertEquals(1, refreshCount)
        assertEquals(listOf("new"), api.listAccessTokens)
        assertEquals(rotated, vault.tokens)
    }

    @Test
    fun `sessions 401 triggers exactly one refresh and retry`() = runTest {
        val current = tokens(access = "old", refresh = "refresh-1", expiresAt = 1000)
        val rotated = tokens(access = "new", refresh = "refresh-2", expiresAt = 2000)
        val vault = FakeTokenVault(current)
        val api = FakeSessionsApi(
            listResults = ArrayDeque(
                listOf(
                    SessionsResult.HttpFailure(401),
                    SessionsResult.Success(EMPTY_PAGE),
                ),
            ),
        )
        var refreshCount = 0
        val repository = repository(
            vault = vault,
            api = api,
            refresh = TokenRefresh { _, _ ->
                refreshCount += 1
                NativeAuthResult.Success(rotated)
            },
        )

        val result = repository.loadSessions()

        assertIs<SessionRepositoryResult.Data<SessionPage>>(result)
        assertEquals(1, refreshCount)
        assertEquals(listOf("old", "new"), api.listAccessTokens)
        assertEquals(rotated, vault.tokens)
    }

    @Test
    fun `invalid refresh clears unusable credentials`() = runTest {
        val current = tokens(access = "old", refresh = "refresh-1", expiresAt = 120)
        val vault = FakeTokenVault(current)
        val repository = repository(
            vault = vault,
            refresh = TokenRefresh { _, _ -> NativeAuthResult.HttpFailure(401) },
        )

        val result = repository.loadSessions()

        assertIs<SessionRepositoryResult.AuthenticationRequired>(result)
        assertNull(vault.tokens)
        assertEquals(1, vault.clearCount)
    }

    @Test
    fun `a second 401 is not retried and clears rejected rotated credentials`() = runTest {
        val current = tokens(access = "old", refresh = "refresh-1", expiresAt = 1000)
        val rotated = tokens(access = "new", refresh = "refresh-2", expiresAt = 2000)
        val vault = FakeTokenVault(current)
        val api = FakeSessionsApi(
            listResults = ArrayDeque(
                listOf(
                    SessionsResult.HttpFailure(401),
                    SessionsResult.HttpFailure(401),
                ),
            ),
        )
        val repository = repository(
            vault = vault,
            api = api,
            refresh = TokenRefresh { _, _ -> NativeAuthResult.Success(rotated) },
        )

        val result = repository.loadSessions()

        assertIs<SessionRepositoryResult.AuthenticationRequired>(result)
        assertEquals(listOf("old", "new"), api.listAccessTokens)
        assertNull(vault.tokens)
    }

    private fun repository(
        vault: FakeTokenVault,
        api: FakeSessionsApi = FakeSessionsApi(),
        refresh: TokenRefresh = TokenRefresh { _, _ -> error("Unexpected refresh") },
        authenticationRequired: Boolean = true,
    ): AuthenticatedSessionsRepository = AuthenticatedSessionsRepository(
        endpoint = endpoint,
        tokenVault = vault,
        sessionsApi = api,
        tokenRefresh = refresh,
        authenticationRequired = authenticationRequired,
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

    private class FakeTokenVault(
        var tokens: NativeTokens? = null,
    ) : TokenVault {
        var clearCount = 0

        override fun load(endpointId: String): NativeTokens? = tokens

        override fun save(endpointId: String, tokens: NativeTokens) {
            this.tokens = tokens
        }

        override fun clear(endpointId: String) {
            tokens = null
            clearCount += 1
        }
    }

    private class FakeSessionsApi(
        private val listResults: ArrayDeque<SessionsResult<SessionPage>> = ArrayDeque(
            listOf(SessionsResult.Success(EMPTY_PAGE)),
        ),
    ) : SessionsApi {
        val listAccessTokens = mutableListOf<String>()

        override suspend fun listSessions(
            endpoint: GatewayEndpoint,
            accessToken: String,
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionsResult<SessionPage> {
            listAccessTokens += accessToken
            return listResults.removeFirst()
        }

        override suspend fun getSession(
            endpoint: GatewayEndpoint,
            sessionKey: SessionKey,
            accessToken: String,
            profile: String?,
        ): SessionsResult<SessionProjection> = error("Unexpected detail request")

        override suspend fun getMessages(
            endpoint: GatewayEndpoint,
            sessionKey: SessionKey,
            accessToken: String,
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionsResult<SessionTranscript> = error("Unexpected transcript request")
    }

    private companion object {
        val EMPTY_PAGE = SessionPage(
            sessions = emptyList(),
            total = 0,
            limit = 20,
            offset = 0,
        )
    }
}
