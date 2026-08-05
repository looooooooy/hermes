package app.hermesmobile.pairing

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import app.hermesmobile.protocol.pairing.CancelPairingRequest
import app.hermesmobile.protocol.pairing.ClaimPairingRequest
import app.hermesmobile.protocol.pairing.ConfirmPairingRequest
import app.hermesmobile.protocol.pairing.DeviceRevokeReason
import app.hermesmobile.protocol.pairing.PairingCancelReason
import app.hermesmobile.protocol.pairing.PairingDeviceId
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingSessionId
import app.hermesmobile.protocol.pairing.PairingCode
import app.hermesmobile.protocol.pairing.RevokePairingDeviceRequest
import app.hermesmobile.protocol.pairing.RevokedPairingDevice
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlin.math.max

sealed interface PairingActionResult<out T> {
    data class Data<T>(val value: T) : PairingActionResult<T>

    data class Failed(val failure: PairingFailure) : PairingActionResult<Nothing>
}

interface PairingOwnerActions {
    suspend fun claim(request: ClaimPairingRequest): PairingActionResult<PairingOwnerView>

    suspend fun confirm(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        request: ConfirmPairingRequest,
    ): PairingActionResult<PairingOwnerView>

    suspend fun status(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
    ): PairingActionResult<PairingOwnerView>

    suspend fun cancel(
        sessionId: PairingSessionId,
        expectedOwnerView: PairingOwnerView,
        request: CancelPairingRequest,
    ): PairingActionResult<PairingOwnerView>

    suspend fun revoke(
        deviceId: PairingDeviceId,
        request: RevokePairingDeviceRequest,
    ): PairingActionResult<RevokedPairingDevice>
}

