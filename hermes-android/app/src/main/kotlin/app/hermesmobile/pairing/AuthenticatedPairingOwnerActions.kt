package app.hermesmobile.pairing

import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.pairing.CancelPairingRequest
import app.hermesmobile.protocol.pairing.ClaimPairingRequest
import app.hermesmobile.protocol.pairing.ConfirmPairingRequest
import app.hermesmobile.protocol.pairing.PairingDeviceId
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingIdempotencyKey
import app.hermesmobile.protocol.pairing.PairingOwnerApi
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingRequestDigest
import app.hermesmobile.protocol.pairing.PairingResult
import app.hermesmobile.protocol.pairing.PairingSessionId
import app.hermesmobile.protocol.pairing.RevokePairingDeviceRequest
import app.hermesmobile.protocol.pairing.RevokedPairingDevice
import app.hermesmobile.protocol.pairing.normalizedRequestDigest
import app.hermesmobile.sessions.TokenRefresh
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

class AuthenticatedPairingOwnerActions(
    private val endpoint: GatewayEndpoint,
    private val tokenVault: TokenVault,
    private val pairingApi: PairingOwnerApi,
    private val tokenRefresh: TokenRefresh,
    private val clockEpochSeconds: () -> Long = { System.currentTimeMillis() / 1_000L },
) : PairingOwnerActions {
    private val endpointId = endpoint.baseUrl.toString()
    private val authenticationMutex = Mutex()
    private var pendingMutation: PendingMutation? = null

    override suspend fun claim(
        request: ClaimPairingRequest,
    ): PairingActionResult<PairingOwnerView> =
        authenticatedMutation(request.normalizedRequestDigest()) {
                accessToken,
                idempotencyKey,
            ->
            pairingApi.claim(
                endpoint = endpoint,
                accessToken = accessToken,
                idempotencyKey = idempotencyKey,
                request = request,
            )
        }

    override suspend fun confirm(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        request: ConfirmPairingRequest,
    ): PairingActionResult<PairingOwnerView> =
        authenticatedMutation(request.normalizedRequestDigest(sessionId)) {
                accessToken,
                idempotencyKey,
            ->
            pairingApi.confirm(
                endpoint = endpoint,
                accessToken = accessToken,
                sessionId = sessionId,
                expectedOwnerView = expectedOwnerView,
                idempotencyKey = idempotencyKey,
                request = request,
            )
        }

    override suspend fun status(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
    ): PairingActionResult<PairingOwnerView> =
        authenticatedRead { accessToken ->
            pairingApi.status(
                endpoint = endpoint,
                accessToken = accessToken,
                sessionId = sessionId,
                expectedOwnerView = expectedOwnerView,
            )
        }

    override suspend fun cancel(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        request: CancelPairingRequest,
    ): PairingActionResult<PairingOwnerView> =
        authenticatedMutation(request.normalizedRequestDigest(sessionId)) {
                accessToken,
                idempotencyKey,
            ->
            pairingApi.cancel(
                endpoint = endpoint,
                accessToken = accessToken,
                sessionId = sessionId,
                expectedOwnerView = expectedOwnerView,
                idempotencyKey = idempotencyKey,
                request = request,
            )
        }

    override suspend fun revoke(
        deviceId: PairingDeviceId,
        request: RevokePairingDeviceRequest,
    ): PairingActionResult<RevokedPairingDevice> =
        authenticatedMutation(request.normalizedRequestDigest(deviceId)) {
                accessToken,
                idempotencyKey,
            ->
            pairingApi.revoke(
                endpoint = endpoint,
                accessToken = accessToken,
                deviceId = deviceId,
                idempotencyKey = idempotencyKey,
                request = request,
            )
        }

    private suspend fun <T> authenticatedMutation(
        digest: PairingRequestDigest,
        mutation: suspend (
            accessToken: String,
            idempotencyKey: PairingIdempotencyKey,
        ) -> PairingResult<T>,
    ): PairingActionResult<T> = authenticationMutex.withLock {
        val pending = pendingMutation
        if (pending != null && pending.digest != digest) {
            return@withLock PairingActionResult.Failed(PairingFailure.InvalidResponse)
        }
        val idempotencyKey = pending?.idempotencyKey ?: PairingIdempotencyKey.random()
        if (pending == null) {
            pendingMutation = PendingMutation(digest, idempotencyKey)
        }
        val stored = tokenVault.load(endpointId)
            ?: return@withLock PairingActionResult.Failed(
                PairingFailure.AuthenticationRequired,
            )
        val initial = if (stored.shouldRefresh()) {
            when (val refresh = refresh(stored)) {
                is RefreshResult.Rotated -> refresh.tokens
                RefreshResult.AuthenticationRequired -> {
                    return@withLock PairingActionResult.Failed(
                        PairingFailure.AuthenticationRequired,
                    )
                }
                is RefreshResult.Unavailable -> {
                    return@withLock PairingActionResult.Failed(PairingFailure.Unavailable)
                }
            }
        } else {
            stored
        }

        when (val first = mutation(initial.accessToken, idempotencyKey)) {
            is PairingResult.Success -> {
                clearPending(digest)
                PairingActionResult.Data(first.value)
            }
            is PairingResult.HttpFailure -> if (first.isUnauthorized()) {
                retryAfterUnauthorized(
                    rejected = initial,
                    digest = digest,
                    idempotencyKey = idempotencyKey,
                    mutation = mutation,
                )
            } else {
                clearPending(digest)
                first.toActionFailure()
            }
            is PairingResult.InvalidResponse ->
                deliveryUnknown(digest, idempotencyKey)
            is PairingResult.NetworkFailure ->
                deliveryUnknown(digest, idempotencyKey)
        }
    }

    private suspend fun <T> authenticatedRead(
        read: suspend (accessToken: String) -> PairingResult<T>,
    ): PairingActionResult<T> = authenticationMutex.withLock {
        val stored = tokenVault.load(endpointId)
            ?: return@withLock PairingActionResult.Failed(
                PairingFailure.AuthenticationRequired,
            )
        val initial = if (stored.shouldRefresh()) {
            when (val refresh = refresh(stored)) {
                is RefreshResult.Rotated -> refresh.tokens
                RefreshResult.AuthenticationRequired -> {
                    return@withLock PairingActionResult.Failed(
                        PairingFailure.AuthenticationRequired,
                    )
                }
                is RefreshResult.Unavailable -> {
                    return@withLock PairingActionResult.Failed(PairingFailure.Unavailable)
                }
            }
        } else {
            stored
        }
        when (val first = read(initial.accessToken)) {
            is PairingResult.Success -> PairingActionResult.Data(first.value)
            is PairingResult.HttpFailure -> if (first.isUnauthorized()) {
                retryReadAfterUnauthorized(initial, read)
            } else {
                first.toActionFailure()
            }
            is PairingResult.InvalidResponse ->
                PairingActionResult.Failed(PairingFailure.InvalidResponse)
            is PairingResult.NetworkFailure ->
                PairingActionResult.Failed(PairingFailure.Unavailable)
        }
    }

    private suspend fun <T> retryReadAfterUnauthorized(
        rejected: NativeTokens,
        read: suspend (accessToken: String) -> PairingResult<T>,
    ): PairingActionResult<T> = when (val refresh = refresh(rejected)) {
        is RefreshResult.Rotated -> when (val retry = read(refresh.tokens.accessToken)) {
            is PairingResult.Success -> PairingActionResult.Data(retry.value)
            is PairingResult.HttpFailure -> if (retry.isUnauthorized()) {
                tokenVault.clear(endpointId)
                PairingActionResult.Failed(PairingFailure.AuthenticationRequired)
            } else {
                retry.toActionFailure()
            }
            is PairingResult.InvalidResponse ->
                PairingActionResult.Failed(PairingFailure.InvalidResponse)
            is PairingResult.NetworkFailure ->
                PairingActionResult.Failed(PairingFailure.Unavailable)
        }
        RefreshResult.AuthenticationRequired ->
            PairingActionResult.Failed(PairingFailure.AuthenticationRequired)
        is RefreshResult.Unavailable ->
            PairingActionResult.Failed(PairingFailure.Unavailable)
    }

    private suspend fun <T> retryAfterUnauthorized(
        rejected: NativeTokens,
        digest: PairingRequestDigest,
        idempotencyKey: PairingIdempotencyKey,
        mutation: suspend (
            accessToken: String,
            idempotencyKey: PairingIdempotencyKey,
        ) -> PairingResult<T>,
    ): PairingActionResult<T> = when (val refresh = refresh(rejected)) {
        is RefreshResult.Rotated -> when (
            val retry = mutation(refresh.tokens.accessToken, idempotencyKey)
        ) {
            is PairingResult.Success -> {
                clearPending(digest)
                PairingActionResult.Data(retry.value)
            }
            is PairingResult.HttpFailure -> if (retry.isUnauthorized()) {
                tokenVault.clear(endpointId)
                PairingActionResult.Failed(PairingFailure.AuthenticationRequired)
            } else {
                clearPending(digest)
                retry.toActionFailure()
            }
            is PairingResult.InvalidResponse ->
                deliveryUnknown(digest, idempotencyKey)
            is PairingResult.NetworkFailure ->
                deliveryUnknown(digest, idempotencyKey)
        }
        RefreshResult.AuthenticationRequired ->
            PairingActionResult.Failed(PairingFailure.AuthenticationRequired)
        is RefreshResult.Unavailable ->
            PairingActionResult.Failed(PairingFailure.Unavailable)
    }

    private fun deliveryUnknown(
        digest: PairingRequestDigest,
        idempotencyKey: PairingIdempotencyKey,
    ): PairingActionResult.Failed {
        pendingMutation = PendingMutation(digest, idempotencyKey)
        return PairingActionResult.Failed(PairingFailure.DeliveryUnknown)
    }

    private fun clearPending(digest: PairingRequestDigest) {
        if (pendingMutation?.digest == digest) {
            pendingMutation = null
        }
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
                    RefreshResult.Unavailable
                }
            }
            is NativeAuthResult.InvalidResponse,
            is NativeAuthResult.NetworkFailure,
            -> RefreshResult.Unavailable
        }

    private fun NativeTokens.shouldRefresh(): Boolean =
        expiresAtEpochSeconds <= clockEpochSeconds() + REFRESH_SKEW_SECONDS

    private fun PairingResult.HttpFailure.isUnauthorized(): Boolean =
        statusCode == HTTP_UNAUTHORIZED || code == PairingErrorCode.UNAUTHORIZED

    private fun PairingResult.HttpFailure.toActionFailure(): PairingActionResult.Failed =
        PairingActionResult.Failed(PairingFailure.Contract(code, retryAfterSeconds))

    private sealed interface RefreshResult {
        data class Rotated(val tokens: NativeTokens) : RefreshResult

        data object AuthenticationRequired : RefreshResult

        data object Unavailable : RefreshResult
    }

    private data class PendingMutation(
        val digest: PairingRequestDigest,
        val idempotencyKey: PairingIdempotencyKey,
    )

    private companion object {
        const val HTTP_UNAUTHORIZED = 401
        const val REFRESH_SKEW_SECONDS = 60L
        val INVALID_REFRESH_STATUS_CODES = setOf(400, 401, 403)
    }
}
