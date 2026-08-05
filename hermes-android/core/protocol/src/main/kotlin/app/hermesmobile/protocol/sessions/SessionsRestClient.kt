package app.hermesmobile.protocol.sessions

import app.hermesmobile.protocol.GatewayEndpoint
import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.ResponseBody

@JvmInline
value class SessionKey(val value: String) {
    init {
        require(value.isNotBlank()) { "Session key must not be blank." }
    }
}

@JvmInline
value class RuntimeSessionId(val value: String) {
    init {
        require(value.isNotBlank()) { "Runtime session id must not be blank." }
    }
}

data class SessionProjection(
    val sessionKey: SessionKey,
    val lineageRoot: SessionKey,
    val lineageTip: SessionKey,
    val parentSessionKey: SessionKey?,
    val title: String?,
    val preview: String?,
    val source: String?,
    val model: String?,
    val profile: String?,
    val cwd: String?,
    val gitBranch: String?,
    val startedAtEpochSeconds: Double,
    val endedAtEpochSeconds: Double?,
    val lastActiveEpochSeconds: Double,
    val messageCount: Int,
    val toolCallCount: Int,
    val inputTokens: Long,
    val outputTokens: Long,
    val isActive: Boolean,
    val archived: Boolean,
)

data class SessionPage(
    val sessions: List<SessionProjection>,
    val total: Int,
    val limit: Int,
    val offset: Int,
)

data class SessionMessageProjection(
    val messageId: Long?,
    val role: String,
    val content: JsonElement?,
    val timestampEpochSeconds: Double?,
    val reasoning: String?,
    val reasoningContent: String?,
    val reasoningDetails: JsonElement?,
    val toolCallId: String?,
    val toolCalls: JsonElement?,
    val toolName: String?,
    val displayKind: String?,
    val displayMetadata: JsonElement?,
)

data class TranscriptPagination(
    val limit: Int?,
    val offset: Int,
    val returned: Int,
)

data class SessionTranscript(
    val sessionKey: SessionKey,
    val lineageTip: SessionKey,
    val messages: List<SessionMessageProjection>,
    val pagination: TranscriptPagination,
)

sealed interface SessionsResult<out T> {
    data class Success<T>(val value: T) : SessionsResult<T>

    data class HttpFailure(
        val statusCode: Int,
        val summary: String = "Hermes Sessions API returned HTTP $statusCode.",
    ) : SessionsResult<Nothing>

    data class InvalidResponse(
        val summary: String = "Hermes returned an invalid Sessions API response.",
    ) : SessionsResult<Nothing>

    data class NetworkFailure(
        val summary: String = "Hermes sessions could not be reached.",
    ) : SessionsResult<Nothing>
}

@Serializable
private data class SessionPagePayload(
    val sessions: List<SessionPayload> = emptyList(),
    val total: Int = 0,
    val limit: Int = 0,
    val offset: Int = 0,
)

@Serializable
private data class SessionPayload(
    val id: String,
    @SerialName("_lineage_root_id") val lineageRootId: String? = null,
    @SerialName("parent_session_id") val parentSessionId: String? = null,
    val title: String? = null,
    val preview: String? = null,
    val source: String? = null,
    val model: String? = null,
    val profile: String? = null,
    val cwd: String? = null,
    @SerialName("git_branch") val gitBranch: String? = null,
    @SerialName("started_at") val startedAt: Double = 0.0,
    @SerialName("ended_at") val endedAt: Double? = null,
    @SerialName("last_active") val lastActive: Double? = null,
    @SerialName("message_count") val messageCount: Int = 0,
    @SerialName("tool_call_count") val toolCallCount: Int = 0,
    @SerialName("input_tokens") val inputTokens: Long = 0,
    @SerialName("output_tokens") val outputTokens: Long = 0,
    @SerialName("is_active") val isActive: Boolean = false,
    val archived: Boolean = false,
)

@Serializable
private data class SessionMessagesPayload(
    @SerialName("session_id") val sessionId: String,
    val messages: List<SessionMessagePayload> = emptyList(),
    val pagination: TranscriptPaginationPayload = TranscriptPaginationPayload(),
)

