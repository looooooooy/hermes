package app.hermesmobile.sessions

import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionPage
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.SessionsApi
import app.hermesmobile.protocol.sessions.SessionsResult
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

fun interface TokenRefresh {
    suspend fun refresh(
        endpoint: GatewayEndpoint,
        current: NativeTokens,
    ): NativeAuthResult<NativeTokens>
}

sealed interface SessionRepositoryResult<out T> {
    data class Data<T>(val value: T) : SessionRepositoryResult<T>

    data object AuthenticationRequired : SessionRepositoryResult<Nothing>

    data class Unavailable(
        val summary: String,
    ) : SessionRepositoryResult<Nothing>
}

interface SessionTranscriptSource {
    suspend fun loadMessages(
        sessionKey: SessionKey,
        limit: Int = 200,
        offset: Int = 0,
        profile: String? = null,
    ): SessionRepositoryResult<SessionTranscript>
}

interface SessionBrowserSource : SessionTranscriptSource {
    suspend fun loadSessions(
        limit: Int = 20,
        offset: Int = 0,
        profile: String? = null,
    ): SessionRepositoryResult<SessionPage>
}

/**
 * Authenticated, read-only access to Hermes' authoritative SessionDB projection.
 *
 * This repository deliberately owns no local session truth. It refreshes access
 * credentials when needed, performs one retry after an HTTP 401, and returns the
 * latest server projection to callers.
 */
class AuthenticatedSessionsRepository(
    private val endpoint: GatewayEndpoint,
    private val tokenVault: TokenVault,
    private val sessionsApi: SessionsApi,
    private val tokenRefresh: TokenRefresh,
    private val authenticationRequired: Boolean = true,
    private val clockEpochSeconds: () -> Long = { System.currentTimeMillis() / 1_000L },
) : SessionBrowserSource {
    private val authenticationMutex = Mutex()
    private val endpointId = endpoint.baseUrl.toString()

    override suspend fun loadSessions(
        limit: Int,
        offset: Int,
        profile: String?,
    ): SessionRepositoryResult<SessionPage> = authenticatedRequest { accessToken ->
        sessionsApi.listSessions(endpoint, accessToken, limit, offset, profile)
    }

    suspend fun loadSession(
        sessionKey: SessionKey,
        profile: String? = null,
    ): SessionRepositoryResult<SessionProjection> = authenticatedRequest { accessToken ->
        sessionsApi.getSession(endpoint, sessionKey, accessToken, profile)
    }

    override suspend fun loadMessages(
        sessionKey: SessionKey,
        limit: Int,
        offset: Int,
        profile: String?,
    ): SessionRepositoryResult<SessionTranscript> = authenticatedRequest { accessToken ->
        sessionsApi.getMessages(endpoint, sessionKey, accessToken, limit, offset, profile)
    }

    private suspend fun <T> authenticatedRequest(
        request: suspend (String) -> SessionsResult<T>,
    ): SessionRepositoryResult<T> = authenticationMutex.withLock {
        val stored = tokenVault.load(endpointId)
        if (stored == null) {
            if (authenticationRequired) {
                return@withLock SessionRepositoryResult.AuthenticationRequired
            }
            return@withLock when (val anonymous = request("")) {
                is SessionsResult.Success -> SessionRepositoryResult.Data(anonymous.value)
                is SessionsResult.HttpFailure -> {
                    if (anonymous.statusCode == HTTP_UNAUTHORIZED) {
                        SessionRepositoryResult.AuthenticationRequired
                    } else {
                        anonymous.toUnavailable()
                    }
                }
                is SessionsResult.InvalidResponse ->
                    SessionRepositoryResult.Unavailable(anonymous.summary)
                is SessionsResult.NetworkFailure ->
                    SessionRepositoryResult.Unavailable(anonymous.summary)
            }
        }
        val initial = if (stored.shouldRefresh()) {
            when (val refresh = refresh(stored)) {
                is RefreshResult.Rotated -> refresh.tokens
                RefreshResult.AuthenticationRequired -> {
                    return@withLock SessionRepositoryResult.AuthenticationRequired
                }
                is RefreshResult.Unavailable -> {
                    return@withLock SessionRepositoryResult.Unavailable(refresh.summary)
                }
            }
        } else {
            stored
        }

        when (val first = request(initial.accessToken)) {
            is SessionsResult.Success -> SessionRepositoryResult.Data(first.value)
            is SessionsResult.HttpFailure -> {
                if (first.statusCode != HTTP_UNAUTHORIZED) {
                    first.toUnavailable()
                } else {
                    retryAfterUnauthorized(initial, request)
                }
            }
            is SessionsResult.InvalidResponse -> SessionRepositoryResult.Unavailable(first.summary)
            is SessionsResult.NetworkFailure -> SessionRepositoryResult.Unavailable(first.summary)
        }
    }

    private suspend fun <T> retryAfterUnauthorized(
        rejected: NativeTokens,
        request: suspend (String) -> SessionsResult<T>,
    ): SessionRepositoryResult<T> = when (val refresh = refresh(rejected)) {
        is RefreshResult.Rotated -> when (val retry = request(refresh.tokens.accessToken)) {
            is SessionsResult.Success -> SessionRepositoryResult.Data(retry.value)
            is SessionsResult.HttpFailure -> {
                if (retry.statusCode == HTTP_UNAUTHORIZED) {
                    tokenVault.clear(endpointId)
                    SessionRepositoryResult.AuthenticationRequired
                } else {
                    retry.toUnavailable()
                }
            }
            is SessionsResult.InvalidResponse -> SessionRepositoryResult.Unavailable(retry.summary)
            is SessionsResult.NetworkFailure -> SessionRepositoryResult.Unavailable(retry.summary)
        }
        RefreshResult.AuthenticationRequired -> SessionRepositoryResult.AuthenticationRequired
        is RefreshResult.Unavailable -> SessionRepositoryResult.Unavailable(refresh.summary)
    }

    private suspend fun refresh(current: NativeTokens): RefreshResult =
        when (val result = tokenRefresh.refresh(endpoint, current)) {
            is NativeAuthResult.Success -> {
                tokenVault.save(endpointId, result.value)
                RefreshResult.Rotated(result.value)
            }
            is NativeAuthResult.HttpFailure -> {
                if (result.statusCode in INVALID_REFRESH_STATUS_CODES) {
                    tokenVault.clear(endpointId)
                    RefreshResult.AuthenticationRequired
                } else {
                    RefreshResult.Unavailable(result.summary)
                }
            }
            is NativeAuthResult.InvalidResponse -> RefreshResult.Unavailable(result.summary)
            is NativeAuthResult.NetworkFailure -> RefreshResult.Unavailable(result.summary)
        }

    private fun NativeTokens.shouldRefresh(): Boolean =
        expiresAtEpochSeconds <= clockEpochSeconds() + REFRESH_SKEW_SECONDS

    private fun SessionsResult.HttpFailure.toUnavailable(): SessionRepositoryResult.Unavailable =
        SessionRepositoryResult.Unavailable(summary)

    private sealed interface RefreshResult {
        data class Rotated(val tokens: NativeTokens) : RefreshResult
        data object AuthenticationRequired : RefreshResult
        data class Unavailable(val summary: String) : RefreshResult
    }

    private companion object {
        const val REFRESH_SKEW_SECONDS = 60L
        const val HTTP_UNAUTHORIZED = 401
        val INVALID_REFRESH_STATUS_CODES = setOf(400, 401, 403)
    }
}
