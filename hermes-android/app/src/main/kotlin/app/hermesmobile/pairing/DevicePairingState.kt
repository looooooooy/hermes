package app.hermesmobile.pairing

import app.hermesmobile.protocol.pairing.ClaimPairingRequest
import app.hermesmobile.protocol.pairing.PairingCode
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingScope
import app.hermesmobile.protocol.pairing.PairingSessionState
import app.hermesmobile.protocol.pairing.PairingActivationState

enum class DevicePairingPhase {
    ENTER_CODE,
    CLAIMING,
    REVIEW,
    CONFIRMING,
    AWAITING_CONNECTOR_PROOF,
    ACTIVE,
    CANCELLING,
    CANCELLED,
    REVOKING,
    REVOKED,
    BLOCKED,
    EXPIRED,
    CLAIM_RATE_LIMITED,
    AUTHENTICATION_REQUIRED,
    DELIVERY_UNKNOWN,
    ERROR,
}

enum class PairingOperationKind {
    CLAIM,
    CONFIRM,
    CANCEL,
    REVOKE,
}

data class DevicePairingUiState(
    val phase: DevicePairingPhase = DevicePairingPhase.ENTER_CODE,
    val pairingCodeInput: String = "",
    val workspaceIdInput: String = "",
    val agentIdInput: String = "",
    val deviceDisplayNameInput: String = "",
    val requestControlScope: Boolean = false,
    val ownerView: PairingOwnerView? = null,
    val fingerprintVerified: Boolean = false,
    val failure: PairingFailure? = null,
    val revokedAt: String? = null,
    val pendingOperation: PairingOperationKind? = null,
) {
    val selectedScopes: Set<PairingScope>
        get() = buildSet {
            add(PairingScope.SESSION_OBSERVE)
            if (requestControlScope) add(PairingScope.SESSION_CONTROL_REQUEST)
        }

    val isBusy: Boolean
        get() = phase in setOf(
            DevicePairingPhase.CLAIMING,
            DevicePairingPhase.CONFIRMING,
            DevicePairingPhase.CANCELLING,
            DevicePairingPhase.REVOKING,
        )

    val canClaim: Boolean
        get() = phase == DevicePairingPhase.ENTER_CODE &&
            runCatching {
                ClaimPairingRequest(
                    pairingCode = PairingCode.fromUserInput(pairingCodeInput),
                    workspaceId = workspaceIdInput,
                    agentId = agentIdInput,
                    deviceDisplayName = deviceDisplayNameInput,
                    scopes = selectedScopes,
                )
            }.isSuccess

    val canConfirm: Boolean
        get() = phase == DevicePairingPhase.REVIEW &&
            ownerView != null &&
            fingerprintVerified

    val canCancel: Boolean
        get() = phase in setOf(
            DevicePairingPhase.REVIEW,
            DevicePairingPhase.AWAITING_CONNECTOR_PROOF,
        ) && ownerView != null

    val canRevoke: Boolean
        get() = phase in setOf(
            DevicePairingPhase.AWAITING_CONNECTOR_PROOF,
            DevicePairingPhase.ACTIVE,
        ) && ownerView != null

    val canRetryPending: Boolean
        get() = pendingOperation != null &&
            phase in setOf(
                DevicePairingPhase.DELIVERY_UNKNOWN,
                DevicePairingPhase.AUTHENTICATION_REQUIRED,
                DevicePairingPhase.ERROR,
            )

    val fixedExpiresAt: String?
        get() = ownerView?.expiresAt

    /**
     * Pairing grants an authorized scope to request control; it never grants a
     * realtime controller lease.
     */
    val grantsController: Boolean
        get() = false

    override fun toString(): String =
        "DevicePairingUiState(phase=$phase, pairingCodeInput=[REDACTED], " +
            "workspaceIdInput=$workspaceIdInput, agentIdInput=$agentIdInput, " +
            "deviceDisplayNameInput=$deviceDisplayNameInput, " +
            "requestControlScope=$requestControlScope, ownerView=$ownerView, " +
            "fingerprintVerified=$fingerprintVerified, failure=$failure, " +
            "revokedAt=$revokedAt, pendingOperation=$pendingOperation)"
}

sealed interface PairingFailure {
    data object AuthenticationRequired : PairingFailure

    data class Contract(
        val code: PairingErrorCode,
        val retryAfterSeconds: Int? = null,
    ) : PairingFailure