@Serializable
private data class TranscriptPaginationPayload(
    val limit: Int? = null,
    val offset: Int = 0,
    val returned: Int = 0,
)

@Serializable
private data class SessionMessagePayload(
    val id: Long? = null,
    val role: String,
    val content: JsonElement? = null,
    val timestamp: Double? = null,
    val reasoning: String? = null,
    @SerialName("reasoning_content") val reasoningContent: String? = null,
    @SerialName("reasoning_details") val reasoningDetails: JsonElement? = null,
    @SerialName("tool_call_id") val toolCallId: String? = null,
    @SerialName("tool_calls") val toolCalls: JsonElement? = null,
    @SerialName("tool_name") val toolName: String? = null,
    @SerialName("display_kind") val displayKind: String? = null,
    @SerialName("display_metadata") val displayMetadata: JsonElement? = null,
)

interface SessionsApi {
    suspend fun listSessions(
        endpoint: GatewayEndpoint,
        accessToken: String,
        limit: Int = 20,
        offset: Int = 0,
        profile: String? = null,
    ): SessionsResult<SessionPage>

    suspend fun getSession(
        endpoint: GatewayEndpoint,
        sessionKey: SessionKey,
        accessToken: String,
        profile: String? = null,
    ): SessionsResult<SessionProjection>

    suspend fun getMessages(
        endpoint: GatewayEndpoint,
        sessionKey: SessionKey,
        accessToken: String,
        limit: Int = 200,
        offset: Int = 0,
        profile: String? = null,
    ): SessionsResult<SessionTranscript>
}

