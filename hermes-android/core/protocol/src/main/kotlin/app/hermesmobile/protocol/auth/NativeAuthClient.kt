package app.hermesmobile.protocol.auth

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.gateway.GatewayConnectionRole
import app.hermesmobile.protocol.sessions.SessionKey
import java.io.IOException
import java.net.URI
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.Cookie
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.ResponseBody

class NativeTokens(
    val accessToken: String,
    val refreshToken: String,
    val tokenType: String,
    val expiresAtEpochSeconds: Long,
    val provider: String,
    val userId: String,
) {
    override fun toString(): String =
        "NativeTokens(accessToken=[REDACTED], refreshToken=[REDACTED], " +
            "tokenType=$tokenType, expiresAtEpochSeconds=$expiresAtEpochSeconds, " +
            "provider=$provider, userId=$userId)"
}

class WebSocketTicket(
    val value: String,
    val ttlSeconds: Int,
) {
    override fun toString(): String =
        "WebSocketTicket(value=[REDACTED], ttlSeconds=$ttlSeconds)"
}

@JvmInline
value class ClientInstanceId private constructor(val value: String) {
    companion object {
        operator fun invoke(value: String): ClientInstanceId {
            val parsed = runCatching { UUID.fromString(value) }.getOrNull()
            require(parsed != null) {
                "Client instance id must be a canonical UUID."
            }
            return ClientInstanceId(parsed.toString())
        }
    }
}

data class ScopedWebSocketTicketRequest(
    val connectionRole: GatewayConnectionRole,
    val clientInstanceId: ClientInstanceId,
    val sessionKey: SessionKey? = null,
    val profile: String? = null,
    val observerContractVersion: Int? = if (connectionRole == GatewayConnectionRole.OBSERVER) 2 else null,
) {
    init {
        require(profile == null || profile.isNotBlank()) { "Profile must not be blank." }
        if (connectionRole == GatewayConnectionRole.CONTROL) {
            require(
                sessionKey != null &&
                    sessionKey.value == sessionKey.value.trim() &&
                    profile != null &&
                    profile == profile.trim()
            ) {
                "Control WebSocket tickets require immutable session and profile targets."
            }
            require(observerContractVersion == null) {
                "Control WebSocket tickets must not select an observer contract."
            }
        } else {
            require(sessionKey == null && profile == null) {
                "Observer WebSocket tickets must not include session or profile targets."
            }
            require(observerContractVersion == 1 || observerContractVersion == 2) {
                "Observer WebSocket tickets require an explicit supported contract version."
            }
        }
    }
}

class ScopedWebSocketTicket(
    val value: String,
    val ttlSeconds: Int,
    val connectionRole: GatewayConnectionRole,
    val observerContractVersion: Int? = null,
) {
    override fun toString(): String =
        "ScopedWebSocketTicket(value=[REDACTED], ttlSeconds=$ttlSeconds, connectionRole=$connectionRole, observerContractVersion=$observerContractVersion)"
}

sealed interface NativeAuthResult<out T> {
    data class Success<T>(val value: T) : NativeAuthResult<T>

    data class HttpFailure(
        val statusCode: Int,
        val summary: String = "Hermes authentication returned HTTP $statusCode.",
    ) : NativeAuthResult<Nothing>

    data class InvalidResponse(
        val summary: String = "Hermes returned an invalid authentication response.",
    ) : NativeAuthResult<Nothing>

    data class NetworkFailure(
        val summary: String = "Hermes authentication could not be reached.",
    ) : NativeAuthResult<Nothing>
}

@Serializable
private data class CodeExchangeBody(
    val code: String,
    @SerialName("code_verifier") val codeVerifier: String,
)

@Serializable
private data class RefreshBody(
    @SerialName("refresh_token") val refreshToken: String,
    val provider: String,
)

@Serializable
private data class PasswordLoginBody(
    val provider: String,
    val username: String,
    val password: String,
    val next: String,
)

@Serializable
private data class PasswordLoginPayload(
    val ok: Boolean,
)

@Serializable
private data class TokenPayload(
    @SerialName("access_token") val accessToken: String,
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("token_type") val tokenType: String,
    @SerialName("expires_at") val expiresAtEpochSeconds: Long,
    val provider: String,
    @SerialName("user_id") val userId: String,
)

