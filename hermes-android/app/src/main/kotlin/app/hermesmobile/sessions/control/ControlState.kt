package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlStatus
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionPendingInput

data class ControlRevisionGap(
    val expectedRevision: Long,
    val observedRevision: Long,
)

data class ControlState(
    val mode: ControlMode = ControlMode.Disconnected,
    val controlRevision: Long? = null,
    val revisionGap: ControlRevisionGap? = null,
) {
    val canMutate: Boolean
        get() = mode is ControlMode.Controller && revisionGap == null
}

sealed interface ControlMode {
    data object Disconnected : ControlMode
    data object Observer : ControlMode
    data object Acquiring : ControlMode
    data class Controller(val lease: SessionControlLease) : ControlMode
    data class Conflict(
        val controllerKind: SessionControllerKind,
        val controllerLabel: String?,
        val leaseExpiresAtEpochMs: Long,
        val pendingInput: SessionPendingInput?,
    ) : ControlMode
    data class Lost(val reason: ControlLossReason) : ControlMode
}

enum class ControlLossReason {
    LEASE_EXPIRED,
    CONNECTION_LOST,
    RELEASED,
    REJECTED,
}

sealed interface ControlAction {
    data object ObserverConnected : ControlAction
    data object AcquireStarted : ControlAction
    data class LeaseGranted(val lease: SessionControlLease) : ControlAction
    data class ControlChanged(val status: SessionControlStatus) : ControlAction
    data class StatusReconciled(val status: SessionControlStatus) : ControlAction
    data class LeaseLost(val reason: ControlLossReason) : ControlAction
}

class ControlStateReducer {
    fun reduce(current: ControlState, action: ControlAction): ControlState = when (action) {
        ControlAction.ObserverConnected -> current.copy(mode = ControlMode.Observer)
        ControlAction.AcquireStarted -> current.copy(mode = ControlMode.Acquiring)
        is ControlAction.LeaseGranted -> if (current.mode is ControlMode.Lost) {
            current
        } else {
            current.copy(
                mode = ControlMode.Controller(action.lease),
                controlRevision = action.lease.controlRevision,
                revisionGap = null,
            )
        }
        is ControlAction.ControlChanged -> reduceControlEvent(current, action.status)
        is ControlAction.StatusReconciled -> reduceStatus(current, action.status)
        is ControlAction.LeaseLost -> current.copy(
            mode = ControlMode.Lost(action.reason),
            revisionGap = null,
        )
    }

    private fun reduceStatus(
        current: ControlState,
        status: SessionControlStatus,
    ): ControlState {
        val minimumRevision = current.revisionGap?.observedRevision
            ?: current.controlRevision?.plus(1)
            ?: 0
        if (status.controlRevision < minimumRevision) return current
        return current.copy(
            mode = status.toMode(),
            controlRevision = status.controlRevision,
            revisionGap = null,
        )
    }

    private fun reduceControlEvent(
        current: ControlState,
        status: SessionControlStatus,
    ): ControlState {
        val currentRevision = current.controlRevision
        val observedWatermark = current.revisionGap?.observedRevision ?: currentRevision ?: 0
        if (status.controlRevision <= observedWatermark) return current

        val expectedRevision = (currentRevision ?: 0) + 1
        if (current.revisionGap != null || status.controlRevision != expectedRevision) {
            return current.copy(
                revisionGap = ControlRevisionGap(
                    expectedRevision = current.revisionGap?.expectedRevision ?: expectedRevision,
                    observedRevision = status.controlRevision,
                ),
            )
        }
        return current.copy(
            mode = status.toMode(),
            controlRevision = status.controlRevision,
        )
    }

    private fun SessionControlStatus.toMode(): ControlMode =
        if (controllerKind == SessionControllerKind.NONE) {
            ControlMode.Observer
        } else {
            ControlMode.Conflict(
                controllerKind = controllerKind,
                controllerLabel = controllerLabel,
                leaseExpiresAtEpochMs = leaseExpiresAtEpochMs,
                pendingInput = pendingInput,
            )
        }
}
