package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.sessions.control.ControlLossReason
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class SessionChromePresentationTest {
    @Test
    fun `controller owner labels distinguish Desktop and normalized local owners`() {
        assertEquals(
            "Hermes Desktop",
            controllerOwnerDisplayLabel(
                ControlMode.Conflict(
                    controllerKind = SessionControllerKind.DESKTOP,
                    controllerLabel = null,
                    leaseExpiresAtEpochMs = 0,
                    pendingInput = null,
                ),
            ),
        )
        assertEquals(
            "Hermes Local",
            controllerOwnerDisplayLabel(
                ControlMode.Conflict(
                    controllerKind = SessionControllerKind.DESKTOP,
                    controllerLabel = "Hermes Local",
                    leaseExpiresAtEpochMs = 0,
                    pendingInput = null,
                ),
            ),
        )
        assertEquals(
            "Hermes Mobile",
            controllerOwnerDisplayLabel(
                ControlMode.Conflict(
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = null,
                    leaseExpiresAtEpochMs = 0,
                    pendingInput = null,
                ),
            ),
        )
    }

    @Test
    fun `live controller stays compact without a redundant status strip`() {
        val presentation = sessionChromePresentation(
            controlMode = controllerMode(),
            connectionStatus = RealtimeConnectionStatus.LIVE,
            pendingOutcome = null,
            hasStatusMessage = false,
        )

        assertEquals(SessionChromeBadge.Controller, presentation.badge)
        assertFalse(presentation.showsStatusStrip)
    }

    @Test
    fun `same-request recovery takes visible precedence over controller`() {
        val presentation = sessionChromePresentation(
            controlMode = controllerMode(),
            connectionStatus = RealtimeConnectionStatus.LIVE,
            pendingOutcome = PendingInputInteractionOutcome.RetryAvailable,
            hasStatusMessage = false,
        )

        assertEquals(SessionChromeBadge.Restored, presentation.badge)
        assertTrue(presentation.showsStatusStrip)
    }

    @Test
    fun `lost control and reconnecting transport remain visible`() {
        val lost = sessionChromePresentation(
            controlMode = ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
            connectionStatus = RealtimeConnectionStatus.DISCONNECTED,
            pendingOutcome = PendingInputInteractionOutcome.DeliveryUnknown,
            hasStatusMessage = true,
        )
        val reconnecting = sessionChromePresentation(
            controlMode = ControlMode.Observer,
            connectionStatus = RealtimeConnectionStatus.RECONNECTING,
            pendingOutcome = null,
            hasStatusMessage = false,
        )

        assertEquals(SessionChromeBadge.Disconnected, lost.badge)
        assertTrue(lost.showsStatusStrip)
        assertEquals(SessionChromeBadge.Observer, reconnecting.badge)
        assertTrue(reconnecting.showsStatusStrip)
    }

    @Test
    fun `live observer transport keeps unavailable control distinct from disconnected`() {
        val controlUnavailable = sessionChromePresentation(
            controlMode = ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
            connectionStatus = RealtimeConnectionStatus.LIVE,
            pendingOutcome = null,
            hasStatusMessage = true,
            hasControlCapability = false,
        )
        val observerDisconnected = sessionChromePresentation(
            controlMode = ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
            connectionStatus = RealtimeConnectionStatus.DISCONNECTED,
            pendingOutcome = null,
            hasStatusMessage = true,
        )

        assertEquals(SessionChromeBadge.ControlUnavailable, controlUnavailable.badge)
        assertTrue(controlUnavailable.showsStatusStrip)
        assertFalse(controlUnavailable.showsControlLossReason)
        assertFalse(controlUnavailable.canRetryControl)
        assertEquals(SessionChromeBadge.Disconnected, observerDisconnected.badge)
        assertTrue(observerDisconnected.showsStatusStrip)
        assertTrue(observerDisconnected.canRetryControl)
    }

    @Test
    fun `expired controller lease remains explicitly retryable`() {
        val presentation = sessionChromePresentation(
            controlMode = ControlMode.Lost(ControlLossReason.LEASE_EXPIRED),
            connectionStatus = RealtimeConnectionStatus.LIVE,
            pendingOutcome = null,
            hasStatusMessage = false,
            hasControlCapability = false,
        )

        assertEquals(SessionChromeBadge.ControlUnavailable, presentation.badge)
        assertTrue(presentation.showsControlLossReason)
        assertTrue(presentation.canRetryControl)
    }

    @Test
    fun `server upgrade reason remains visible for idle observer`() {
        val presentation = sessionChromePresentation(
            controlMode = ControlMode.Observer,
            connectionStatus = RealtimeConnectionStatus.IDLE,
            pendingOutcome = null,
            hasStatusMessage = false,
            requiresServerUpgrade = true,
        )

        assertEquals(SessionChromeBadge.Observer, presentation.badge)
        assertTrue(presentation.showsStatusStrip)
    }

    @Test
    fun `live observer keeps data plane status visible below observer badge`() {
        val presentation = sessionChromePresentation(
            controlMode = ControlMode.Observer,
            connectionStatus = RealtimeConnectionStatus.LIVE,
            pendingOutcome = null,
            hasStatusMessage = false,
        )

        assertEquals(SessionChromeBadge.Observer, presentation.badge)
        assertTrue(presentation.showsStatusStrip)
    }

    @Test
    fun `current execution stays hidden outside an authoritative running turn`() {
        assertEquals(
            null,
            currentExecutionPresentation(
                running = false,
                turns = listOf(streamingTurn()),
            ),
        )
    }

    @Test
    fun `current execution names the latest running tool instead of generic activity`() {
        val turn = streamingTurn(
            tools = listOf(
                ConversationToolUiModel(
                    key = "tool-1",
                    toolId = "tool-1",
                    name = "terminal",
                    callLabel = "session.command.status",
                    status = ConversationToolStatus.RUNNING,
                ),
            ),
        )

        assertEquals(
            CurrentExecutionPresentation(
                kind = CurrentExecutionKind.TOOL,
                detail = "session.command.status",
            ),
            currentExecutionPresentation(running = true, turns = listOf(turn)),
        )
    }

    @Test
    fun `streaming response takes focus after completed process work`() {
        val turn = streamingTurn(
            thinking = "Finished checking the request.",
            response = "The original request identity is preserved.",
        )

        assertEquals(
            CurrentExecutionPresentation(
                kind = CurrentExecutionKind.RESPONSE,
                detail = null,
            ),
            currentExecutionPresentation(running = true, turns = listOf(turn)),
        )
    }

    @Test
    fun `running turn without projected activity keeps an explicit working fallback`() {
        assertEquals(
            CurrentExecutionPresentation(CurrentExecutionKind.WORKING, detail = null),
            currentExecutionPresentation(running = true, turns = emptyList()),
        )
    }

    private fun controllerMode() = ControlMode.Controller(
        SessionControlLease(
            leaseId = SessionControlLeaseId("lease-1"),
            expiresAtEpochMs = 9_000_000_000_000L,
            controlRevision = 1,
            controllerKind = SessionControllerKind.MOBILE,
            controllerLabel = "Hermes Mobile",
            pendingInput = null,
        ),
    )

    private fun streamingTurn(
        thinking: String = "",
        tools: List<ConversationToolUiModel> = emptyList(),
        response: String = "",
    ) = ConversationTurnUiModel(
        key = "turn-1",
        userPrompt = null,
        thinking = thinking,
        statusText = "",
        tools = tools,
        response = response,
        status = ConversationTurnStatus.STREAMING,
    )
}