class DevicePairingViewModel(
    private val actions: PairingOwnerActions,
) : ViewModel() {
    private val mutableState = MutableStateFlow(DevicePairingUiState())
    private var pendingOperation: PendingOperation? = null
    private var statusPollJob: Job? = null
    private var statusFlowGeneration = 0L
    private var isVisible = true
    val state: StateFlow<DevicePairingUiState> = mutableState.asStateFlow()

    fun onPairingCodeChanged(value: String) {
        if (pendingOperation != null) return
        dispatch(DevicePairingEvent.PairingCodeChanged(value))
    }

    fun onWorkspaceIdChanged(value: String) {
        if (pendingOperation != null) return
        dispatch(DevicePairingEvent.WorkspaceIdChanged(value))
    }

    fun onAgentIdChanged(value: String) {
        if (pendingOperation != null) return
        dispatch(DevicePairingEvent.AgentIdChanged(value))
    }

    fun onDeviceDisplayNameChanged(value: String) {
        if (pendingOperation != null) return
        dispatch(DevicePairingEvent.DeviceDisplayNameChanged(value))
    }

    fun onRequestControlScopeChanged(enabled: Boolean) {
        if (pendingOperation != null) return
        dispatch(DevicePairingEvent.RequestControlScopeChanged(enabled))
    }

    fun onFingerprintVerificationChanged(checked: Boolean) {
        if (pendingOperation != null) return
        dispatch(DevicePairingEvent.FingerprintVerificationChanged(checked))
    }

    fun claim() {
        val current = mutableState.value
        if (!current.canClaim) return
        val request = ClaimPairingRequest(
            pairingCode = PairingCode.fromUserInput(current.pairingCodeInput),
            workspaceId = current.workspaceIdInput,
            agentId = current.agentIdInput,
            deviceDisplayName = current.deviceDisplayNameInput,
            scopes = current.selectedScopes,
        )
        start(PendingOperation.Claim(request), DevicePairingEvent.ClaimStarted)
    }

    fun confirm() {
        val current = mutableState.value
        if (!current.canConfirm) return
        val view = checkNotNull(current.ownerView)
        start(
            PendingOperation.Confirm(
                sessionId = view.pairingSessionId,
                expectedOwnerView = view,
                request = ConfirmPairingRequest(
                    credentialFingerprint = view.credentialFingerprint,
                    expectedRevision = view.revision,
                ),
            ),
            DevicePairingEvent.ConfirmStarted,
        )
    }

    fun cancel() {
        cancel(PairingCancelReason.OWNER_CANCELLED)
    }

    fun rejectFingerprint() {
        cancel(PairingCancelReason.FINGERPRINT_MISMATCH)
    }

    private fun cancel(reason: PairingCancelReason) {
        val current = mutableState.value
        if (!current.canCancel) return
        val view = checkNotNull(current.ownerView)
        start(
            PendingOperation.Cancel(
                sessionId = view.pairingSessionId,
                expectedOwnerView = view,
                request = CancelPairingRequest(
                    reason = reason,
                    expectedRevision = view.revision,
                ),
            ),
            DevicePairingEvent.CancelStarted,
        )
    }

    fun revoke() {
        val current = mutableState.value
        if (!current.canRevoke) return
        val view = checkNotNull(current.ownerView)
        start(
            PendingOperation.Revoke(
                deviceId = view.binding.deviceId,
                request = RevokePairingDeviceRequest(
                    reason = DeviceRevokeReason.USER_REQUESTED,
                    expectedRevision = view.deviceRevision,
                ),
            ),
            DevicePairingEvent.RevokeStarted,
        )
    }

    fun retryPending() {
        val pending = pendingOperation ?: return
        if (!mutableState.value.canRetryPending) return
        dispatch(DevicePairingEvent.RetryStarted)
        viewModelScope.launch {
            execute(pending)
        }
    }

    fun reset() {
        if (mutableState.value.isBusy || pendingOperation != null) return
        invalidateStatusPolling()
        dispatch(DevicePairingEvent.Reset)
    }

    fun onVisible() {
        isVisible = true
        syncStatusPolling()
    }

    fun onHidden() {
        isVisible = false
        invalidateStatusPolling()
    }

    private fun start(
        pending: PendingOperation,
        event: DevicePairingEvent,
    ) {
        invalidateStatusPolling()
        pendingOperation = pending
        dispatch(event)
        viewModelScope.launch {
            execute(pending)
        }
    }

    private suspend fun execute(pending: PendingOperation) {
        when (pending) {
            is PendingOperation.Claim ->
                complete(pending, actions.claim(pending.request))
            is PendingOperation.Confirm ->
                complete(
                    pending,
                    actions.confirm(
                        pending.sessionId,
                        pending.expectedOwnerView,
                        pending.request,
                    ),
                )
            is PendingOperation.Cancel ->
                complete(
                    pending,
                    actions.cancel(
                        pending.sessionId,
                        pending.expectedOwnerView,
                        pending.request,
                    ),
                )
            is PendingOperation.Revoke ->
                completeRevoke(
                    pending,
                    actions.revoke(pending.deviceId, pending.request),
                )
        }
    }

    private fun complete(
        pending: PendingOperation,
        result: PairingActionResult<PairingOwnerView>,
    ) {
        clearPendingUnlessRetryableBlock(pending, result)
        dispatch(result.toEvent())
        syncStatusPolling()
    }

    private fun completeRevoke(
        pending: PendingOperation.Revoke,
        result: PairingActionResult<RevokedPairingDevice>,
    ) {
        clearPendingUnlessRetryableBlock(pending, result)
        when (result) {
            is PairingActionResult.Data -> dispatch(
                DevicePairingEvent.DeviceRevoked(
                    deviceId = result.value.deviceId.value,
                    revokedAt = result.value.revokedAt,
                ),
            )
            is PairingActionResult.Failed -> dispatch(
                DevicePairingEvent.Failed(result.failure),
            )
        }
    }

    private fun clearPendingUnlessRetryableBlock(
        pending: PendingOperation,
        result: PairingActionResult<*>,
    ) {
        val failure = (result as? PairingActionResult.Failed)?.failure
        val keepFrozen = failure == PairingFailure.DeliveryUnknown ||
            (
                mutableState.value.pendingOperation != null &&
                    failure in RETRYABLE_PENDING_BLOCKS
                )
        if (!keepFrozen && pendingOperation === pending) {
            pendingOperation = null
        }
    }

    private fun dispatch(event: DevicePairingEvent) {
        mutableState.value = DevicePairingReducer.reduce(mutableState.value, event)
    }

    private fun syncStatusPolling() {
        val snapshot = mutableState.value.ownerView
        if (
            !isVisible ||
            mutableState.value.phase != DevicePairingPhase.AWAITING_CONNECTOR_PROOF ||
            snapshot == null ||
            statusPollJob?.isActive == true
        ) {
            return
        }
        val generation = ++statusFlowGeneration
        statusPollJob = viewModelScope.launch {
            pollStatus(generation, snapshot)
        }
    }

    private suspend fun pollStatus(
        generation: Long,
        initialSnapshot: PairingOwnerView,
    ) {
        var expectedSnapshot = initialSnapshot
        var nextDelayMillis = STATUS_POLL_INTERVAL_MILLIS
        var consecutiveTransientFailures = 0
        while (
            generation == statusFlowGeneration &&
            isVisible &&
            mutableState.value.phase == DevicePairingPhase.AWAITING_CONNECTOR_PROOF
        ) {
            delay(nextDelayMillis)
            nextDelayMillis = STATUS_POLL_INTERVAL_MILLIS
            val result = actions.status(
                sessionId = expectedSnapshot.pairingSessionId,
                expectedOwnerView = expectedSnapshot,
            )
            if (
                generation != statusFlowGeneration ||
                !isVisible ||
                mutableState.value.ownerView?.pairingSessionId !=
                expectedSnapshot.pairingSessionId
            ) {
                return
            }
            when (result) {
                is PairingActionResult.Data -> {
                    consecutiveTransientFailures = 0
                    expectedSnapshot = result.value
                    dispatch(DevicePairingEvent.OwnerViewReceived(result.value))
                    if (
                        mutableState.value.phase !=
                        DevicePairingPhase.AWAITING_CONNECTOR_PROOF
                    ) {
                        return
                    }
                }
                is PairingActionResult.Failed -> when (val failure = result.failure) {
                    PairingFailure.Unavailable -> {
                        consecutiveTransientFailures += 1
                        if (consecutiveTransientFailures >= MAX_STATUS_TRANSIENT_FAILURES) {
                            dispatch(DevicePairingEvent.Failed(PairingFailure.Unavailable))
                            return
                        }
                    }
                    is PairingFailure.Contract -> when {
                        failure.code == PairingErrorCode.UNKNOWN -> {
                            consecutiveTransientFailures += 1
                            if (
                                consecutiveTransientFailures >=
                                MAX_STATUS_TRANSIENT_FAILURES
                            ) {
                                dispatch(DevicePairingEvent.Failed(PairingFailure.Unavailable))
                                return
                            }
                        }
                        failure.code == PairingErrorCode.RATE_LIMITED &&
                            failure.retryAfterSeconds != null -> {
                            nextDelayMillis = max(
                                STATUS_POLL_INTERVAL_MILLIS,
                                failure.retryAfterSeconds.toLong() * 1_000L,
                            )
                        }
                        else -> {
                            dispatch(DevicePairingEvent.Failed(failure))
                            return
                        }
                    }
                    else -> {
                        dispatch(DevicePairingEvent.Failed(failure))
                        return
                    }
                }
            }
        }
    }

    private fun invalidateStatusPolling() {
        statusFlowGeneration += 1
        statusPollJob?.cancel()
        statusPollJob = null
    }

    private fun PairingActionResult<PairingOwnerView>.toEvent(): DevicePairingEvent = when (this) {
        is PairingActionResult.Data -> DevicePairingEvent.OwnerViewReceived(value)
        is PairingActionResult.Failed -> DevicePairingEvent.Failed(failure)
    }

    private sealed interface PendingOperation {
        data class Claim(
            val request: ClaimPairingRequest,
        ) : PendingOperation

        data class Confirm(
            val sessionId: PairingSessionId,
            val expectedOwnerView: PairingOwnerView,
            val request: ConfirmPairingRequest,
        ) : PendingOperation

        data class Cancel(
            val sessionId: PairingSessionId,
            val expectedOwnerView: PairingOwnerView,
            val request: CancelPairingRequest,
        ) : PendingOperation

        data class Revoke(
            val deviceId: PairingDeviceId,
            val request: RevokePairingDeviceRequest,
        ) : PendingOperation
    }

    private companion object {
        const val STATUS_POLL_INTERVAL_MILLIS = 1_000L
        const val MAX_STATUS_TRANSIENT_FAILURES = 3
        val RETRYABLE_PENDING_BLOCKS = setOf(
            PairingFailure.AuthenticationRequired,
            PairingFailure.Unavailable,
        )
    }
}
