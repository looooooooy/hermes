package app.hermesmobile.sessions

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeDown
import androidx.compose.ui.test.swipeUp
import androidx.compose.ui.unit.dp
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import app.hermesmobile.streamingPerformanceReviewState
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.MobileControlMethods
import app.hermesmobile.sessions.control.CommandPhase
import app.hermesmobile.sessions.control.CommandRecord
import app.hermesmobile.sessions.control.CommandState
import app.hermesmobile.sessions.control.ComposerState
import app.hermesmobile.sessions.control.ComposerSubmission
import app.hermesmobile.sessions.control.ControlLossReason
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.ControlState
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import app.hermesmobile.sessions.control.PendingInputInteractionState
import app.hermesmobile.ui.theme.HermesMobileTheme
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Rule
import org.junit.Test

class SessionBrowserScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun sessionListShowsAuthoritativeRowsAndOpensByStableKey() {
        var opened: SessionKey? = null
        var backRequests = 0
        var refreshRequests = 0
        var pairingRequests = 0
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.LIST,
                    sessions = listOf(session),
                ),
                onOpenSession = { opened = it },
                onBack = { backRequests += 1 },
                onRefresh = { refreshRequests += 1 },
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
                onOpenPairing = { pairingRequests += 1 },
            )
        }

        composeRule.onNodeWithText("Sessions").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Back").performClick()
        composeRule.onNodeWithText("Pair device").performClick()
        composeRule.onNodeWithText("Refresh").performClick()
        composeRule.onNodeWithText("First session").assertIsDisplayed()
        composeRule.onNodeWithText("Preview text").assertIsDisplayed()
        composeRule.onNodeWithText("First session").performClick()
        composeRule.runOnIdle {
            check(backRequests == 1)
            check(refreshRequests == 1)
            check(pairingRequests == 1)
            check(opened == SessionKey("stored-1"))
        }
    }

    @Test
    fun sessionListLoadsMoreWhenTheServerHasAnotherPage() {
        var loadMoreRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.LIST,
                    sessions = listOf(session()),
                    hasMoreSessions = true,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = { loadMoreRequests += 1 },
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Load more").performClick()

        composeRule.runOnIdle { check(loadMoreRequests == 1) }
    }

    @Test
    fun transcriptIsReadableButComposerIsGatedWithoutMultiClientServerContract() {
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = transcript(),
                    control = ControlState(mode = ControlMode.Observer),
                    controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
                    realtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("First session").assertIsDisplayed()
        composeRule.onNodeWithText("hello from Android").assertIsDisplayed()
        composeRule.onNodeWithText("Server upgrade required for live observer/controller mode").assertIsDisplayed()
        composeRule.onNodeWithTag("message-input").assertIsNotEnabled()
        composeRule.onNodeWithTag("send-button").assertIsNotEnabled()
    }

    @Test
    fun liveObserverShowsRetryableControlLossWithoutClaimingDataPlaneDisconnected() {
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = transcript(),
                    control = ControlState(
                        mode = ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                    ),
                    controlAvailableMethods = setOf(MobileControlMethods.PROMPT_SUBMIT),
                    controlStatus = RealtimeControlStatus.OBSERVER,
                    realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Control unavailable").assertIsDisplayed()
        composeRule.onNodeWithText("Control connection lost").assertIsDisplayed()
        composeRule.onNodeWithText("Live").assertIsDisplayed()
        composeRule.onNodeWithTag("retry-control").assertIsDisplayed()
        composeRule.onNodeWithText("Disconnected").assertDoesNotExist()
    }

    @Test
    fun liveObserverWithUnavailableControlCapabilityStaysLiveWithoutRetryableLossChrome() {
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = transcript(),
                    control = ControlState(
                        mode = ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                    ),
                    controlAvailableMethods = emptySet(),
                    controlStatus = RealtimeControlStatus.OBSERVER,
                    realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Control unavailable").assertIsDisplayed()
        composeRule.onNodeWithText("Live").assertIsDisplayed()
        composeRule.onNodeWithText("Control connection lost").assertDoesNotExist()
        composeRule.onNodeWithTag("retry-control").assertDoesNotExist()
        composeRule.onNodeWithText("Disconnected").assertDoesNotExist()
        composeRule.onNodeWithTag("message-input").assertIsNotEnabled()
        composeRule.onNodeWithTag("send-button").assertIsNotEnabled()
    }

    @Test
    fun controllerComposerUsesAuthoritativeDraftAndDispatchesDraftAndSendCallbacks() {
        val session = session()
        var changedDraft: String? = null
        var sendRequests = 0
        var state by mutableStateOf(
            controlledTranscriptState(session, running = false).copy(
                composer = ComposerState(draft = "Ready to send"),
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onDraftChanged = {
                    changedDraft = it
                    state = state.copy(composer = state.composer.copy(draft = it))
                },
                onSend = { sendRequests += 1 },
                onStop = {},
            )
        }

        composeRule.onNodeWithTag("message-input")
            .assertTextEquals("Ready to send")
            .assertIsEnabled()
            .performTextReplacement("Updated draft")
        composeRule.runOnIdle { check(changedDraft == "Updated draft") }

        composeRule.onNodeWithTag("send-button").assertIsEnabled().performClick()
        composeRule.runOnIdle { check(sendRequests == 1) }
    }

    @Test
    fun controllerComposerDispatchesKeyboardSendThroughTheSameGate() {
        val session = session()
        var sendRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(session, running = false).copy(
                    composer = ComposerState(draft = "Send from keyboard"),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onDraftChanged = {},
                onSend = { sendRequests += 1 },
            )
        }

        composeRule.onNodeWithTag("message-input").performImeAction()
        composeRule.runOnIdle { check(sendRequests == 1) }
    }

    @Test
    fun omittedMutationCapabilitiesKeepRunningControllerActionsFailClosed() {
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(session, running = true).copy(
                    composer = ComposerState(draft = "Must not queue"),
                    controlAvailableMethods = setOf(MobileControlMethods.ACQUIRE),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
                onStop = {},
            )
        }

        composeRule.onNodeWithTag("queue-button").assertIsNotEnabled()
        composeRule.onNodeWithTag("stop-button").assertIsNotEnabled()
        composeRule.onNodeWithTag("guidance-toggle").assertDoesNotExist()
    }

    @Test
    fun liveControllerUsesCompactTranscriptChromeWithoutRedundantStatusStrip() {
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(session, running = false),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("First session").assertIsDisplayed()
        composeRule.onNodeWithText("session · stored-1").assertIsDisplayed()
        composeRule.onNodeWithText("Controller").assertIsDisplayed()
        composeRule.onNodeWithTag("session-status-strip").assertDoesNotExist()
    }

    @Test
    fun runningControllerShowsTheFocusConsoleCurrentExecutionStrip() {
        val session = session()
        composeRule.setContent {
            HermesMobileTheme(darkTheme = true) {
                SessionBrowserScreen(
                    state = controlledTranscriptState(session, running = true),
                    onOpenSession = {},
                    onBack = {},
                    onRefresh = {},
                    onLoadMore = {},
                    onReconnect = {},
                    onSend = {},
                )
            }
        }

        composeRule.onNodeWithTag("current-execution-strip").assertIsDisplayed()
        composeRule.onNodeWithText("CURRENTLY EXECUTING").assertIsDisplayed()
        composeRule.onNodeWithText("Working…").assertIsDisplayed()
        composeRule.onNodeWithText("Running").assertIsDisplayed()
    }

    @Test
    fun pendingDockReplacesEveryOrdinaryComposerInputWithoutOverlay() {
        val pending = SessionPendingInput.Approval(
            requestId = "approval-layout",
            title = "Run focused tests?",
            description = "Review the exact command before responding.",
            command = "./gradlew :app:testDebugUnitTest",
            choices = listOf(
                SessionApprovalChoice.ALLOW_ONCE,
                SessionApprovalChoice.ALLOW_ALWAYS,
                SessionApprovalChoice.DENY,
            ),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session(),
                    running = true,
                    pendingInput = pending,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        val transcriptBounds = composeRule.onNodeWithTag("transcript-list")
            .fetchSemanticsNode().boundsInRoot
        val dockBounds = composeRule.onNodeWithTag("pending-input-dock")
            .fetchSemanticsNode().boundsInRoot

        check(transcriptBounds.bottom <= dockBounds.top)
        composeRule.onNodeWithTag("message-input").assertDoesNotExist()
        composeRule.onNodeWithTag("voice-input-button").assertDoesNotExist()
        composeRule.onNodeWithTag("guidance-toggle").assertDoesNotExist()
        composeRule.onNodeWithTag("queue-button").assertDoesNotExist()
        composeRule.onNodeWithTag("stop-button").assertDoesNotExist()
    }

    @Test
    fun reconnectingTranscriptKeepsStatusVisibleBelowTheCompactChrome() {
        val session = session()
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(session, running = false).copy(
                    realtimeConnectionStatus = RealtimeConnectionStatus.RECONNECTING,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("session-status-strip").assertIsDisplayed()
        composeRule.onNodeWithText("Reconnecting…").assertIsDisplayed()
    }

    @Test
    fun runningControllerQueuesDraftAndKeepsStopIndependentlyAvailable() {
        val session = session()
        var state by mutableStateOf(
            controlledTranscriptState(session, running = true).copy(
                composer = ComposerState(draft = "Run this next"),
            ),
        )
        var queueRequests = 0
        var stopRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onDraftChanged = {},
                onSend = { queueRequests += 1 },
                onStop = { stopRequests += 1 },
            )
        }

        composeRule.onNodeWithTag("send-button").assertDoesNotExist()
        composeRule.onNodeWithTag("queue-button").assertIsEnabled().performClick()
        composeRule.onNodeWithTag("stop-button").assertIsEnabled().performClick()
        composeRule.runOnIdle {
            check(queueRequests == 1)
            check(stopRequests == 1)
            state = state.copy(
                interruptRequestId = app.hermesmobile.protocol.gateway.ClientRequestId("stop-1"),
            )
        }

        composeRule.onNodeWithTag("queue-button").assertIsNotEnabled()
        composeRule.onNodeWithTag("stop-button").assertIsNotEnabled()
        composeRule.onNodeWithContentDescription("Stopping…").assertIsDisplayed()
    }

    @Test
    fun runningControllerCanGuideTheCurrentTurnWithoutQueuingANewPrompt() {
        val session = session()
        var state by mutableStateOf(
            controlledTranscriptState(session, running = true).copy(
                composer = ComposerState(draft = "Queue this separately"),
                guidance = SessionGuidanceState(draft = "Verify authorization next"),
            ),
        )
        var queueRequests = 0
        var guidanceRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = { queueRequests += 1 },
                onGuidanceDraftChanged = {
                    state = state.copy(guidance = state.guidance.withDraft(it))
                },
                onSubmitGuidance = { guidanceRequests += 1 },
            )
        }

        composeRule.onNodeWithTag("guidance-toggle").assertIsEnabled().performClick()
        composeRule.onNodeWithTag("guidance-panel").assertDoesNotExist()
        composeRule.onNodeWithTag("guidance-input").assertDoesNotExist()
        composeRule.onNodeWithTag("message-input")
            .assertTextEquals("Verify authorization next")
            .assertIsFocused()
            .assertIsEnabled()
            .performImeAction()
        composeRule.onNodeWithText("GUIDE CURRENT RUN").assertIsDisplayed()
        composeRule.onNodeWithText("Next message: guide the active turn").assertIsDisplayed()
        composeRule.runOnIdle {
            check(guidanceRequests == 1)
            check(queueRequests == 0)
        }
        composeRule.onNodeWithTag("guidance-submit-button").assertIsEnabled().performClick()

        composeRule.runOnIdle {
            check(guidanceRequests == 2)
            check(queueRequests == 0)
        }
        composeRule.onNodeWithText("Queued (", substring = true).assertDoesNotExist()
    }

    @Test
    fun runningTranscriptShowsOnlyServerAcknowledgedQueuedPromptPreview() {
        val requestId = ClientRequestId("queued-1")
        val state = controlledTranscriptState(session(), running = true).copy(
            commands = CommandState(
                commands = linkedMapOf(
                    requestId to CommandRecord(
                        requestId = requestId,
                        clientTurnId = ClientTurnId("turn-queued-1"),
                        phase = CommandPhase.QUEUED,
                        promptPreview = "Run the accessibility checks next",
                    ),
                    ClientRequestId("sending-1") to CommandRecord(
                        requestId = ClientRequestId("sending-1"),
                        clientTurnId = ClientTurnId("turn-sending-1"),
                        phase = CommandPhase.SENDING,
                        promptPreview = "Not queued yet",
                    ),
                ),
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("queued-prompt-panel").assertIsDisplayed()
        composeRule.onNodeWithText("Queued (1)").assertIsDisplayed()
        composeRule.onNodeWithText("1. Run the accessibility checks next").assertIsDisplayed()
        composeRule.onNodeWithText("Not queued yet", substring = true).assertDoesNotExist()
    }

    @Test
    fun unknownDeliveryAndLostControlRemainExplicitAndFailClosed() {
        val session = session()
        val requestId = ClientRequestId("request-1")
        var retryControlRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(session, running = false).copy(
                    control = ControlState(
                        mode = ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                    ),
                    composer = ComposerState(
                        draft = "Do not resend",
                        submitted = ComposerSubmission(
                            requestId = requestId,
                            clientTurnId = ClientTurnId("turn-1"),
                            text = "Do not resend",
                        ),
                    ),
                    commands = CommandState(
                        commands = mapOf(
                            requestId to CommandRecord(
                                requestId = requestId,
                                clientTurnId = ClientTurnId("turn-1"),
                                phase = CommandPhase.UNKNOWN,
                            ),
                        ),
                    ),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onDraftChanged = {},
                onSend = {},
                onStop = {},
                onRetryControl = { retryControlRequests += 1 },
            )
        }

        composeRule.onNodeWithText("Control connection lost").assertIsDisplayed()
        composeRule.onNodeWithText("Delivery status unknown. Do not resend.").assertIsDisplayed()
        composeRule.onNodeWithTag("message-input").assertIsNotEnabled()
        composeRule.onNodeWithTag("send-button").assertIsNotEnabled()
        composeRule.onNodeWithTag("retry-control").performClick()
        composeRule.runOnIdle { check(retryControlRequests == 1) }
    }

    @Test
    fun transcriptOffersEarlierMessagesWhenTheInitialWindowIsPartial() {
        val session = session()
        var loadEarlierRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = transcript().copy(
                        pagination = TranscriptPagination(limit = 20, offset = 20, returned = 1),
                    ),
                    hasOlderMessages = true,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onLoadOlder = { loadEarlierRequests += 1 },
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Load earlier messages").assertIsDisplayed().performClick()
        composeRule.runOnIdle { check(loadEarlierRequests == 1) }
    }

    @Test
    fun transcriptFollowsLatestUntilUserBrowsesHistoryThenJumpRestoresFollow() {
        val session = session()
        var state by mutableStateOf(
            SessionBrowserUiState(
                phase = SessionBrowserPhase.TRANSCRIPT,
                sessions = listOf(session),
                selectedSession = session,
                transcript = longTranscript(40),
                controlStatus = RealtimeControlStatus.OBSERVER,
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("transcript-list")
            .performScrollToNode(hasText("message-39"))
        composeRule.onNodeWithText("message-39").assertIsDisplayed()
        composeRule.onNodeWithTag("transcript-list")
            .performTouchInput { swipeDown() }
        composeRule.onNodeWithTag("jump-to-latest").assertIsDisplayed()

        composeRule.runOnIdle {
            state = state.copy(transcript = longTranscript(41))
        }

        composeRule.onNodeWithTag("jump-to-latest").assertIsDisplayed().performClick()
        composeRule.onNodeWithText("message-40").assertIsDisplayed()
        composeRule.onNodeWithTag("jump-to-latest").assertDoesNotExist()
    }

    @Test
    fun transcriptMinimapHidesForShortContentAndNavigatesLongConversation() {
        val session = session()
        var state by mutableStateOf(
            SessionBrowserUiState(
                phase = SessionBrowserPhase.TRANSCRIPT,
                sessions = listOf(session),
                selectedSession = session,
                transcript = longTranscript(4),
                controlStatus = RealtimeControlStatus.OBSERVER,
            ),
        )
        composeRule.setContent {
            HermesMobileTheme {
                SessionBrowserScreen(
                    state = state,
                    onOpenSession = {},
                    onBack = {},
                    onRefresh = {},
                    onLoadMore = {},
                    onReconnect = {},
                    onSend = {},
                )
            }
        }

        composeRule.onNodeWithTag("transcript-minimap").assertDoesNotExist()

        composeRule.runOnIdle {
            state = state.copy(transcript = longTranscript(40))
        }

        composeRule.onNodeWithTag("transcript-minimap")
            .assertIsDisplayed()
            .assertHeightIsAtLeast(48.dp)
        composeRule.onNodeWithTag("transcript-minimap-marker:1")
            .assert(
                SemanticsMatcher("has a turn status and summary") { node ->
                    node.config.contains(SemanticsProperties.ContentDescription) &&
                        node.config[SemanticsProperties.ContentDescription]
                            .singleOrNull()
                            ?.startsWith("Turn 1,") == true
                },
            )
            .performClick()
        composeRule.onNodeWithText("message-0").assertIsDisplayed()

        composeRule.onNodeWithTag("transcript-minimap-marker:40").performClick()
        composeRule.onNodeWithText("message-39").assertIsDisplayed()
        composeRule.onNodeWithTag("transcript-minimap")
            .performTouchInput { swipeUp() }
        composeRule.onNodeWithText("message-0").assertIsDisplayed()
    }

    @Test
    fun loadingEarlierMessagesKeepsTheCurrentTurnVisible() {
        val session = session()
        val fullHistory = longTranscript(40)
        val recentWindow = fullHistory.copy(
            messages = fullHistory.messages.drop(20),
            pagination = TranscriptPagination(limit = 20, offset = 20, returned = 20),
        )
        var state by mutableStateOf(
            SessionBrowserUiState(
                phase = SessionBrowserPhase.TRANSCRIPT,
                sessions = listOf(session),
                selectedSession = session,
                transcript = recentWindow,
                hasOlderMessages = true,
                controlStatus = RealtimeControlStatus.OBSERVER,
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onLoadOlder = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("transcript-list")
            .performTouchInput { swipeDown() }
        composeRule.onNodeWithTag("jump-to-latest").assertIsDisplayed()
        composeRule.onNodeWithTag("transcript-list")
            .performScrollToNode(hasText("message-20"))
        composeRule.onNodeWithText("message-20").assertIsDisplayed()

        composeRule.runOnIdle {
            state = state.copy(
                transcript = fullHistory,
                hasOlderMessages = false,
            )
        }

        composeRule.onNodeWithText("message-20").assertIsDisplayed()
        composeRule.onNodeWithTag("jump-to-latest").assertIsDisplayed()
        composeRule.onNodeWithText("Back to latest").assertIsDisplayed()
        composeRule.onNodeWithText("Back to latest · 1 new").assertDoesNotExist()
    }

    @Test
    fun transcriptShowsRealtimeConnectionAndReconnectFeedback() {
        val session = session()
        var state by mutableStateOf(
            SessionBrowserUiState(
                phase = SessionBrowserPhase.TRANSCRIPT,
                sessions = listOf(session),
                selectedSession = session,
                transcript = transcript(),
                controlStatus = RealtimeControlStatus.OBSERVER,
                realtimeConnectionStatus = RealtimeConnectionStatus.RECONNECTING,
                realtimeMessage = "Connection lost. Reconnecting safely.",
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Reconnecting…").assertIsDisplayed()
        composeRule.onNodeWithText("Connection lost. Reconnecting safely.").assertIsDisplayed()

        composeRule.runOnIdle {
            state = state.copy(
                realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
                realtimeMessage = null,
            )
        }

        composeRule.onNodeWithText("Live").assertIsDisplayed()
    }

    @Test
    fun transcriptShowsStreamingAssistantReasoningAndToolOutput() {
        val session = session()
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = app.hermesmobile.protocol.sessions.RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        ).copy(
            running = true,
            streamingAssistantText = "First line\nSecond line",
            streamingReasoningText = "Checking the workspace",
            tools = listOf(
                LiveToolProjection(
                    key = "tool-1",
                    name = "terminal",
                    status = LiveToolStatus.RUNNING,
                    payload = buildJsonObject {
                        put(
                            "arguments",
                            buildJsonObject {
                                put("command", "pwd")
                                put("workdir", "/workspace")
                            },
                        )
                        put(
                            "output",
                            buildJsonObject {
                                put("content", "line 1\nline 2")
                                put("exit_code", 0)
                            },
                        )
                        put("trace_id", "trace-123")
                    },
                ),
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = baseline,
                    realtime = realtime,
                    controlStatus = RealtimeControlStatus.OBSERVER,
                    realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithContentDescription("Thinking").assertIsDisplayed()
        composeRule.onNodeWithText("Checking the workspace ▍").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithTag("tool:tool-1")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Running",
                ),
            )
        composeRule.onNodeWithText("Terminal(\"pwd\")").assertIsDisplayed()
        composeRule.onNodeWithText("Command: pwd\nWorkdir: /workspace").assertDoesNotExist()
        composeRule.onNodeWithText("line 1\nline 2\nExit code: 0\nTrace id: trace-123").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Expand Args").performClick()
        composeRule.onNodeWithContentDescription("Collapse Result").performClick()
        composeRule.onNodeWithText("Command: pwd\nWorkdir: /workspace").assertIsDisplayed()
        composeRule.onNodeWithText("line 1\nline 2\nExit code: 0\nTrace id: trace-123").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand Result").performClick()
        composeRule.onNodeWithText("line 1\nline 2\nExit code: 0\nTrace id: trace-123").assertIsDisplayed()
        composeRule.onNodeWithTag("transcript-list").assert(
            SemanticsMatcher.keyNotDefined(SemanticsProperties.HorizontalScrollAxisRange),
        )
        composeRule.onNodeWithTag("tool-detail-scroll:tool-1:result").assert(
            SemanticsMatcher.keyIsDefined(SemanticsProperties.HorizontalScrollAxisRange),
        )
        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
        composeRule.onNodeWithText("First line\nSecond line ▍").assertIsDisplayed()
        composeRule.onNodeWithText("Input").assertDoesNotExist()
        composeRule.onNodeWithText(
            "{\"command\":\"pwd\",\"workdir\":\"/workspace\"}",
        ).assertDoesNotExist()
    }

    @Test
    fun legacyRealtimeSkipsBlankArgumentsAndUsesStructuredAlias() {
        val session = session()
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = app.hermesmobile.protocol.sessions.RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        ).copy(
            running = true,
            streamingAssistantText = "Working",
            tools = listOf(
                LiveToolProjection(
                    key = "tool-fallback",
                    name = "terminal",
                    status = LiveToolStatus.RUNNING,
                    payload = buildJsonObject {
                        put("arguments", "")
                        put(
                            "args",
                            buildJsonObject {
                                put("command", "pwd")
                                put("workdir", "/workspace")
                            },
                        )
                    },
                ),
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = baseline,
                    realtime = realtime,
                    controlStatus = RealtimeControlStatus.OBSERVER,
                    realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Terminal(\"pwd\")").assertIsDisplayed()
        composeRule.onNodeWithText("Command: pwd\nWorkdir: /workspace").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand Args").performClick()
        composeRule.onNodeWithText("Command: pwd\nWorkdir: /workspace").assertIsDisplayed()
    }

    @Test
    fun structuredRealtimeUsesTheConversationTurnLayout() {
        val session = session()
        val baseline = transcript().copy(
            messages = emptyList(),
            pagination = TranscriptPagination(limit = 20, offset = 0, returned = 0),
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = app.hermesmobile.protocol.sessions.RuntimeSessionId("runtime-1"),
            connectionEpoch = 2,
        ).copy(
            running = false,
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:2:1",
                    text = "Partial **answer**",
                    reasoning = "Checking facts",
                    statusText = "Working",
                    status = AssistantTurnStatus.STREAMING,
                ),
                SessionTimelineItem.ToolActivity(
                    key = "tool:call-1",
                    toolId = "call-1",
                    name = "browser",
                    args = "https://example.com",
                    output = "Loading source",
                    status = ToolActivityStatus.RUNNING,
                ),
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = baseline,
                    realtime = realtime,
                    controlStatus = RealtimeControlStatus.OBSERVER,
                    realtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("transcript-list").performScrollToIndex(0)
        composeRule.onNodeWithContentDescription("Thinking").assertIsDisplayed()
        composeRule.onNodeWithText("Checking facts").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithTag("tool:tool:call-1")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Running",
                ),
            )
        composeRule.onNodeWithTag("tool:tool:call-1")
            .assertTextEquals("Browser(\"https://example.com\")", "Running")
        composeRule.onNodeWithText("Value: https://example.com").assertDoesNotExist()
        composeRule.onNodeWithText("Loading source").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Expand Args").performClick()
        composeRule.onNodeWithContentDescription("Collapse Result").performClick()
        composeRule.onNodeWithText("Value: https://example.com").assertIsDisplayed()
        composeRule.onNodeWithText("Loading source").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand Result").performClick()
        composeRule.onNodeWithText("Loading source").assertIsDisplayed()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
    }

    @Test
    fun structuredRealtimeResponseIsVisibleInTheConversationLayout() {
        val session = session()
        val baseline = transcript().copy(
            messages = emptyList(),
            pagination = TranscriptPagination(limit = 20, offset = 0, returned = 0),
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = app.hermesmobile.protocol.sessions.RuntimeSessionId("runtime-response"),
            connectionEpoch = 3,
        ).copy(
            running = false,
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:3:1",
                    text = "Partial **answer**",
                    reasoning = "Checking facts",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = baseline,
                    realtime = realtime,
                    controlStatus = RealtimeControlStatus.OBSERVER,
                    realtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
        composeRule.onNodeWithText("Partial answer ▍").assertIsDisplayed()
    }

    @Test
    fun disclosureChoiceSurvivesTemporaryTranscriptRemovalWithinTheScreen() {
        val session = session()
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = app.hermesmobile.protocol.sessions.RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:2:1",
                    text = "Answer",
                    reasoning = "Inspecting state",
                    statusText = "",
                    status = AssistantTurnStatus.COMPLETE,
                ),
            ),
        )
        var state by mutableStateOf(
            SessionBrowserUiState(
                phase = SessionBrowserPhase.TRANSCRIPT,
                sessions = listOf(session),
                selectedSession = session,
                transcript = baseline,
                realtime = realtime,
                controlStatus = RealtimeControlStatus.OBSERVER,
                realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithContentDescription("Thinking").performClick()
        composeRule.onNodeWithText("Inspecting state").assertDoesNotExist()

        composeRule.runOnIdle { state = state.copy(phase = SessionBrowserPhase.LOADING_TRANSCRIPT) }
        composeRule.onNodeWithText("Loading conversation…").assertIsDisplayed()
        composeRule.runOnIdle { state = state.copy(phase = SessionBrowserPhase.TRANSCRIPT) }

        composeRule.onNodeWithText("Inspecting state").assertDoesNotExist()
    }

    @Test
    fun historicalToolExchangeUsesOneConversationTurn() {
        val session = session()
        val seed = transcript().messages.single()
        val history = transcript().copy(
            messages = listOf(
                seed.copy(content = JsonPrimitive("Inspect the workspace")),
                seed.copy(
                    messageId = 2,
                    role = "assistant",
                    content = JsonPrimitive(""),
                    reasoning = "Checking the current directory",
                    toolCalls = buildJsonArray {
                        add(
                            buildJsonObject {
                                put("id", "call-1")
                                put(
                                    "function",
                                    buildJsonObject {
                                        put("name", "terminal")
                                        put("arguments", "{\"command\":\"pwd\"}")
                                    },
                                )
                            },
                        )
                    },
                ),
                seed.copy(
                    messageId = 3,
                    role = "tool",
                    content = JsonPrimitive("/workspace"),
                    toolCallId = "call-1",
                    toolName = "terminal",
                ),
                seed.copy(
                    messageId = 4,
                    role = "assistant",
                    content = JsonPrimitive("The workspace is **ready**."),
                ),
            ),
            pagination = TranscriptPagination(limit = 20, offset = 0, returned = 4),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.TRANSCRIPT,
                    sessions = listOf(session),
                    selectedSession = session,
                    transcript = history,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("❯").assertIsDisplayed()
        composeRule.onNodeWithText("Inspect the workspace").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Thinking").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithText("Terminal(\"pwd\")").assertIsDisplayed()
        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
        composeRule.onNodeWithText("The workspace is ready.").assertIsDisplayed()
    }

    @Test
    fun approvalDockShowsOnlyAuthorizedChoicesAndRoutesStableWireChoice() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "Hermes needs permission.",
            command = "./gradlew test",
            choices = listOf(
                SessionApprovalChoice.ALLOW_ONCE,
                SessionApprovalChoice.ALLOW_ALWAYS,
                SessionApprovalChoice.DENY,
            ),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        var selected: String? = null
        var submitRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session,
                    running = true,
                    pendingInput = pending,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
                onPendingChoice = { selected = it },
                onPendingSubmit = { submitRequests += 1 },
            )
        }

        composeRule.onNodeWithTag("pending-input-dock").assertIsDisplayed()
        composeRule.onNodeWithText("Run command?").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-choice:allow_once")
            .assertIsDisplayed()
            .assertHeightIsAtLeast(48.dp)
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
        composeRule.onNodeWithTag("pending-choice:allow_always")
            .assertHeightIsAtLeast(48.dp)
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Confirmation required",
                ),
            )
        composeRule.onNodeWithTag("pending-choice:deny").assertHeightIsAtLeast(48.dp)
        composeRule.onNodeWithText("Allow for session").assertDoesNotExist()
        composeRule.onNodeWithText("Allow once").performClick()
        composeRule.runOnIdle {
            check(selected == "allow_once")
            check(submitRequests == 1)
        }
        composeRule.onNodeWithText("Always allow").performClick()
        composeRule.runOnIdle {
            check(selected == "allow_always")
            check(submitRequests == 1)
        }
    }

    @Test
    fun omittedApprovalCapabilityKeepsAuthoritativeDecisionVisibleAndReadOnly() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "Hermes needs permission.",
            command = "./gradlew test",
            choices = listOf(SessionApprovalChoice.ALLOW_ONCE, SessionApprovalChoice.DENY),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session,
                    running = true,
                    pendingInput = pending,
                ).copy(
                    controlAvailableMethods = setOf(MobileControlMethods.ACQUIRE),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("pending-input-dock").assertIsDisplayed()
        composeRule.onNodeWithText("Run command?").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-choice:allow_once").assertIsNotEnabled()
        composeRule.onNodeWithTag("pending-choice:deny").assertIsNotEnabled()
    }

    @Test
    fun durableApprovalRequiresAnExplicitConfirmationStep() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Always allow this operation?",
            description = "This choice affects future sessions.",
            command = "./gradlew test",
            choices = listOf(SessionApprovalChoice.ALLOW_ALWAYS, SessionApprovalChoice.DENY),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        var state by mutableStateOf(
            controlledTranscriptState(
                session = session,
                running = true,
                pendingInput = pending,
            ),
        )
        var confirmations = 0
        var cancellations = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
                onPendingChoice = { choice ->
                    state = state.copy(
                        pendingInteraction = state.pendingInteraction.copy(
                            selectedChoiceId = choice,
                            requiresConfirmation = choice == SessionApprovalChoice.ALLOW_ALWAYS.wireValue,
                        ),
                    )
                },
                onPendingConfirm = { confirmations += 1 },
                onPendingCancelConfirmation = {
                    cancellations += 1
                    state = state.copy(
                        pendingInteraction = state.pendingInteraction.copy(
                            selectedChoiceId = null,
                            requiresConfirmation = false,
                        ),
                    )
                },
            )
        }

        composeRule.onNodeWithTag("pending-choice:allow_always").performClick()
        composeRule.onNodeWithTag("pending-confirmation").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-choice:allow_always").assertDoesNotExist()
        composeRule.onNodeWithTag("cancel-pending-confirmation")
            .assertHeightIsAtLeast(48.dp)
            .performClick()
        composeRule.runOnIdle { check(cancellations == 1) }
        composeRule.onNodeWithTag("pending-confirmation").assertDoesNotExist()
        composeRule.onNodeWithTag("pending-choice:allow_always")
            .assertIsDisplayed()
            .performClick()
        composeRule.onNodeWithTag("confirm-pending-choice")
            .assertHeightIsAtLeast(48.dp)
            .performClick()
        composeRule.runOnIdle { check(confirmations == 1) }
    }

    @Test
    fun clarifyDockShowsOtherFieldOnlyWhenAuthorized() {
        val session = session()
        val pending = SessionPendingInput.Clarify(
            requestId = "clarify-1",
            question = "Which target?",
            choices = listOf(
                app.hermesmobile.protocol.gateway.SessionClarifyChoice("staging", "Staging"),
            ),
            allowOther = false,
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session,
                    running = true,
                    pendingInput = pending,
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Which target?").assertIsDisplayed()
        composeRule.onNodeWithText("Staging").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-other-input").assertDoesNotExist()
    }

    @Test
    fun clarifyDockShowsSelectionAndSubmitsOneAuthoritativeChoiceId() {
        val session = session()
        val pending = SessionPendingInput.Clarify(
            requestId = "clarify-1",
            question = "Which target should Hermes use?",
            choices = listOf(
                app.hermesmobile.protocol.gateway.SessionClarifyChoice("staging", "Staging"),
                app.hermesmobile.protocol.gateway.SessionClarifyChoice("production", "Production"),
            ),
            allowOther = true,
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        var state by mutableStateOf(
            controlledTranscriptState(
                session = session,
                running = true,
                pendingInput = pending,
            ),
        )
        var submitRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
                onPendingChoice = { choice ->
                    state = state.copy(
                        pendingInteraction = state.pendingInteraction.copy(
                            selectedChoiceId = choice,
                            otherDraft = "",
                        ),
                    )
                },
                onPendingSubmit = { submitRequests += 1 },
            )
        }

        composeRule.onNodeWithTag("pending-choice:staging").performClick()
        composeRule.onNodeWithContentDescription("Other answer").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-choice:staging")
            .assertHeightIsAtLeast(48.dp)
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.RadioButton))
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Selected, true))
        composeRule.onNodeWithTag("pending-choice:production").assert(
            SemanticsMatcher.expectValue(SemanticsProperties.Selected, false),
        )
        composeRule.onNodeWithTag("submit-pending-answer").assertIsEnabled().performClick()
        composeRule.runOnIdle { check(submitRequests == 1) }
    }

    @Test
    fun approvalDeliveryUnknownAllowsOnlyExplicitFrozenPayloadRetry() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "Hermes needs permission.",
            command = "./gradlew test",
            choices = listOf(SessionApprovalChoice.ALLOW_ONCE, SessionApprovalChoice.DENY),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        var retryRequests = 0
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session,
                    running = true,
                    pendingInput = pending,
                ).copy(
                    pendingInteraction = PendingInputInteractionState(
                        requestId = pending.requestId,
                        selectedChoiceId = SessionApprovalChoice.ALLOW_ONCE.wireValue,
                        inFlightClientRequestId = ClientRequestId("response-1"),
                        outcome = PendingInputInteractionOutcome.RetryAvailable,
                    ),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
                onPendingSubmit = { retryRequests += 1 },
            )
        }

        composeRule.onNodeWithTag("pending-choice:allow_once")
            .assertIsNotEnabled()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Selected, true))
        composeRule.onNodeWithTag("pending-choice:deny")
            .assertIsNotEnabled()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Selected, false))
        composeRule.onNodeWithTag("session-status-strip").assertIsDisplayed()
        composeRule.onNodeWithText("Control connection restored").assertIsDisplayed()
        composeRule.onNodeWithText("INPUT REQUIRED · RESTORED").assertIsDisplayed()
        composeRule.onNodeWithText("Try again · same request").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-input-feedback").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.LiveRegion,
                LiveRegionMode.Polite,
            ),
        )
        composeRule.onNodeWithTag("retry-pending-input")
            .assertIsEnabled()
            .assertHeightIsAtLeast(48.dp)
            .performClick()
        composeRule.runOnIdle { check(retryRequests == 1) }
    }

    @Test
    fun acceptedPendingResponseKeepsAuthoritativeDockReadOnlyUntilSnapshotClears() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "Hermes needs permission.",
            command = "./gradlew test",
            choices = listOf(SessionApprovalChoice.ALLOW_ONCE),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        var state by mutableStateOf(
            controlledTranscriptState(
                session = session,
                running = true,
                pendingInput = pending,
            ),
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = state,
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("pending-input-dock").assertIsDisplayed()
        composeRule.runOnIdle {
            state = state.copy(
                pendingInteraction = PendingInputInteractionState(
                    outcome = PendingInputInteractionOutcome.Accepted,
                ),
            )
        }

        composeRule.onNodeWithTag("pending-input-dock").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-choice:allow_once").assertIsNotEnabled()
        composeRule.onNodeWithTag("pending-input-feedback").assertDoesNotExist()
        composeRule.onNodeWithTag("message-input").assertDoesNotExist()
    }

    @Test
    fun resolvedElsewhereKeepsAuthoritativeDockReadOnlyUntilSnapshotClears() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "Hermes needs permission.",
            command = "./gradlew test",
            choices = listOf(SessionApprovalChoice.ALLOW_ONCE),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session,
                    running = true,
                    pendingInput = pending,
                ).copy(
                    pendingInteraction = PendingInputInteractionState(
                        outcome = PendingInputInteractionOutcome.ResolvedElsewhere,
                    ),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("pending-input-dock").assertIsDisplayed()
        composeRule.onNodeWithTag("pending-choice:allow_once").assertIsNotEnabled()
        composeRule.onNodeWithTag("pending-input-feedback")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.LiveRegion,
                    LiveRegionMode.Polite,
                ),
            )
        composeRule.onNodeWithText("This request was resolved elsewhere.").assertIsDisplayed()
        composeRule.onNodeWithTag("message-input").assertDoesNotExist()
    }

    @Test
    fun pendingResponseFailureIsVisibleWithoutExposingHiddenServerDetail() {
        val session = session()
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "Hermes needs permission.",
            command = "./gradlew test",
            choices = listOf(SessionApprovalChoice.DENY),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        composeRule.setContent {
            SessionBrowserScreen(
                state = controlledTranscriptState(
                    session = session,
                    running = true,
                    pendingInput = pending,
                ).copy(
                    pendingInteraction = PendingInputInteractionState(
                        requestId = pending.requestId,
                        selectedChoiceId = SessionApprovalChoice.DENY.wireValue,
                        outcome = PendingInputInteractionOutcome.Failed(
                            "The selected response is no longer allowed.",
                        ),
                    ),
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = {},
                onSend = {},
            )
        }

        composeRule.onNodeWithTag("pending-input-feedback")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.LiveRegion,
                    LiveRegionMode.Assertive,
                ),
            )
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.Error,
                    "The selected response is no longer allowed.",
                ),
            )
        composeRule.onNodeWithText("The selected response is no longer allowed.")
            .assertIsDisplayed()
    }

    @Test
    fun streamingPerformanceFixtureUsesProductionScreenWithLongReducerBackedContent() {
        val fixture = streamingPerformanceReviewState()
        val realtime = checkNotNull(fixture.realtime)

        check(fixture.selectedSession?.title == "Streaming performance")
        check(checkNotNull(fixture.transcript).messages.size >= 160)
        check(realtime.lastEventOrdinal > 0)
        check(
            realtime.timeline
                .filterIsInstance<SessionTimelineItem.AssistantTurn>()
                .count { it.status == AssistantTurnStatus.COMPLETE } >= 4,
        )
        check(
            realtime.timeline
                .filterIsInstance<SessionTimelineItem.AssistantTurn>()
                .any { it.thinking.contains("## Thinking") },
        )
        check(
            realtime.timeline
                .filterIsInstance<SessionTimelineItem.AssistantTurn>()
                .any { it.text.contains("**Response") },
        )
        check(
            realtime.timeline
                .filterIsInstance<SessionTimelineItem.ToolActivity>()
                .any { it.output.lineSequence().count() >= 80 },
        )

        composeRule.setContent {
            HermesMobileTheme(darkTheme = true) {
                SessionBrowserScreen(
                    state = fixture,
                    onOpenSession = {},
                    onBack = {},
                    onRefresh = {},
                    onLoadMore = {},
                    onReconnect = {},
                    onSend = {},
                )
            }
        }

        composeRule.onNodeWithText("Streaming performance").assertIsDisplayed()
    }

    @Test
    fun authenticationFailureOffersReturnToConnection() {
        var reconnect = false
        composeRule.setContent {
            SessionBrowserScreen(
                state = SessionBrowserUiState(
                    phase = SessionBrowserPhase.AUTHENTICATION_REQUIRED,
                    message = "Sign in again to load Hermes sessions.",
                ),
                onOpenSession = {},
                onBack = {},
                onRefresh = {},
                onLoadMore = {},
                onReconnect = { reconnect = true },
                onSend = {},
            )
        }

        composeRule.onNodeWithText("Sign in again to load Hermes sessions.").assertIsDisplayed()
        composeRule.onNodeWithText("Back to connection").performClick()
        composeRule.runOnIdle { check(reconnect) }
    }

    private fun controlledTranscriptState(
        session: SessionProjection,
        running: Boolean,
        pendingInput: SessionPendingInput? = null,
    ): SessionBrowserUiState {
        val history = transcript()
        return SessionBrowserUiState(
            phase = SessionBrowserPhase.TRANSCRIPT,
            sessions = listOf(session),
            selectedSession = session,
            transcript = history,
            realtime = RealtimeSessionReducer().seed(
                transcript = history,
                runtimeSessionId = app.hermesmobile.protocol.sessions.RuntimeSessionId("runtime-1"),
                connectionEpoch = 1,
            ).copy(running = running),
            control = ControlState(
                mode = ControlMode.Controller(
                    SessionControlLease(
                        leaseId = SessionControlLeaseId("lease-1"),
                        expiresAtEpochMs = 9_000_000_000_000L,
                        controlRevision = 1L,
                        controllerKind = SessionControllerKind.MOBILE,
                        controllerLabel = "Hermes Mobile",
                        pendingInput = pendingInput,
                    ),
                ),
            ),
            controlAvailableMethods = MobileControlMethods.IMPLEMENTED,
            controlStatus = RealtimeControlStatus.CONTROLLER,
            realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
            pendingInteraction = PendingInputInteractionState(
                requestId = pendingInput?.requestId,
            ),
        )
    }

    private fun session() = SessionProjection(
        sessionKey = SessionKey("stored-1"),
        lineageRoot = SessionKey("stored-1"),
        lineageTip = SessionKey("tip-1"),
        parentSessionKey = null,
        title = "First session",
        preview = "Preview text",
        source = "desktop",
        model = "test-model",
        profile = null,
        cwd = null,
        gitBranch = null,
        startedAtEpochSeconds = 100.0,
        endedAtEpochSeconds = null,
        lastActiveEpochSeconds = 120.0,
        messageCount = 3,
        toolCallCount = 1,
        inputTokens = 10,
        outputTokens = 20,
        isActive = true,
        archived = false,
    )

    private fun transcript() = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = SessionKey("tip-1"),
        messages = listOf(
            SessionMessageProjection(
                messageId = 1,
                role = "user",
                content = JsonPrimitive("hello from Android"),
                timestampEpochSeconds = 100.0,
                reasoning = null,
                reasoningContent = null,
                reasoningDetails = null,
                toolCallId = null,
                toolCalls = null,
                toolName = null,
                displayKind = null,
                displayMetadata = null,
            ),
        ),
        pagination = TranscriptPagination(limit = 200, offset = 0, returned = 1),
    )

    private fun longTranscript(messageCount: Int) = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = SessionKey("tip-1"),
        messages = (0 until messageCount).map { messageId ->
            SessionMessageProjection(
                messageId = messageId.toLong(),
                role = "user",
                content = JsonPrimitive("message-$messageId"),
                timestampEpochSeconds = 100.0 + messageId,
                reasoning = null,
                reasoningContent = null,
                reasoningDetails = null,
                toolCallId = null,
                toolCalls = null,
                toolName = null,
                displayKind = null,
                displayMetadata = null,
            )
        },
        pagination = TranscriptPagination(
            limit = 200,
            offset = 0,
            returned = messageCount,
        ),
    )
}