    data object InvalidResponse : PairingFailure

    data object Unavailable : PairingFailure

    data object DeliveryUnknown : PairingFailure
}

sealed interface DevicePairingEvent {
    data class PairingCodeChanged(val value: String) : DevicePairingEvent

    data class WorkspaceIdChanged(val value: String) : DevicePairingEvent

    data class AgentIdChanged(val value: String) : DevicePairingEvent

    data class DeviceDisplayNameChanged(val value: String) : DevicePairingEvent

    data class RequestControlScopeChanged(val enabled: Boolean) : DevicePairingEvent

    data object ClaimStarted : DevicePairingEvent

    data class FingerprintVerificationChanged(val checked: Boolean) : DevicePairingEvent

    data object ConfirmStarted : DevicePairingEvent

    data object CancelStarted : DevicePairingEvent

    data object RevokeStarted : DevicePairingEvent

    data object RetryStarted : DevicePairingEvent

    data class OwnerViewReceived(val view: PairingOwnerView) : DevicePairingEvent

    data class DeviceRevoked(val deviceId: String, val revokedAt: String) : DevicePairingEvent

    data class Failed(val failure: PairingFailure) : DevicePairingEvent

    data object Reset : DevicePairingEvent
}

object DevicePairingReducer {
    fun reduce(
        state: DevicePairingUiState,
        event: DevicePairingEvent,
    ): DevicePairingUiState = when (event) {
        is DevicePairingEvent.PairingCodeChanged -> state.copy(
            pairingCodeInput = formatPairingCodeInput(event.value),
            failure = null,
        )

        is DevicePairingEvent.WorkspaceIdChanged -> state.copy(
            workspaceIdInput = event.value.trim(),
            failure = null,
        )

        is DevicePairingEvent.AgentIdChanged -> state.copy(
            agentIdInput = event.value.trim(),
            failure = null,
        )

        is DevicePairingEvent.DeviceDisplayNameChanged -> state.copy(
            deviceDisplayNameInput = event.value,
            failure = null,
        )

        is DevicePairingEvent.RequestControlScopeChanged -> state.copy(
            requestControlScope = event.enabled,
            failure = null,
        )

        DevicePairingEvent.ClaimStarted -> state.copy(
            phase = DevicePairingPhase.CLAIMING,
            pairingCodeInput = "",
            ownerView = null,
            fingerprintVerified = false,
            failure = null,
            revokedAt = null,
            pendingOperation = PairingOperationKind.CLAIM,
        )

        is DevicePairingEvent.FingerprintVerificationChanged -> if (
            state.phase == DevicePairingPhase.REVIEW
        ) {
            state.copy(fingerprintVerified = event.checked)
        } else {
            state
        }

        DevicePairingEvent.ConfirmStarted -> state.copy(
            phase = DevicePairingPhase.CONFIRMING,
            failure = null,
            pendingOperation = PairingOperationKind.CONFIRM,
        )

        DevicePairingEvent.CancelStarted -> state.copy(
            phase = DevicePairingPhase.CANCELLING,
            failure = null,
            pendingOperation = PairingOperationKind.CANCEL,
        )

        DevicePairingEvent.RevokeStarted -> state.copy(
            phase = DevicePairingPhase.REVOKING,
            failure = null,
            pendingOperation = PairingOperationKind.REVOKE,
        )

        DevicePairingEvent.RetryStarted -> if (state.canRetryPending) {
            state.copy(
                phase = checkNotNull(state.pendingOperation).busyPhase,
                failure = null,
            )
        } else {
            state
        }

        is DevicePairingEvent.OwnerViewReceived -> state.copy(
            phase = event.view.toPhase(),
            ownerView = event.view,
            fingerprintVerified = false,
            failure = null,
            pendingOperation = null,
        )

        is DevicePairingEvent.DeviceRevoked -> state.copy(
            phase = DevicePairingPhase.REVOKED,
            ownerView = null,
            fingerprintVerified = false,
            failure = null,
            revokedAt = event.revokedAt,
            pendingOperation = null,
        )

        is DevicePairingEvent.Failed -> when {
            event.failure == PairingFailure.DeliveryUnknown -> state.copy(
                phase = DevicePairingPhase.DELIVERY_UNKNOWN,
                pairingCodeInput = "",
                fingerprintVerified = false,
                failure = event.failure,
                pendingOperation = state.phase.pendingOperationKind,
            )
            state.pendingOperation != null &&
                event.failure in RETRYABLE_PENDING_BLOCKS -> state.copy(
                phase = event.failure.toPhase(),
                pairingCodeInput = "",
                fingerprintVerified = false,
                failure = event.failure,
            )
            else -> state.copy(
                phase = event.failure.toPhase(),
                pairingCodeInput = "",
                fingerprintVerified = false,
                failure = event.failure,
                pendingOperation = null,
            )
        }

        DevicePairingEvent.Reset -> DevicePairingUiState(
            workspaceIdInput = state.workspaceIdInput,
            agentIdInput = state.agentIdInput,
            deviceDisplayNameInput = state.deviceDisplayNameInput,
            requestControlScope = state.requestControlScope,
        )
    }

