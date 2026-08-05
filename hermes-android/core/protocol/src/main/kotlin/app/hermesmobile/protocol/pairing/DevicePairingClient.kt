package app.hermesmobile.protocol.pairing

import app.hermesmobile.protocol.GatewayEndpoint
import java.io.IOException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.ResponseBody

class PairingCode private constructor(
    internal val requestValue: String,
) {
    override fun equals(other: Any?): Boolean =
        other is PairingCode && requestValue == other.requestValue

    override fun hashCode(): Int = requestValue.hashCode()

    override fun toString(): String = "PairingCode([REDACTED])"

    companion object {
        fun fromUserInput(value: String): PairingCode {
            val compact = value
                .uppercase()
                .filterNot { it == '-' || it.isWhitespace() }
            require(compact.matches(PAIRING_CODE_COMPACT)) {
                "Pairing code must contain eight supported characters."
            }
            return PairingCode("${compact.take(4)}-${compact.drop(4)}")
        }

        private val PAIRING_CODE_COMPACT = Regex("^[0-9A-HJKMNP-TV-Z]{8}$")
    }
}

class PairingIdempotencyKey(value: String) {
    val value: String = canonicalUuid(value, "Idempotency key")

    override fun equals(other: Any?): Boolean =
        other is PairingIdempotencyKey && value == other.value

    override fun hashCode(): Int = value.hashCode()

    override fun toString(): String = "PairingIdempotencyKey($value)"

    companion object {
        fun random(): PairingIdempotencyKey = PairingIdempotencyKey(UUID.randomUUID().toString())
    }
}

class PairingRequestDigest internal constructor(
    private val bytes: ByteArray,
) {
    init {
        require(bytes.size == SHA256_BYTES)
    }

    override fun equals(other: Any?): Boolean =
        other is PairingRequestDigest && MessageDigest.isEqual(bytes, other.bytes)

    override fun hashCode(): Int = bytes.contentHashCode()

    override fun toString(): String = "PairingRequestDigest([REDACTED])"

    private companion object {
        const val SHA256_BYTES = 32
    }
}

class PairingSessionId(value: String) {
    val value: String = canonicalUuid(value, "Pairing session id")

    override fun equals(other: Any?): Boolean = other is PairingSessionId && value == other.value

    override fun hashCode(): Int = value.hashCode()

    override fun toString(): String = "PairingSessionId($value)"
}

class PairingDeviceId(value: String) {
    val value: String = canonicalUuid(value, "Pairing device id")

    override fun equals(other: Any?): Boolean = other is PairingDeviceId && value == other.value

    override fun hashCode(): Int = value.hashCode()

    override fun toString(): String = "PairingDeviceId($value)"
}

enum class PairingScope(val wireValue: String) {
    SESSION_OBSERVE("session.observe"),
    SESSION_CONTROL_REQUEST("session.control.request"),
}

enum class PairingSessionState {
    CLAIMED,
    CONFIRMED,
    EXPIRED,
    CANCELLED,
}

enum class PairingActivationState {
    WAITING_OWNER_CONFIRMATION,
    AWAITING_PROOF,
    ACTIVE,
    BLOCKED,
}

enum class PairingCancelReason(val wireValue: String) {
    OWNER_CANCELLED("owner_cancelled"),
    FINGERPRINT_MISMATCH("fingerprint_mismatch"),
}

enum class DeviceRevokeReason(val wireValue: String) {
    USER_REQUESTED("user_requested"),
    DEVICE_LOST("device_lost"),
    SECURITY_EVENT("security_event"),
}

enum class PairingErrorCode {
    PAIRING_INVALID_REQUEST,
    UNAUTHORIZED,
    FORBIDDEN,
    PAIRING_NOT_FOUND,
    PAIRING_STATE_CONFLICT,
    IDEMPOTENCY_CONFLICT,
    PAIRING_EXPIRED,
    PAIRING_CLAIM_UNAVAILABLE,
    PAIRING_CLAIM_RATE_LIMITED,
    RATE_LIMITED,
    UNKNOWN,
}

