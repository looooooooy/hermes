package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControlStatus
import app.hermesmobile.protocol.gateway.SessionControllerKind
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertSame
import kotlin.test.assertTrue

class SessionControlReducersTest {
    private val controlReducer = ControlStateReducer()

    @Test
    fun `control begins disconnected and observation enables read-only state`() {
        val initial = ControlState()

        val observing = controlReducer.reduce(initial, ControlAction.ObserverConnected)

        assertIs<ControlMode.Disconnected>(initial.mode)
        assertIs<ControlMode.Observer>(observing.mode)
        assertFalse(observing.canMutate)
    }

    @Test
    fun `a granted lease enables mutations only after acquisition starts`() {
        val observing = controlReducer.reduce(ControlState(), ControlAction.ObserverConnected)
        val acquiring = controlReducer.reduce(observing, ControlAction.AcquireStarted)

        val controlling = controlReducer.reduce(acquiring, ControlAction.LeaseGranted(lease(1)))

        assertIs<ControlMode.Acquiring>(acquiring.mode)
        assertFalse(acquiring.canMutate)
        assertIs<ControlMode.Controller>(controlling.mode)
        assertTrue(controlling.canMutate)
    }

    @Test
    fun `a sequential control update reports another controller as a conflict`() {
        val controlling = controlReducer.reduce(
            ControlState(mode = ControlMode.Acquiring),
            ControlAction.LeaseGranted(lease(1)),
        )

        val conflicted = controlReducer.reduce(
            controlling,
            ControlAction.ControlChanged(status(revision = 2, kind = SessionControllerKind.DESKTOP)),
        )

        val conflict = assertIs<ControlMode.Conflict>(conflicted.mode)
        assertEquals(SessionControllerKind.DESKTOP, conflict.controllerKind)
        assertEquals(2, conflicted.controlRevision)
        assertFalse(conflicted.canMutate)
    }

    @Test
    fun `lower and equal control revisions are ignored as stale`() {
        val current = controlReducer.reduce(
            ControlState(mode = ControlMode.Acquiring),
            ControlAction.LeaseGranted(lease(5)),
        )

        val equal = controlReducer.reduce(
            current,
            ControlAction.ControlChanged(status(5, SessionControllerKind.DESKTOP)),
        )
        val lower = controlReducer.reduce(
            current,
            ControlAction.ControlChanged(status(4, SessionControllerKind.NONE)),
        )

        assertSame(current, equal)
        assertSame(current, lower)
    }

    @Test
    fun `a control revision gap is flagged and immediately disables mutations`() {
        val current = controlReducer.reduce(
            ControlState(mode = ControlMode.Acquiring),
            ControlAction.LeaseGranted(lease(1)),
        )

        val gapped = controlReducer.reduce(
            current,
            ControlAction.ControlChanged(status(3, SessionControllerKind.DESKTOP)),
        )

        assertIs<ControlMode.Controller>(gapped.mode)
        assertEquals(1, gapped.controlRevision)
        assertEquals(ControlRevisionGap(expectedRevision = 2, observedRevision = 3), gapped.revisionGap)
        assertFalse(gapped.canMutate)
    }

    @Test
    fun `authoritative status at the observed revision reconciles a control gap`() {
        val controlling = controlReducer.reduce(
            ControlState(mode = ControlMode.Acquiring),
            ControlAction.LeaseGranted(lease(1)),
        )
        val gapped = controlReducer.reduce(
            controlling,
            ControlAction.ControlChanged(status(3, SessionControllerKind.DESKTOP)),
        )

        val reconciled = controlReducer.reduce(
            gapped,
            ControlAction.StatusReconciled(status(3, SessionControllerKind.NONE)),
        )

        assertIs<ControlMode.Observer>(reconciled.mode)
        assertEquals(3, reconciled.controlRevision)
        assertEquals(null, reconciled.revisionGap)
        assertFalse(reconciled.canMutate)
    }

    @Test
    fun `lease loss immediately disables mutations and a late grant cannot restore them`() {
        val controlling = controlReducer.reduce(
            ControlState(mode = ControlMode.Acquiring),
            ControlAction.LeaseGranted(lease(1)),
        )

        val lost = controlReducer.reduce(
            controlling,
            ControlAction.LeaseLost(ControlLossReason.LEASE_EXPIRED),
        )
        val afterLateGrant = controlReducer.reduce(lost, ControlAction.LeaseGranted(lease(2)))

        val lostMode = assertIs<ControlMode.Lost>(lost.mode)
        assertEquals(ControlLossReason.LEASE_EXPIRED, lostMode.reason)
        assertEquals(1, lost.controlRevision)
        assertFalse(lost.canMutate)
        assertSame(lost, afterLateGrant)
    }

    private fun status(revision: Long, kind: SessionControllerKind) = SessionControlStatus(
        controllerKind = kind,
        controllerLabel = if (kind == SessionControllerKind.NONE) null else "Other controller",
        controlRevision = revision,
        leaseExpiresAtEpochMs = 1_900_000_000_000,
        pendingInput = null,
    )

    private fun lease(revision: Long) = SessionControlLease(
        leaseId = SessionControlLeaseId("lease-$revision"),
        expiresAtEpochMs = 1_900_000_000_000,
        controlRevision = revision,
        controllerKind = SessionControllerKind.MOBILE,
        controllerLabel = "Hermes Mobile",
        pendingInput = null,
    )
}
