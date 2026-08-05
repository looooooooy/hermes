package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.ServerTurnId
import app.hermesmobile.protocol.gateway.SessionCommandState
import app.hermesmobile.protocol.gateway.SessionCommandStatus
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull

class CommandStateReducerTest {
    private val reducer = CommandStateReducer()
    private val requestId = ClientRequestId("request-1")
    private val turnId = ClientTurnId("turn-1")

    @Test
    fun `definitive failure is terminal and preserves its summary`() {
        val sending = reducer.reduce(
            CommandState(),
            CommandAction.Started(requestId = requestId, clientTurnId = turnId),
        )

        val failed = reducer.reduce(
            sending,
            CommandAction.Failed(requestId, "Lease rejected"),
        )

        val command = assertNotNull(failed.commands[requestId])
        assertEquals(CommandPhase.FAILED, command.phase)
        assertEquals("Lease rejected", command.failureSummary)
        assertFalse(command.canAutomaticallyRetry)
    }

    @Test
    fun `timeout remains unknown under the original request id until status reconciliation`() {
        val sending = reducer.reduce(
            CommandState(),
            CommandAction.Started(requestId = requestId, clientTurnId = turnId),
        )
        val timedOut = reducer.reduce(sending, CommandAction.DeliveryUnknown(requestId))
        val reconciled = reducer.reduce(
            timedOut,
            CommandAction.StatusReconciled(
                SessionCommandStatus(
                    status = SessionCommandState.UNKNOWN,
                    clientRequestId = requestId,
                    clientTurnId = turnId,
                ),
            ),
        )

        val command = assertNotNull(reconciled.commands[requestId])
        assertEquals(CommandPhase.UNKNOWN, command.phase)
        assertEquals(turnId, command.clientTurnId)
        assertFalse(command.canAutomaticallyRetry)
    }

    @Test
    fun `acknowledgement updates only its correlated command with authoritative turn ids`() {
        val otherRequestId = ClientRequestId("request-2")
        val otherTurnId = ClientTurnId("turn-2")
        val started = reducer.reduce(
            reducer.reduce(
                CommandState(),
                CommandAction.Started(requestId = requestId, clientTurnId = turnId),
            ),
            CommandAction.Started(requestId = otherRequestId, clientTurnId = otherTurnId),
        )

        val acknowledged = reducer.reduce(
            started,
            CommandAction.Acknowledged(
                SessionCommandStatus(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = requestId,
                    clientTurnId = turnId,
                    serverTurnId = ServerTurnId("server-turn-1"),
                ),
            ),
        )

        val command = assertNotNull(acknowledged.commands[requestId])
        assertEquals(CommandPhase.ACCEPTED, command.phase)
        assertEquals(ServerTurnId("server-turn-1"), command.serverTurnId)
        assertEquals(CommandPhase.SENDING, acknowledged.commands[otherRequestId]?.phase)
    }
}
