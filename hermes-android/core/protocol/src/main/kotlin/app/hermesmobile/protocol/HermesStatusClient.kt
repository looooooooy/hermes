package app.hermesmobile.protocol

import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.ResponseBody

@Serializable
private data class GatewayStatusPayload(
    val version: String? = null,
    @SerialName("release_date") val releaseDate: String? = null,
    @SerialName("gateway_running") val gatewayRunning: Boolean = false,
    @SerialName("gateway_state") val gatewayState: String? = null,
    @SerialName("auth_required") val authRequired: Boolean = false,
    @SerialName("auth_providers") val authProviders: Set<String> = emptySet(),
    @SerialName("auth_flows") val authFlows: Set<String> = emptySet(),
    val overall: String? = null,
)

data class GatewayStatus(
    val version: String?,
    val releaseDate: String?,
    val gatewayRunning: Boolean,
    val gatewayState: String?,
    val authRequired: Boolean,
    val authProviders: Set<String> = emptySet(),
    val authFlows: Set<String>,
    val overall: String?,
) {
    val supportsNativePkce: Boolean
        get() = "native_pkce" in authFlows

    val supportsPassword: Boolean
        get() = "basic" in authProviders
}

sealed interface DiscoveryResult {
    data class Reachable(val status: GatewayStatus) : DiscoveryResult

    data class HttpFailure(
        val statusCode: Int,
        val summary: String = "Hermes returned HTTP $statusCode.",
    ) : DiscoveryResult

    data class InvalidResponse(
        val summary: String = "The server response is not a compatible Hermes status document.",
    ) : DiscoveryResult

    data class NetworkFailure(
        val summary: String = "Hermes could not be reached.",
    ) : DiscoveryResult
}

fun interface GatewayDiscovery {
    suspend fun discover(endpoint: GatewayEndpoint): DiscoveryResult
}

class HermesStatusClient(
    private val httpClient: OkHttpClient,
    private val json: Json = Json { ignoreUnknownKeys = true },
) : GatewayDiscovery {
    override suspend fun discover(endpoint: GatewayEndpoint): DiscoveryResult = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(endpoint.statusUrl)
            .header("Accept", "application/json")
            .get()
            .build()

        try {
            httpClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@withContext DiscoveryResult.HttpFailure(response.code)
                }

                val document = response.body?.readStatusDocument()
                    ?: return@withContext DiscoveryResult.InvalidResponse()
                val payload = try {
                    json.decodeFromString<GatewayStatusPayload>(document)
                } catch (_: SerializationException) {
                    return@withContext DiscoveryResult.InvalidResponse()
                } catch (_: IllegalArgumentException) {
                    return@withContext DiscoveryResult.InvalidResponse()
                }

                DiscoveryResult.Reachable(
                    GatewayStatus(
                        version = payload.version,
                        releaseDate = payload.releaseDate,
                        gatewayRunning = payload.gatewayRunning,
                        gatewayState = payload.gatewayState,
                        authRequired = payload.authRequired,
                        authProviders = payload.authProviders,
                        authFlows = payload.authFlows,
                        overall = payload.overall,
                    ),
                )
            }
        } catch (_: IOException) {
            DiscoveryResult.NetworkFailure()
        }
    }

    private fun ResponseBody.readStatusDocument(): String? {
        if (contentLength() > MAX_STATUS_BYTES) return null
        val source = source()
        source.request(MAX_STATUS_BYTES + 1L)
        if (source.buffer.size > MAX_STATUS_BYTES) return null
        return source.readUtf8()
    }

    private companion object {
        const val MAX_STATUS_BYTES = 256L * 1024L
    }
}
