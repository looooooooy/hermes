package app.hermesmobile.sessions

import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.auth.ScopedWebSocketTicket
import app.hermesmobile.protocol.auth.ScopedWebSocketTicketRequest
import app.hermesmobile.protocol.auth.WebSocketTicket
import app.hermesmobile.protocol.gateway.GatewayConnectionRole
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

fun interface WebSocketTicketMint {
    suspend fun mint(
        endpoint: GatewayEndpoint,
        accessToken: String,
    ): NativeAuthResult<WebSocketTicket>
}

fun interface ScopedWebSocketTicketMint {
    suspend fun mint(
        endpoint: GatewayEndpoint,
        accessToken: String,
        request: ScopedWebSocketTicketRequest,
    ): NativeAuthResult<ScopedWebSocketTicket>
}

fun interface CookieWebSocketTicketMint {
    suspend fun mint(endpoint: GatewayEndpoint): NativeAuthResult<WebSocketTicket>
}

fun interface ScopedCookieWebSocketTicketMint {
    suspend fun mint(
        endpoint: GatewayEndpoint,
        request: ScopedWebSocketTicketRequest,
    ): NativeAuthResult<ScopedWebSocketTicket>
}

sealed interface WebSocketTicketResult {
    data class Ready(val ticket: WebSocketTicket) : WebSocketTicketResult
    data object AuthenticationRequired : WebSocketTicketResult
    data class Unavailable(val summary: String) : WebSocketTicketResult
}

fun interface WebSocketTicketSource {
    suspend fun mint(clientInstanceId: ClientInstanceId): WebSocketTicketResult
}

sealed interface ScopedWebSocketTicketResult {
    data class Ready(val ticket: ScopedWebSocketTicket) : ScopedWebSocketTicketResult
    data object AuthenticationRequired : ScopedWebSocketTicketResult
    data class Unavailable(val summary: String) : ScopedWebSocketTicketResult
}

fun interface ScopedWebSocketTicketSource {
    suspend fun mint(request: ScopedWebSocketTicketRequest): ScopedWebSocketTicketResult
}