    private fun PairingOwnerView.toPhase(): DevicePairingPhase = when {
        state == PairingSessionState.EXPIRED -> DevicePairingPhase.EXPIRED
        state == PairingSessionState.CANCELLED -> DevicePairingPhase.CANCELLED
        state == PairingSessionState.CONFIRMED &&
            activationState == PairingActivationState.BLOCKED ->
            DevicePairingPhase.BLOCKED
        activationState == PairingActivationState.ACTIVE -> DevicePairingPhase.ACTIVE
        state == PairingSessionState.CONFIRMED &&
            activationState == PairingActivationState.AWAITING_PROOF ->
            DevicePairingPhase.AWAITING_CONNECTOR_PROOF
        state == PairingSessionState.CLAIMED &&
            activationState == PairingActivationState.WAITING_OWNER_CONFIRMATION ->
            DevicePairingPhase.REVIEW
        else -> DevicePairingPhase.ERROR
    }

    private fun PairingFailure.toPhase(): DevicePairingPhase = when (this) {
        PairingFailure.AuthenticationRequired -> DevicePairingPhase.AUTHENTICATION_REQUIRED
        is PairingFailure.Contract -> when (code) {
            PairingErrorCode.PAIRING_EXPIRED -> DevicePairingPhase.EXPIRED
            PairingErrorCode.PAIRING_CLAIM_RATE_LIMITED ->
                DevicePairingPhase.CLAIM_RATE_LIMITED
            PairingErrorCode.UNAUTHORIZED -> DevicePairingPhase.AUTHENTICATION_REQUIRED
            else -> DevicePairingPhase.ERROR
        }
        PairingFailure.DeliveryUnknown -> DevicePairingPhase.DELIVERY_UNKNOWN
        PairingFailure.InvalidResponse,
        PairingFailure.Unavailable,
        -> DevicePairingPhase.ERROR
    }

    private val DevicePairingPhase.pendingOperationKind: PairingOperationKind?
        get() = when (this) {
            DevicePairingPhase.CLAIMING -> PairingOperationKind.CLAIM
            DevicePairingPhase.CONFIRMING -> PairingOperationKind.CONFIRM
            DevicePairingPhase.CANCELLING -> PairingOperationKind.CANCEL
            DevicePairingPhase.REVOKING -> PairingOperationKind.REVOKE
            else -> null
        }

    private val PairingOperationKind.busyPhase: DevicePairingPhase
        get() = when (this) {
            PairingOperationKind.CLAIM -> DevicePairingPhase.CLAIMING
            PairingOperationKind.CONFIRM -> DevicePairingPhase.CONFIRMING
            PairingOperationKind.CANCEL -> DevicePairingPhase.CANCELLING
            PairingOperationKind.REVOKE -> DevicePairingPhase.REVOKING
        }

    private fun formatPairingCodeInput(value: String): String {
        val compact = value
            .uppercase()
            .filter(Char::isLetterOrDigit)
            .take(PAIRING_CODE_LENGTH)
        return if (compact.length <= PAIRING_CODE_GROUP_LENGTH) {
            compact
        } else {
            "${compact.take(PAIRING_CODE_GROUP_LENGTH)}-${compact.drop(PAIRING_CODE_GROUP_LENGTH)}"
        }
    }

    private const val PAIRING_CODE_LENGTH = 8
    private const val PAIRING_CODE_GROUP_LENGTH = 4
    private val RETRYABLE_PENDING_BLOCKS = setOf(
        PairingFailure.AuthenticationRequired,
        PairingFailure.Unavailable,
    )
}