@Serializable
private data class WebSocketTicketPayload(
    val ticket: String,
    @SerialName("ttl_seconds") val ttlSeconds: Int,
    @SerialName("connection_role") val connectionRole: String? = null,
    @SerialName("observer_contract") val observerContract: Int? = null,
)

@Serializable
private data class ScopedWebSocketTicketBody(
    @SerialName("connection_role") val connectionRole: String,
    @SerialName("client_instance_id") val clientInstanceId: String,
    @SerialName("session_key") val sessionKey: String? = null,
    val profile: String? = null,
    @SerialName("observer_contract") val observerContract: Int? = null,
)

class NativeAuthClient(
    private val httpClient: OkHttpClient,
    private val json: Json = Json { ignoreUnknownKeys = true },
) {
    fun authorizationUrl(
        endpoint: GatewayEndpoint,
        redirectUri: String,
        credentials: PkceCredentials,
        provider: String = "",
    ): HttpUrl {
        validateLoopbackRedirect(redirectUri)
        require(credentials.challenge.isNotBlank()) { "PKCE challenge is required." }
        require(credentials.state.isNotBlank()) { "OAuth state is required." }

        val builder = requireNotNull(endpoint.baseUrl.resolve("auth/native/authorize"))
            .newBuilder()
            .addQueryParameter("code_challenge", credentials.challenge)
            .addQueryParameter("code_challenge_method", "S256")
            .addQueryParameter("redirect_uri", redirectUri)
            .addQueryParameter("state", credentials.state)
        if (provider.isNotBlank()) {
            builder.addQueryParameter("provider", provider)
        }
        return builder.build()
    }

    suspend fun exchangeCode(
        endpoint: GatewayEndpoint,
        code: String,
        verifier: String,
    ): NativeAuthResult<NativeTokens> {
        require(code.isNotBlank()) { "Authorization code is required." }
        Pkce.challengeFor(verifier)
        val body = json.encodeToString(CodeExchangeBody(code, verifier))
        val request = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("auth/native/token")))
            .header("Accept", "application/json")
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return executeJson(request) { document ->
            json.decodeFromString<TokenPayload>(document).toTokens()
        }
    }

    suspend fun passwordLogin(
        endpoint: GatewayEndpoint,
        username: String,
        password: String,
        provider: String = "basic",
    ): NativeAuthResult<NativeTokens> {
        require(username.isNotBlank()) { "Username is required." }
        require(password.isNotEmpty()) { "Password is required." }
        require(provider.isNotBlank()) { "Password provider is required." }
        val body = json.encodeToString(
            PasswordLoginBody(
                provider = provider,
                username = username,
                password = password,
                next = "",
            ),
        )
        val request = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("auth/password-login")))
            .header("Accept", "application/json")
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return executePasswordLogin(request, username, provider)
    }

    suspend fun refresh(
        endpoint: GatewayEndpoint,
        current: NativeTokens,
    ): NativeAuthResult<NativeTokens> {
        require(current.refreshToken.isNotBlank()) { "Refresh token is required." }
        val body = json.encodeToString(
            RefreshBody(
                refreshToken = current.refreshToken,
                provider = current.provider,
            ),
        )
        val request = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("auth/native/refresh")))
            .header("Accept", "application/json")
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return executeJson(request) { document ->
            json.decodeFromString<TokenPayload>(document).toTokens()
        }
    }

    suspend fun mintWebSocketTicket(
        endpoint: GatewayEndpoint,
        accessToken: String,
    ): NativeAuthResult<WebSocketTicket> {
        require(accessToken.isNotBlank()) { "Access token is required." }
        val request = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("api/auth/ws-ticket")))
            .header("Accept", "application/json")
            .header("Authorization", "Bearer $accessToken")
            .post(EMPTY_JSON_BODY)
            .build()
        return executeJson(request) { document ->
            val payload = json.decodeFromString<WebSocketTicketPayload>(document)
            require(payload.ticket.isNotBlank() && payload.ttlSeconds > 0)
            WebSocketTicket(payload.ticket, payload.ttlSeconds)
        }
    }

    suspend fun mintCookieWebSocketTicket(
        endpoint: GatewayEndpoint,
    ): NativeAuthResult<WebSocketTicket> {
        val origin = endpoint.cookieOrigin()
            ?: return NativeAuthResult.InvalidResponse(
                "Hermes cookie session requires HTTPS.",
            )
        val request = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("api/auth/ws-ticket")))
            .header("Accept", "application/json")
            .header("Origin", origin)
            .post(EMPTY_JSON_BODY)
            .build()
        return executeJson(request) { document ->
            val payload = json.decodeFromString<WebSocketTicketPayload>(document)
            require(
                payload.ticket.isNotBlank() &&
                    payload.ttlSeconds > 0 &&
                    GatewayConnectionRole.fromWireValue(payload.connectionRole) ==
                    GatewayConnectionRole.OBSERVER,
            )
            WebSocketTicket(payload.ticket, payload.ttlSeconds)
        }
    }

    suspend fun mintWebSocketTicket(
        endpoint: GatewayEndpoint,
        accessToken: String,
        request: ScopedWebSocketTicketRequest,
    ): NativeAuthResult<ScopedWebSocketTicket> {
        require(accessToken.isNotBlank()) { "Access token is required." }
        val body = json.encodeToString(
            ScopedWebSocketTicketBody(
                connectionRole = request.connectionRole.wireValue,
                clientInstanceId = request.clientInstanceId.value,
                sessionKey = request.sessionKey?.value,
                profile = request.profile,
                observerContract = request.observerContractVersion?.takeIf { it == 2 },
            ),
        )
        val httpRequest = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("api/auth/ws-ticket")))
            .header("Accept", "application/json")
            .header("Authorization", "Bearer $accessToken")
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return executeJson(httpRequest) { document ->
            val payload = json.decodeFromString<WebSocketTicketPayload>(document)
            val echoedRole = GatewayConnectionRole.fromWireValue(payload.connectionRole)
            require(
                payload.ticket.isNotBlank() &&
                    payload.ttlSeconds > 0 &&
                    echoedRole == request.connectionRole &&
                    payload.observerContract == request.observerContractVersion?.takeIf { it == 2 },
            )
            ScopedWebSocketTicket(
                value = payload.ticket,
                ttlSeconds = payload.ttlSeconds,
                connectionRole = requireNotNull(echoedRole),
                observerContractVersion = request.observerContractVersion,
            )
        }
    }

    suspend fun mintCookieWebSocketTicket(
        endpoint: GatewayEndpoint,
        request: ScopedWebSocketTicketRequest,
    ): NativeAuthResult<ScopedWebSocketTicket> {
        val origin = endpoint.cookieOrigin()
            ?: return NativeAuthResult.InvalidResponse(
                "Hermes cookie session requires HTTPS.",
            )
        val body = json.encodeToString(
            ScopedWebSocketTicketBody(
                connectionRole = request.connectionRole.wireValue,
                clientInstanceId = request.clientInstanceId.value,
                sessionKey = request.sessionKey?.value,
                profile = request.profile,
                observerContract = request.observerContractVersion?.takeIf { it == 2 },
            ),
        )
        val httpRequest = Request.Builder()
            .url(requireNotNull(endpoint.baseUrl.resolve("api/auth/ws-ticket")))
            .header("Accept", "application/json")
            .header("Origin", origin)
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return executeJson(httpRequest) { document ->
            val payload = json.decodeFromString<WebSocketTicketPayload>(document)
            val echoedRole = GatewayConnectionRole.fromWireValue(payload.connectionRole)
            require(
                payload.ticket.isNotBlank() &&
                    payload.ttlSeconds > 0 &&
                    echoedRole == request.connectionRole &&
                    payload.observerContract == request.observerContractVersion?.takeIf { it == 2 },
            )
            ScopedWebSocketTicket(
                value = payload.ticket,
                ttlSeconds = payload.ttlSeconds,
                connectionRole = requireNotNull(echoedRole),
                observerContractVersion = request.observerContractVersion,
            )
        }
    }

    private suspend fun <T> executeJson(
        request: Request,
        decode: (String) -> T,
    ): NativeAuthResult<T> = withContext(Dispatchers.IO) {
        try {
            httpClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@withContext NativeAuthResult.HttpFailure(response.code)
                }
                val document = response.body?.readLimitedDocument()
                    ?: return@withContext NativeAuthResult.InvalidResponse()
                try {
                    NativeAuthResult.Success(decode(document))
                } catch (_: SerializationException) {
                    NativeAuthResult.InvalidResponse()
                } catch (_: IllegalArgumentException) {
                    NativeAuthResult.InvalidResponse()
                }
            }
        } catch (_: IOException) {
            NativeAuthResult.NetworkFailure()
        }
    }

    private suspend fun executePasswordLogin(
        request: Request,
        username: String,
        requestedProvider: String,
    ): NativeAuthResult<NativeTokens> = withContext(Dispatchers.IO) {
        try {
            httpClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@withContext NativeAuthResult.HttpFailure(response.code)
                }
                val document = response.body?.readLimitedDocument()
                    ?: return@withContext NativeAuthResult.InvalidResponse()
                try {
                    require(json.decodeFromString<PasswordLoginPayload>(document).ok)
                    val cookies = response.headers.values("Set-Cookie")
                        .mapNotNull { Cookie.parse(response.request.url, it) }
                    val access = cookies.firstOrNull {
                        it.name.endsWith(SESSION_ACCESS_COOKIE_SUFFIX)
                    }
                    val refresh = cookies.firstOrNull {
                        it.name.endsWith(SESSION_REFRESH_COOKIE_SUFFIX)
                    }
                    val provider = cookies.firstOrNull {
                        it.name.endsWith(SESSION_PROVIDER_COOKIE_SUFFIX)
                    }?.value.orEmpty().ifBlank { requestedProvider }
                    require(
                        access != null &&
                            refresh != null &&
                            access.value.isNotBlank() &&
                            refresh.value.isNotBlank() &&
                            provider.isNotBlank() &&
                            access.expiresAt > 0,
                    )
                    NativeAuthResult.Success(
                        NativeTokens(
                            accessToken = access.value,
                            refreshToken = refresh.value,
                            tokenType = "Bearer",
                            expiresAtEpochSeconds = access.expiresAt / 1_000L,
                            provider = provider,
                            userId = username,
                        ),
                    )
                } catch (_: SerializationException) {
                    NativeAuthResult.InvalidResponse()
                } catch (_: IllegalArgumentException) {
                    NativeAuthResult.InvalidResponse()
                }
            }
        } catch (_: IOException) {
            NativeAuthResult.NetworkFailure()
        }
    }

    private fun TokenPayload.toTokens(): NativeTokens {
        require(
            accessToken.isNotBlank() &&
                refreshToken.isNotBlank() &&
                tokenType.equals("Bearer", ignoreCase = true) &&
                expiresAtEpochSeconds > 0,
        )
        return NativeTokens(
            accessToken = accessToken,
            refreshToken = refreshToken,
            tokenType = tokenType,
            expiresAtEpochSeconds = expiresAtEpochSeconds,
            provider = provider,
            userId = userId,
        )
    }

    private fun validateLoopbackRedirect(raw: String) {
        val uri = runCatching { URI(raw) }.getOrNull()
        require(uri != null) { "Native redirect URI is invalid." }
        val loopbackHost = uri.host == "::1" ||
            uri.host == "127.0.0.1" ||
            uri.host?.matches(Regex("^127(?:\\.\\d{1,3}){3}$")) == true
        require(
            uri.scheme == "http" &&
                loopbackHost &&
                uri.port in 1..65535 &&
                uri.rawUserInfo == null &&
                uri.rawQuery == null &&
                uri.rawFragment == null,
        ) {
            "Native redirect URI must use an HTTP loopback IP literal and an ephemeral port."
        }
    }

    private fun ResponseBody.readLimitedDocument(): String? {
        if (contentLength() > MAX_RESPONSE_BYTES) return null
        val source = source()
        source.request(MAX_RESPONSE_BYTES + 1L)
        if (source.buffer.size > MAX_RESPONSE_BYTES) return null
        return source.readUtf8()
    }

    private companion object {
        val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
        val EMPTY_JSON_BODY = "{}".toRequestBody(JSON_MEDIA_TYPE)
        const val MAX_RESPONSE_BYTES = 256L * 1024L
        const val SESSION_ACCESS_COOKIE_SUFFIX = "hermes_session_at"
        const val SESSION_REFRESH_COOKIE_SUFFIX = "hermes_session_rt"
        const val SESSION_PROVIDER_COOKIE_SUFFIX = "hermes_session_provider"
    }
}

private fun GatewayEndpoint.cookieOrigin(): String? {
    if (!baseUrl.isHttps) return null
    val port = baseUrl.port.takeUnless { it == 443 } ?: -1
    return URI("https", null, baseUrl.host, port, null, null, null).toASCIIString()
}