class ClaimPairingRequest(
    val pairingCode: PairingCode,
    workspaceId: String,
    agentId: String,
    val deviceDisplayName: String,
    val scopes: Set<PairingScope>,
    val expectedRevision: Int = 1,
) {
    val workspaceId: String = canonicalUuid(workspaceId, "Workspace id")
    val agentId: String = canonicalUuid(agentId, "Agent id")

    init {
        require(
            deviceDisplayName.isNotBlank() &&
                deviceDisplayName == deviceDisplayName.trim() &&
                deviceDisplayName.toByteArray(Charsets.UTF_8).size <= MAX_DEVICE_NAME_BYTES,
        ) {
            "Device display name must be trimmed and at most 128 UTF-8 bytes."
        }
        require(scopes.isNotEmpty() && scopes.size <= 2) {
            "At least one supported pairing scope is required."
        }
        require(expectedRevision >= 1) { "Expected revision must be positive." }
    }

    override fun toString(): String =
        "ClaimPairingRequest(pairingCode=[REDACTED], workspaceId=$workspaceId, " +
            "agentId=$agentId, deviceDisplayName=$deviceDisplayName, " +
            "scopes=$scopes, expectedRevision=$expectedRevision)"

    private companion object {
        const val MAX_DEVICE_NAME_BYTES = 128
    }
}

data class ConfirmPairingRequest(
    val credentialFingerprint: String,
    val expectedRevision: Int,
) {
    init {
        require(credentialFingerprint.matches(FINGERPRINT_PATTERN)) {
            "Credential fingerprint is invalid."
        }
        require(expectedRevision >= 1) { "Expected revision must be positive." }
    }
}

data class CancelPairingRequest(
    val reason: PairingCancelReason,
    val expectedRevision: Int,
) {
    init {
        require(expectedRevision >= 1) { "Expected revision must be positive." }
    }
}

data class RevokePairingDeviceRequest(
    val reason: DeviceRevokeReason,
    val expectedRevision: Int,
) {
    init {
        require(expectedRevision >= 1) { "Expected revision must be positive." }
    }
}

fun ClaimPairingRequest.normalizedRequestDigest(): PairingRequestDigest =
    normalizedPairingRequestDigest(
        "POST",
        "/api/device-pairing/claims",
        pairingCode.requestValue,
        workspaceId,
        agentId,
        deviceDisplayName,
        scopes.sortedBy(PairingScope::ordinal).joinToString(",") { it.wireValue },
        expectedRevision.toString(),
    )

fun ConfirmPairingRequest.normalizedRequestDigest(
    sessionId: PairingSessionId,
): PairingRequestDigest = normalizedPairingRequestDigest(
    "POST",
    "/api/device-pairing/sessions/${sessionId.value}/confirm",
    credentialFingerprint,
    expectedRevision.toString(),
)

fun CancelPairingRequest.normalizedRequestDigest(
    sessionId: PairingSessionId,
): PairingRequestDigest = normalizedPairingRequestDigest(
    "POST",
    "/api/device-pairing/sessions/${sessionId.value}/cancel",
    reason.wireValue,
    expectedRevision.toString(),
)

fun RevokePairingDeviceRequest.normalizedRequestDigest(
    deviceId: PairingDeviceId,
): PairingRequestDigest = normalizedPairingRequestDigest(
    "POST",
    "/api/devices/${deviceId.value}/revoke",
    reason.wireValue,
    expectedRevision.toString(),
)

data class PairingConnectorReview(
    val displayName: String,
    val platformFamily: String,
    val version: String,
    val keyAlgorithm: String,
)

data class PairingOwnerBinding(
    val tenantId: String,
    val userId: String,
    val workspaceId: String,
    val agentId: String,
    val deviceId: PairingDeviceId,
    val credentialId: String,
    val scopes: Set<PairingScope>,
)

data class PairingOwnerView(
    val pairingOfferId: String,
    val pairingSessionId: PairingSessionId,
    val state: PairingSessionState,
    val activationState: PairingActivationState,
    val connector: PairingConnectorReview,
    val binding: PairingOwnerBinding,
    val credentialFingerprint: String,
    val expiresAt: String,
    val revision: Int,
    val deviceRevision: Int,
)

data class RevokedPairingDevice(
    val deviceId: PairingDeviceId,
    val revision: Int,
    val revokedAt: String,
)

sealed interface PairingResult<out T> {
    data class Success<T>(val value: T) : PairingResult<T>

    data class HttpFailure(
        val statusCode: Int,
        val code: PairingErrorCode,
        val retryAfterSeconds: Int? = null,
        val summary: String = "Hermes device pairing returned HTTP $statusCode.",
    ) : PairingResult<Nothing>