class SessionsRestClient(
    private val httpClient: OkHttpClient,
    private val json: Json = Json { ignoreUnknownKeys = true },
) : SessionsApi {
    override suspend fun listSessions(
        endpoint: GatewayEndpoint,
        accessToken: String,
        limit: Int,
        offset: Int,
        profile: String?,
    ): SessionsResult<SessionPage> {
        require(limit in 1..MAX_LIST_PAGE_SIZE) { "Session page size is out of range." }
        require(offset >= 0) { "Session page offset must not be negative." }
        val url = endpoint.route("api", "sessions").newBuilder()
            .addQueryParameter("limit", limit.toString())
            .addQueryParameter("offset", offset.toString())
            .addQueryParameter("min_messages", "1")
            .addQueryParameter("archived", "exclude")
            .addQueryParameter("order", "recent")
            .apply {
                profile?.takeIf(String::isNotBlank)?.let {
                    addQueryParameter("profile", it)
                }
            }
            .build()
        return executeJson(authenticatedGet(url, accessToken)) { document ->
            val payload = json.decodeFromString<SessionPagePayload>(document)
            SessionPage(
                sessions = payload.sessions.map { it.toProjection() },
                total = payload.total,
                limit = payload.limit,
                offset = payload.offset,
            )
        }
    }

    override suspend fun getSession(
        endpoint: GatewayEndpoint,
        sessionKey: SessionKey,
        accessToken: String,
        profile: String?,
    ): SessionsResult<SessionProjection> {
        val url = endpoint.route("api", "sessions", sessionKey.value)
            .withOptionalProfile(profile)
        return executeJson(authenticatedGet(url, accessToken)) { document ->
            json.decodeFromString<SessionPayload>(document).toProjection()
        }
    }

    override suspend fun getMessages(
        endpoint: GatewayEndpoint,
        sessionKey: SessionKey,
        accessToken: String,
        limit: Int,
        offset: Int,
        profile: String?,
    ): SessionsResult<SessionTranscript> {
        require(limit in 1..MAX_TRANSCRIPT_PAGE_SIZE) { "Transcript page size is out of range." }
        require(offset >= 0) { "Transcript offset must not be negative." }
        val url = endpoint.route("api", "sessions", sessionKey.value, "messages")
            .newBuilder()
            .addQueryParameter("limit", limit.toString())
            .addQueryParameter("offset", offset.toString())
            .apply {
                profile?.takeIf(String::isNotBlank)?.let {
                    addQueryParameter("profile", it)
                }
            }
            .build()
        return executeJson(authenticatedGet(url, accessToken)) { document ->
            val payload = json.decodeFromString<SessionMessagesPayload>(document)
            require(payload.sessionId.isNotBlank())
            SessionTranscript(
                sessionKey = sessionKey,
                lineageTip = SessionKey(payload.sessionId),
                messages = payload.messages.map { it.toProjection() },
                pagination = TranscriptPagination(
                    limit = payload.pagination.limit,
                    offset = payload.pagination.offset,
                    returned = payload.pagination.returned,
                ),
            )
        }
    }

    private fun authenticatedGet(url: HttpUrl, accessToken: String): Request =
        Request.Builder()
            .url(url)
            .header("Accept", "application/json")
            .apply {
                if (accessToken.isNotBlank()) {
                    header("Authorization", "Bearer $accessToken")
                }
            }
            .get()
            .build()

    private suspend fun <T> executeJson(
        request: Request,
        decode: (String) -> T,
    ): SessionsResult<T> = withContext(Dispatchers.IO) {
        try {
            httpClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@withContext SessionsResult.HttpFailure(response.code)
                }
                val document = response.body?.readLimitedDocument()
                    ?: return@withContext SessionsResult.InvalidResponse()
                try {
                    SessionsResult.Success(decode(document))
                } catch (_: SerializationException) {
                    SessionsResult.InvalidResponse()
                } catch (_: IllegalArgumentException) {
                    SessionsResult.InvalidResponse()
                }
            }
        } catch (_: IOException) {
            SessionsResult.NetworkFailure()
        }
    }

    private fun GatewayEndpoint.route(vararg segments: String): HttpUrl =
        baseUrl.newBuilder().apply {
            segments.forEach(::addPathSegment)
        }.build()

    private fun HttpUrl.withOptionalProfile(profile: String?): HttpUrl =
        newBuilder().apply {
            profile?.takeIf(String::isNotBlank)?.let {
                addQueryParameter("profile", it)
            }
        }.build()

    private fun SessionPayload.toProjection(): SessionProjection {
        require(id.isNotBlank())
        val root = SessionKey(lineageRootId?.takeIf(String::isNotBlank) ?: id)
        return SessionProjection(
            sessionKey = root,
            lineageRoot = root,
            lineageTip = SessionKey(id),
            parentSessionKey = parentSessionId?.takeIf(String::isNotBlank)?.let(::SessionKey),
            title = title,
            preview = preview,
            source = source,
            model = model,
            profile = profile,
            cwd = cwd,
            gitBranch = gitBranch,
            startedAtEpochSeconds = startedAt,
            endedAtEpochSeconds = endedAt,
            lastActiveEpochSeconds = lastActive ?: startedAt,
            messageCount = messageCount,
            toolCallCount = toolCallCount,
            inputTokens = inputTokens,
            outputTokens = outputTokens,
            isActive = isActive,
            archived = archived,
        )
    }

    private fun SessionMessagePayload.toProjection(): SessionMessageProjection {
        require(role.isNotBlank())
        return SessionMessageProjection(
            messageId = id,
            role = role,
            content = content,
            timestampEpochSeconds = timestamp,
            reasoning = reasoning,
            reasoningContent = reasoningContent,
            reasoningDetails = reasoningDetails,
            toolCallId = toolCallId,
            toolCalls = toolCalls,
            toolName = toolName,
            displayKind = displayKind,
            displayMetadata = displayMetadata,
        )
    }

    private fun ResponseBody.readLimitedDocument(): String? {
        if (contentLength() > MAX_RESPONSE_BYTES) return null
        val source = source()
        source.request(MAX_RESPONSE_BYTES + 1L)
        if (source.buffer.size > MAX_RESPONSE_BYTES) return null
        return source.readUtf8()
    }

    private companion object {
        const val DEFAULT_PAGE_SIZE = 20
        const val MAX_LIST_PAGE_SIZE = 500
        const val DEFAULT_TRANSCRIPT_PAGE_SIZE = 200
        const val MAX_TRANSCRIPT_PAGE_SIZE = 500
        const val MAX_RESPONSE_BYTES = 4L * 1024L * 1024L
    }
}