/** Issues short-lived, single-use WebSocket tickets without exposing bearer tokens to URLs. */
class AuthenticatedWebSocketTicketProvider(
    private val endpoint: GatewayEndpoint,
    private val tokenVault: TokenVault,
    private val tokenRefresh: TokenRefresh,
    private val ticketMint: WebSocketTicketMint,
    private val scopedTicketMint: ScopedWebSocketTicketMint? = null,
    private val cookieTicketMint: CookieWebSocketTicketMint? = null,
    private val scopedCookieTicketMint: ScopedCookieWebSocketTicketMint? = null,
    private val clockEpochSeconds: () -> Long = { System.currentTimeMillis() / 1_000L },
) : WebSocketTicketSource, ScopedWebSocketTicketSource {
    private val mutex = Mutex()
    private val endpointId = endpoint.baseUrl.toString()

    override suspend fun mint(clientInstanceId: ClientInstanceId): WebSocketTicketResult {
        if (scopedCookieTicketMint != null || scopedTicketMint != null) {
            val request = ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.OBSERVER,
                clientInstanceId = clientInstanceId,
            )
            return when (val scoped = mint(request)) {
                is ScopedWebSocketTicketResult.Ready -> WebSocketTicketResult.Ready(
                    WebSocketTicket(
                        value = scoped.ticket.value,
                        ttlSeconds = scoped.ticket.ttlSeconds,
                    ),
                )
                ScopedWebSocketTicketResult.AuthenticationRequired ->
                    WebSocketTicketResult.AuthenticationRequired
                is ScopedWebSocketTicketResult.Unavailable ->
                    WebSocketTicketResult.Unavailable(scoped.summary)
            }
        }
        return mintLegacy()
    }

    private suspend fun mintLegacy(): WebSocketTicketResult = mutex.withLock {
        when (val cookie = cookieTicketMint?.mint(endpoint)) {
            is NativeAuthResult.Success -> {
                return@withLock WebSocketTicketResult.Ready(cookie.value)
            }
            is NativeAuthResult.HttpFailure -> if (cookie.statusCode !in COOKIE_FALLBACK_STATUSES) {
                return@withLock WebSocketTicketResult.Unavailable(cookie.summary)
            }
            is NativeAuthResult.InvalidResponse -> {
                return@withLock WebSocketTicketResult.Unavailable(cookie.summary)
            }
            is NativeAuthResult.NetworkFailure -> {
                return@withLock WebSocketTicketResult.Unavailable(cookie.summary)
            }
            null -> Unit
        }
        val stored = tokenVault.load(endpointId)
            ?: return@withLock WebSocketTicketResult.AuthenticationRequired
        val current = if (stored.expiresAtEpochSeconds <= clockEpochSeconds() + REFRESH_SKEW_SECONDS) {
            when (val refresh = rotate(stored)) {
                is Rotation.Ready -> refresh.tokens
                Rotation.AuthenticationRequired -> {
                    return@withLock WebSocketTicketResult.AuthenticationRequired
                }
                is Rotation.Unavailable -> {
                    return@withLock WebSocketTicketResult.Unavailable(refresh.summary)
                }
            }
        } else {
            stored
        }

        when (val first = ticketMint.mint(endpoint, current.accessToken)) {
            is NativeAuthResult.Success -> WebSocketTicketResult.Ready(first.value)
            is NativeAuthResult.HttpFailure -> {
                if (first.statusCode == HTTP_UNAUTHORIZED) {
                    retryAfterUnauthorized(current)
                } else {
                    WebSocketTicketResult.Unavailable(first.summary)
                }
            }
            is NativeAuthResult.InvalidResponse -> WebSocketTicketResult.Unavailable(first.summary)
            is NativeAuthResult.NetworkFailure -> WebSocketTicketResult.Unavailable(first.summary)
        }
    }

    override suspend fun mint(
        request: ScopedWebSocketTicketRequest,
    ): ScopedWebSocketTicketResult = mutex.withLock {
        when (val cookie = scopedCookieTicketMint?.mint(endpoint, request)) {
            is NativeAuthResult.Success -> {
                if (!cookie.value.matches(request)) {
                    return@withLock ScopedWebSocketTicketResult.Unavailable(
                        "Hermes returned an invalid scoped WebSocket ticket.",
                    )
                }
                return@withLock ScopedWebSocketTicketResult.Ready(cookie.value)
            }
            is NativeAuthResult.HttpFailure -> if (cookie.statusCode !in COOKIE_FALLBACK_STATUSES) {
                return@withLock ScopedWebSocketTicketResult.Unavailable(cookie.summary)
            }
            is NativeAuthResult.InvalidResponse -> {
                return@withLock ScopedWebSocketTicketResult.Unavailable(cookie.summary)
            }
            is NativeAuthResult.NetworkFailure -> {
                return@withLock ScopedWebSocketTicketResult.Unavailable(cookie.summary)
            }
            null -> Unit
        }
        val mint = scopedTicketMint
            ?: return@withLock ScopedWebSocketTicketResult.Unavailable(
                "Hermes scoped WebSocket tickets are not configured.",
            )
        val stored = tokenVault.load(endpointId)
            ?: return@withLock ScopedWebSocketTicketResult.AuthenticationRequired
        val current = if (stored.expiresAtEpochSeconds <= clockEpochSeconds() + REFRESH_SKEW_SECONDS) {
            when (val refresh = rotate(stored)) {
                is Rotation.Ready -> refresh.tokens
                Rotation.AuthenticationRequired -> {
                    return@withLock ScopedWebSocketTicketResult.AuthenticationRequired
                }
                is Rotation.Unavailable -> {
                    return@withLock ScopedWebSocketTicketResult.Unavailable(refresh.summary)
                }
            }
        } else {
            stored
        }

        when (val first = mint.mint(endpoint, current.accessToken, request)) {
            is NativeAuthResult.Success -> first.value.toScopedResult(request)
            is NativeAuthResult.HttpFailure -> {
                if (first.statusCode == HTTP_UNAUTHORIZED) {
                    retryScopedAfterUnauthorized(current, request, mint)
                } else {
                    ScopedWebSocketTicketResult.Unavailable(first.summary)
                }
            }
            is NativeAuthResult.InvalidResponse ->
                ScopedWebSocketTicketResult.Unavailable(first.summary)
            is NativeAuthResult.NetworkFailure ->
                ScopedWebSocketTicketResult.Unavailable(first.summary)
        }
    }

    private suspend fun retryAfterUnauthorized(rejected: NativeTokens): WebSocketTicketResult =
        when (val refresh = rotate(rejected)) {
            is Rotation.Ready -> when (
                val retry = ticketMint.mint(endpoint, refresh.tokens.accessToken)
            ) {
                is NativeAuthResult.Success -> WebSocketTicketResult.Ready(retry.value)
                is NativeAuthResult.HttpFailure -> {
                    if (retry.statusCode == HTTP_UNAUTHORIZED) {
                        tokenVault.clear(endpointId)
                        WebSocketTicketResult.AuthenticationRequired
                    } else {
                        WebSocketTicketResult.Unavailable(retry.summary)
                    }
                }
                is NativeAuthResult.InvalidResponse -> WebSocketTicketResult.Unavailable(retry.summary)
                is NativeAuthResult.NetworkFailure -> WebSocketTicketResult.Unavailable(retry.summary)
            }
            Rotation.AuthenticationRequired -> WebSocketTicketResult.AuthenticationRequired
            is Rotation.Unavailable -> WebSocketTicketResult.Unavailable(refresh.summary)
        }

    private suspend fun retryScopedAfterUnauthorized(
        rejected: NativeTokens,
        request: ScopedWebSocketTicketRequest,
        mint: ScopedWebSocketTicketMint,
    ): ScopedWebSocketTicketResult = when (val refresh = rotate(rejected)) {
        is Rotation.Ready -> when (
            val retry = mint.mint(endpoint, refresh.tokens.accessToken, request)
        ) {
            is NativeAuthResult.Success -> retry.value.toScopedResult(request)
            is NativeAuthResult.HttpFailure -> {
                if (retry.statusCode == HTTP_UNAUTHORIZED) {
                    tokenVault.clear(endpointId)
                    ScopedWebSocketTicketResult.AuthenticationRequired
                } else {
                    ScopedWebSocketTicketResult.Unavailable(retry.summary)
                }
            }
            is NativeAuthResult.InvalidResponse ->
                ScopedWebSocketTicketResult.Unavailable(retry.summary)
            is NativeAuthResult.NetworkFailure ->
                ScopedWebSocketTicketResult.Unavailable(retry.summary)
        }
        Rotation.AuthenticationRequired -> ScopedWebSocketTicketResult.AuthenticationRequired
        is Rotation.Unavailable -> ScopedWebSocketTicketResult.Unavailable(refresh.summary)
    }

    private suspend fun rotate(current: NativeTokens): Rotation =
        when (val result = tokenRefresh.refresh(endpoint, current)) {
            is NativeAuthResult.Success -> {
                tokenVault.save(endpointId, result.value)
                Rotation.Ready(result.value)
            }
            is NativeAuthResult.HttpFailure -> {
                if (result.statusCode in INVALID_REFRESH_STATUS_CODES) {
                    tokenVault.clear(endpointId)
                    Rotation.AuthenticationRequired
                } else {
                    Rotation.Unavailable(result.summary)
                }
            }
            is NativeAuthResult.InvalidResponse -> Rotation.Unavailable(result.summary)
            is NativeAuthResult.NetworkFailure -> Rotation.Unavailable(result.summary)
        }

    private fun ScopedWebSocketTicket.toScopedResult(
        request: ScopedWebSocketTicketRequest,
    ): ScopedWebSocketTicketResult = if (matches(request)) {
        ScopedWebSocketTicketResult.Ready(this)
    } else {
        ScopedWebSocketTicketResult.Unavailable(
            "Hermes returned an invalid scoped WebSocket ticket.",
        )
    }

    private fun ScopedWebSocketTicket.matches(request: ScopedWebSocketTicketRequest): Boolean =
        connectionRole == request.connectionRole &&
            observerContractVersion == request.observerContractVersion

    private sealed interface Rotation {
        data class Ready(val tokens: NativeTokens) : Rotation
        data object AuthenticationRequired : Rotation
        data class Unavailable(val summary: String) : Rotation
    }

    private companion object {
        const val REFRESH_SKEW_SECONDS = 60L
        const val HTTP_UNAUTHORIZED = 401
        val COOKIE_FALLBACK_STATUSES = setOf(401, 403)
        val INVALID_REFRESH_STATUS_CODES = setOf(400, 401, 403)
    }
}