    data class InvalidResponse(
        val summary: String = "Hermes returned an invalid device-pairing response.",
    ) : PairingResult<Nothing>

    data class NetworkFailure(
        val summary: String = "Hermes device pairing could not be reached.",
    ) : PairingResult<Nothing>
}

interface PairingOwnerApi {
    suspend fun claim(
        endpoint: GatewayEndpoint,
        accessToken: String,
        idempotencyKey: PairingIdempotencyKey,
        request: ClaimPairingRequest,
    ): PairingResult<PairingOwnerView>

    suspend fun confirm(
        endpoint: GatewayEndpoint,
        accessToken: String,
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        idempotencyKey: PairingIdempotencyKey,
        request: ConfirmPairingRequest,
    ): PairingResult<PairingOwnerView>

    suspend fun status(
        endpoint: GatewayEndpoint,
        accessToken: String,
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
    ): PairingResult<PairingOwnerView>

    suspend fun cancel(
        endpoint: GatewayEndpoint,
        accessToken: String,
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        idempotencyKey: PairingIdempotencyKey,
        request: CancelPairingRequest,
    ): PairingResult<PairingOwnerView>

    suspend fun revoke(
        endpoint: GatewayEndpoint,
        accessToken: String,
        deviceId: PairingDeviceId,
        idempotencyKey: PairingIdempotencyKey,
        request: RevokePairingDeviceRequest,
    ): PairingResult<RevokedPairingDevice>
}

@Serializable
private data class ClaimPairingPayload(
    @SerialName("pairing_code") val pairingCode: String,
    @SerialName("workspace_id") val workspaceId: String,
    @SerialName("agent_id") val agentId: String,
    @SerialName("device_display_name") val deviceDisplayName: String,
    val scopes: List<String>,
    @SerialName("expected_revision") val expectedRevision: Int,
)

@Serializable
private data class ConfirmPairingPayload(
    @SerialName("credential_fingerprint") val credentialFingerprint: String,
    @SerialName("expected_revision") val expectedRevision: Int,
)

@Serializable
private data class CancelPairingPayload(
    val reason: String,
    @SerialName("expected_revision") val expectedRevision: Int,
)

@Serializable
private data class RevokePairingDevicePayload(
    val reason: String,
    @SerialName("expected_revision") val expectedRevision: Int,
)

@Serializable
private data class PairingOwnerViewPayload(
    @SerialName("pairing_offer_id") val pairingOfferId: String,
    @SerialName("pairing_session_id") val pairingSessionId: String,
    val state: String,
    @SerialName("activation_state") val activationState: String,
    @SerialName("display_name") val displayName: String,
    @SerialName("platform_family") val platformFamily: String,
    @SerialName("connector_version") val connectorVersion: String,
    @SerialName("key_algorithm") val keyAlgorithm: String,
    val binding: PairingOwnerBindingPayload,
    @SerialName("credential_fingerprint") val credentialFingerprint: String,
    @SerialName("expires_at") val expiresAt: String,
    val revision: Int,
    @SerialName("device_revision") val deviceRevision: Int,
)

@Serializable
private data class PairingOwnerBindingPayload(
    @SerialName("tenant_id") val tenantId: String,
    @SerialName("user_id") val userId: String,
    @SerialName("workspace_id") val workspaceId: String,
    @SerialName("agent_id") val agentId: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("credential_id") val credentialId: String,
    val scopes: List<String>,
)

@Serializable
private data class RevokedPairingDevicePayload(
    @SerialName("device_id") val deviceId: String,
    val status: String,
    val revision: Int,
    @SerialName("revoked_at") val revokedAt: String,
)

@Serializable
private data class PairingErrorPayload(
    val code: String,
    val reason: String,
)

private enum class PairingOwnerOperation {
    CLAIM,
    CONFIRM,
    STATUS,
    CANCEL,
    REVOKE,
}

private data class ValidatedPairingError(
    val code: PairingErrorCode,
    val retryAfterSeconds: Int?,
)

class DevicePairingClient(
    private val httpClient: OkHttpClient,
    private val json: Json = Json {
        ignoreUnknownKeys = false
        isLenient = false
        coerceInputValues = false
        explicitNulls = false
    },
) : PairingOwnerApi {
    override suspend fun claim(
        endpoint: GatewayEndpoint,
        accessToken: String,
        idempotencyKey: PairingIdempotencyKey,
        request: ClaimPairingRequest,
    ): PairingResult<PairingOwnerView> {
        val body = json.encodeToString(
            ClaimPairingPayload(
                pairingCode = request.pairingCode.requestValue,
                workspaceId = request.workspaceId,
                agentId = request.agentId,
                deviceDisplayName = request.deviceDisplayName,
                scopes = request.scopes
                    .sortedBy(PairingScope::ordinal)
                    .map(PairingScope::wireValue),
                expectedRevision = request.expectedRevision,
            ),
        )
        return executeOwnerMutation(
            operation = PairingOwnerOperation.CLAIM,
            endpoint = endpoint,
            accessToken = accessToken,
            idempotencyKey = idempotencyKey,
            pathSegments = arrayOf("api", "device-pairing", "claims"),
            body = body,
            decode = { document ->
                json.decodeFromString<PairingOwnerViewPayload>(document)
                    .toDomain()
                    .requireClaimPostcondition(request)
            },
        )
    }

    override suspend fun confirm(
        endpoint: GatewayEndpoint,
        accessToken: String,
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        idempotencyKey: PairingIdempotencyKey,
        request: ConfirmPairingRequest,
    ): PairingResult<PairingOwnerView> {
        if (!expectedOwnerView.canConfirm(sessionId, request)) {
            return PairingResult.InvalidResponse()
        }
        return executeOwnerMutation(
            operation = PairingOwnerOperation.CONFIRM,
            endpoint = endpoint,
            accessToken = accessToken,
            idempotencyKey = idempotencyKey,
            pathSegments = arrayOf(
                "api",
                "device-pairing",
                "sessions",
                sessionId.value,
                "confirm",
            ),
            body = json.encodeToString(
                ConfirmPairingPayload(
                    credentialFingerprint = request.credentialFingerprint,
                    expectedRevision = request.expectedRevision,
                ),
            ),
            decode = { document ->
                json.decodeFromString<PairingOwnerViewPayload>(document)
                    .toDomain()
                    .requireConfirmPostcondition(sessionId, expectedOwnerView, request)
            },
        )
    }

    override suspend fun status(
        endpoint: GatewayEndpoint,
        accessToken: String,
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
    ): PairingResult<PairingOwnerView> {
        if (!expectedOwnerView.canReadStatus(sessionId)) {
            return PairingResult.InvalidResponse()
        }
        return executeOwnerRead(
            operation = PairingOwnerOperation.STATUS,
            endpoint = endpoint,
            accessToken = accessToken,
            pathSegments = arrayOf(
                "api",
                "device-pairing",
                "sessions",
                sessionId.value,
            ),
            decode = { document ->
                json.decodeFromString<PairingOwnerViewPayload>(document)
                    .toDomain()
                    .requireStatusPostcondition(sessionId, expectedOwnerView)
            },
        )
    }

    override suspend fun cancel(
        endpoint: GatewayEndpoint,
        accessToken: String,
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        idempotencyKey: PairingIdempotencyKey,
        request: CancelPairingRequest,
    ): PairingResult<PairingOwnerView> {
        if (!expectedOwnerView.canCancel(sessionId, request)) {
            return PairingResult.InvalidResponse()
        }
        return executeOwnerMutation(
            operation = PairingOwnerOperation.CANCEL,
            endpoint = endpoint,
            accessToken = accessToken,
            idempotencyKey = idempotencyKey,
            pathSegments = arrayOf(
                "api",
                "device-pairing",
                "sessions",
                sessionId.value,
                "cancel",
            ),
            body = json.encodeToString(
                CancelPairingPayload(
                    reason = request.reason.wireValue,
                    expectedRevision = request.expectedRevision,
                ),
            ),
            decode = { document ->
                json.decodeFromString<PairingOwnerViewPayload>(document)
                    .toDomain()
                    .requireCancelPostcondition(sessionId, expectedOwnerView, request)
            },
        )
    }

    override suspend fun revoke(
        endpoint: GatewayEndpoint,
        accessToken: String,
        deviceId: PairingDeviceId,
        idempotencyKey: PairingIdempotencyKey,
        request: RevokePairingDeviceRequest,
    ): PairingResult<RevokedPairingDevice> = executeOwnerMutation(
        operation = PairingOwnerOperation.REVOKE,
        endpoint = endpoint,
        accessToken = accessToken,
        idempotencyKey = idempotencyKey,
        pathSegments = arrayOf("api", "devices", deviceId.value, "revoke"),
        body = json.encodeToString(
            RevokePairingDevicePayload(
                reason = request.reason.wireValue,
                expectedRevision = request.expectedRevision,
            ),
        ),
        decode = { document ->
            json.decodeFromString<RevokedPairingDevicePayload>(document)
                .toDomain()
                .requireRevokePostcondition(deviceId, request)
        },
    )

    private suspend fun <T> executeOwnerMutation(
        operation: PairingOwnerOperation,
        endpoint: GatewayEndpoint,
        accessToken: String,
        idempotencyKey: PairingIdempotencyKey,
        pathSegments: Array<String>,
        body: String,
        decode: (String) -> T,
    ): PairingResult<T> {
        require(accessToken.isNotBlank()) { "Owner access token is required." }
        val request = Request.Builder()
            .url(endpoint.route(*pathSegments))
            .header("Accept", JSON_MEDIA_TYPE_VALUE)
            .header("Authorization", "Bearer $accessToken")
            .header(IDEMPOTENCY_HEADER, idempotencyKey.value)
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return executeJson(operation, request, decode)
    }

    private suspend fun <T> executeOwnerRead(
        operation: PairingOwnerOperation,
        endpoint: GatewayEndpoint,
        accessToken: String,
        pathSegments: Array<String>,
        decode: (String) -> T,
    ): PairingResult<T> {
        require(accessToken.isNotBlank()) { "Owner access token is required." }
        val request = Request.Builder()
            .url(endpoint.route(*pathSegments))
            .header("Accept", JSON_MEDIA_TYPE_VALUE)
            .header("Authorization", "Bearer $accessToken")
            .header("Cache-Control", "no-store")
            .get()
            .build()
        return executeJson(operation, request, decode)
    }

    private suspend fun <T> executeJson(
        operation: PairingOwnerOperation,
        request: Request,
        decode: (String) -> T,
    ): PairingResult<T> = withContext(Dispatchers.IO) {
        try {
            httpClient.newCall(request).execute().use { response ->
                if (response.isSuccessful && response.code != HTTP_OK) {
                    return@withContext PairingResult.InvalidResponse()
                }
                val document = response.body?.readLimitedDocument()
                if (!response.isSuccessful) {
                    if (document == null) {
                        return@withContext PairingResult.InvalidResponse()
                    }
                    val code = decodeErrorCode(document)
                        ?: return@withContext PairingResult.InvalidResponse()
                    val validated = operation.validateError(
                        statusCode = response.code,
                        code = code,
                        retryAfterValues = response.headers.values(RETRY_AFTER_HEADER),
                    ) ?: return@withContext PairingResult.InvalidResponse()
                    return@withContext PairingResult.HttpFailure(
                        statusCode = response.code,
                        code = validated.code,
                        retryAfterSeconds = validated.retryAfterSeconds,
                    )
                }
                if (document == null) {
                    return@withContext PairingResult.InvalidResponse()
                }
                try {
                    PairingResult.Success(decode(document))
                } catch (_: SerializationException) {
                    PairingResult.InvalidResponse()
                } catch (_: IllegalArgumentException) {
                    PairingResult.InvalidResponse()
                }
            }
        } catch (_: IOException) {
            PairingResult.NetworkFailure()
        }
    }

    private fun decodeErrorCode(document: String): PairingErrorCode? = try {
        val payload = json.decodeFromString<PairingErrorPayload>(document)
        require(payload.code.isNotBlank() && payload.code.length <= MAX_ERROR_CODE_LENGTH)
        require(payload.reason.isNotBlank() && payload.reason.length <= MAX_ERROR_REASON_LENGTH)
        PairingErrorCode.entries.firstOrNull { it.name == payload.code }
    } catch (_: SerializationException) {
        null
    } catch (_: IllegalArgumentException) {
        null
    }

    private fun PairingOwnerViewPayload.toDomain(): PairingOwnerView {
        require(pairingOfferId == canonicalUuid(pairingOfferId, "Pairing offer id"))
        require(displayName.isNotBlank() && displayName == displayName.trim())
        require(platformFamily.matches(PLATFORM_PATTERN))
        require(connectorVersion.matches(VERSION_PATTERN))
        require(keyAlgorithm == "Ed25519")
        require(credentialFingerprint.matches(FINGERPRINT_PATTERN))
        Instant.parse(expiresAt)
        require(revision >= 1)
        require(deviceRevision >= 1)
        return PairingOwnerView(
            pairingOfferId = pairingOfferId,
            pairingSessionId = PairingSessionId(pairingSessionId),
            state = state.toPairingSessionState(),
            activationState = activationState.toPairingActivationState(),
            connector = PairingConnectorReview(
                displayName = displayName,
                platformFamily = platformFamily,
                version = connectorVersion,
                keyAlgorithm = keyAlgorithm,
            ),
            binding = binding.toDomain(),
            credentialFingerprint = credentialFingerprint,
            expiresAt = expiresAt,
            revision = revision,
            deviceRevision = deviceRevision,
        )
    }

    private fun PairingOwnerBindingPayload.toDomain(): PairingOwnerBinding {
        val scopeSet = scopes.map { it.toPairingScope() }.toSet()
        require(scopeSet.size == scopes.size && scopeSet.isNotEmpty() && scopeSet.size <= 2)
        return PairingOwnerBinding(
            tenantId = canonicalUuid(tenantId, "Tenant id"),
            userId = canonicalUuid(userId, "User id"),
            workspaceId = canonicalUuid(workspaceId, "Workspace id"),
            agentId = canonicalUuid(agentId, "Agent id"),
            deviceId = PairingDeviceId(deviceId),
            credentialId = canonicalUuid(credentialId, "Credential id"),
            scopes = scopeSet,
        )
    }

    private fun RevokedPairingDevicePayload.toDomain(): RevokedPairingDevice {
        require(status == "revoked")
        require(revision >= 1)
        Instant.parse(revokedAt)
        return RevokedPairingDevice(
            deviceId = PairingDeviceId(deviceId),
            revision = revision,
            revokedAt = revokedAt,
        )
    }

    private fun PairingOwnerView.requireClaimPostcondition(
        request: ClaimPairingRequest,
    ): PairingOwnerView = apply {
        require(state == PairingSessionState.CLAIMED)
        require(activationState == PairingActivationState.WAITING_OWNER_CONFIRMATION)
        require(binding.workspaceId == request.workspaceId)
        require(binding.agentId == request.agentId)
        require(binding.scopes == request.scopes)
        require(revision.isNextAfter(request.expectedRevision))
    }

    private fun PairingOwnerView.canConfirm(
        sessionId: PairingSessionId,
        request: ConfirmPairingRequest,
    ): Boolean =
        pairingSessionId == sessionId &&
            state == PairingSessionState.CLAIMED &&
            activationState == PairingActivationState.WAITING_OWNER_CONFIRMATION &&
            credentialFingerprint.matches(FINGERPRINT_PATTERN) &&
            credentialFingerprint == request.credentialFingerprint &&
            revision == request.expectedRevision

    private fun PairingOwnerView.canReadStatus(
        sessionId: PairingSessionId,
    ): Boolean =
        pairingSessionId == sessionId &&
            state == PairingSessionState.CONFIRMED &&
            activationState == PairingActivationState.AWAITING_PROOF

    private fun PairingOwnerView.canCancel(
        sessionId: PairingSessionId,
        request: CancelPairingRequest,
    ): Boolean =
        pairingSessionId == sessionId &&
            (
                state == PairingSessionState.CLAIMED &&
                    activationState == PairingActivationState.WAITING_OWNER_CONFIRMATION ||
                    state == PairingSessionState.CONFIRMED &&
                    activationState == PairingActivationState.AWAITING_PROOF
                ) &&
            credentialFingerprint.matches(FINGERPRINT_PATTERN) &&
            revision == request.expectedRevision

    private fun PairingOwnerView.requireConfirmPostcondition(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        request: ConfirmPairingRequest,
    ): PairingOwnerView = apply {
        require(pairingSessionId == sessionId)
        requireImmutableOwnerSnapshot(expectedOwnerView)
        require(state == PairingSessionState.CONFIRMED)
        require(activationState == PairingActivationState.AWAITING_PROOF)
        require(credentialFingerprint == request.credentialFingerprint)
        require(revision.isNextAfter(request.expectedRevision))
    }

    private fun PairingOwnerView.requireStatusPostcondition(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
    ): PairingOwnerView = apply {
        require(pairingSessionId == sessionId)
        requireImmutableOwnerSnapshot(expectedOwnerView)
        require(revision >= expectedOwnerView.revision)
        require(deviceRevision >= expectedOwnerView.deviceRevision)
        require(
            state == PairingSessionState.CONFIRMED &&
                activationState in setOf(
                    PairingActivationState.AWAITING_PROOF,
                    PairingActivationState.ACTIVE,
                    PairingActivationState.BLOCKED,
                ) ||
                state in setOf(
                    PairingSessionState.CANCELLED,
                    PairingSessionState.EXPIRED,
                ) &&
                activationState == PairingActivationState.BLOCKED,
        )
    }

    private fun PairingOwnerView.requireCancelPostcondition(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        request: CancelPairingRequest,
    ): PairingOwnerView = apply {
        require(pairingSessionId == sessionId)
        requireImmutableOwnerSnapshot(expectedOwnerView)
        require(state == PairingSessionState.CANCELLED)
        require(activationState == PairingActivationState.BLOCKED)
        require(revision.isNextAfter(request.expectedRevision))
    }

    private fun PairingOwnerView.requireImmutableOwnerSnapshot(
        expected: PairingOwnerView,
    ) {
        require(pairingOfferId == expected.pairingOfferId)
        require(pairingSessionId == expected.pairingSessionId)
        require(connector == expected.connector)
        require(binding == expected.binding)
        require(credentialFingerprint == expected.credentialFingerprint)
        require(expiresAt == expected.expiresAt)
    }

    private fun RevokedPairingDevice.requireRevokePostcondition(
        requestedDeviceId: PairingDeviceId,
        request: RevokePairingDeviceRequest,
    ): RevokedPairingDevice = apply {
        require(deviceId == requestedDeviceId)
        require(revision.isNextAfter(request.expectedRevision))
    }

    private fun Int.isNextAfter(expectedRevision: Int): Boolean =
        toLong() == expectedRevision.toLong() + 1L

    private fun String.toPairingSessionState(): PairingSessionState = when (this) {
        "claimed" -> PairingSessionState.CLAIMED
        "confirmed" -> PairingSessionState.CONFIRMED
        "expired" -> PairingSessionState.EXPIRED
        "cancelled" -> PairingSessionState.CANCELLED
        else -> throw IllegalArgumentException("Unknown pairing state.")
    }

    private fun String.toPairingActivationState(): PairingActivationState = when (this) {
        "waiting_owner_confirmation" -> PairingActivationState.WAITING_OWNER_CONFIRMATION
        "awaiting_proof" -> PairingActivationState.AWAITING_PROOF
        "active" -> PairingActivationState.ACTIVE
        "blocked" -> PairingActivationState.BLOCKED
        else -> throw IllegalArgumentException("Unknown pairing activation state.")
    }

    private fun String.toPairingScope(): PairingScope =
        PairingScope.entries.firstOrNull { it.wireValue == this }
            ?: throw IllegalArgumentException("Unknown pairing scope.")

    private fun PairingOwnerOperation.validateError(
        statusCode: Int,
        code: PairingErrorCode,
        retryAfterValues: List<String>,
    ): ValidatedPairingError? {
        val allowed = when (this) {
            PairingOwnerOperation.CLAIM -> when (statusCode) {
                400 -> code == PairingErrorCode.PAIRING_INVALID_REQUEST
                401 -> code == PairingErrorCode.UNAUTHORIZED
                403 -> code == PairingErrorCode.FORBIDDEN
                404 -> code == PairingErrorCode.PAIRING_CLAIM_UNAVAILABLE
                409 -> code == PairingErrorCode.IDEMPOTENCY_CONFLICT
                429 -> code == PairingErrorCode.PAIRING_CLAIM_RATE_LIMITED
                else -> false
            }
            PairingOwnerOperation.CONFIRM,
            PairingOwnerOperation.CANCEL,
            -> when (statusCode) {
                401 -> code == PairingErrorCode.UNAUTHORIZED
                403 -> code == PairingErrorCode.FORBIDDEN
                404 -> code == PairingErrorCode.PAIRING_NOT_FOUND
                409 -> code in setOf(
                    PairingErrorCode.PAIRING_STATE_CONFLICT,
                    PairingErrorCode.IDEMPOTENCY_CONFLICT,
                )
                410 -> code == PairingErrorCode.PAIRING_EXPIRED
                429 -> code == PairingErrorCode.RATE_LIMITED
                else -> false
            }
            PairingOwnerOperation.STATUS -> when (statusCode) {
                401 -> code == PairingErrorCode.UNAUTHORIZED
                403 -> code == PairingErrorCode.FORBIDDEN
                404 -> code == PairingErrorCode.PAIRING_NOT_FOUND
                429 -> code == PairingErrorCode.RATE_LIMITED
                in 500..599 -> code == PairingErrorCode.UNKNOWN
                else -> false
            }
            PairingOwnerOperation.REVOKE -> when (statusCode) {
                401 -> code == PairingErrorCode.UNAUTHORIZED
                403 -> code == PairingErrorCode.FORBIDDEN
                404 -> code == PairingErrorCode.PAIRING_NOT_FOUND
                409 -> code in setOf(
                    PairingErrorCode.PAIRING_STATE_CONFLICT,
                    PairingErrorCode.IDEMPOTENCY_CONFLICT,
                )
                429 -> code == PairingErrorCode.RATE_LIMITED
                else -> false
            }
        }
        if (!allowed) return null
        val retryAfterSeconds = if (
            code == PairingErrorCode.PAIRING_CLAIM_RATE_LIMITED ||
            this == PairingOwnerOperation.STATUS &&
            code == PairingErrorCode.RATE_LIMITED
        ) {
            retryAfterValues.singleOrNull()
                ?.toIntOrNull()
                ?.takeIf { it in MIN_RETRY_AFTER_SECONDS..MAX_RETRY_AFTER_SECONDS }
                ?: return null
        } else {
            if (retryAfterValues.isNotEmpty()) return null
            null
        }
        return ValidatedPairingError(code, retryAfterSeconds)
    }

    private fun GatewayEndpoint.route(vararg segments: String): HttpUrl =
        baseUrl.newBuilder().apply {
            segments.forEach(::addPathSegment)
        }.build()

    private fun ResponseBody.readLimitedDocument(): String? {
        if (contentLength() > MAX_RESPONSE_BYTES) return null
        val source = source()
        source.request(MAX_RESPONSE_BYTES + 1L)
        if (source.buffer.size > MAX_RESPONSE_BYTES) return null
        return source.readUtf8()
    }

    private companion object {
        const val IDEMPOTENCY_HEADER = "Idempotency-Key"
        const val RETRY_AFTER_HEADER = "Retry-After"
        const val HTTP_OK = 200
        const val JSON_MEDIA_TYPE_VALUE = "application/json"
        val JSON_MEDIA_TYPE = JSON_MEDIA_TYPE_VALUE.toMediaType()
        const val MAX_RESPONSE_BYTES = 256L * 1024L
        const val MIN_RETRY_AFTER_SECONDS = 1
        const val MAX_RETRY_AFTER_SECONDS = 300
        const val MAX_ERROR_CODE_LENGTH = 128
        const val MAX_ERROR_REASON_LENGTH = 256
        val PLATFORM_PATTERN = Regex("^[a-z][a-z0-9_-]{0,31}$")
        val VERSION_PATTERN = Regex("^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
    }
}

private val FINGERPRINT_PATTERN = Regex("^SHA256:[A-Za-z0-9_-]{43}$")
private val CANONICAL_UUID_PATTERN =
    Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

private fun canonicalUuid(value: String, label: String): String {
    require(value.matches(CANONICAL_UUID_PATTERN)) { "$label must be a canonical UUID." }
    val parsed = runCatching { UUID.fromString(value) }.getOrNull()
    require(
        parsed != null &&
            parsed.toString() == value &&
            parsed.variant() == 2 &&
            parsed.version() in 1..5 &&
            parsed != NIL_UUID,
    ) {
        "$label must be a canonical non-nil UUID."
    }
    return parsed.toString()
}

private val NIL_UUID = UUID(0L, 0L)

private fun normalizedPairingRequestDigest(
    vararg fields: String,
): PairingRequestDigest {
    val digest = MessageDigest.getInstance("SHA-256")
    fields.forEach { field ->
        val bytes = field.toByteArray(Charsets.UTF_8)
        digest.update(ByteBuffer.allocate(Int.SIZE_BYTES).putInt(bytes.size).array())
        digest.update(bytes)
    }
    return PairingRequestDigest(digest.digest())
}
