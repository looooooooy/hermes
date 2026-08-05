package app.hermesmobile.sessions

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled

import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.Density
import app.hermesmobile.ui.theme.HermesMobileTheme
import org.junit.Rule
import org.junit.Test

class SessionTranscriptComposerTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun guideActionLivesInTheComposerAndExposesItsDisclosureState() {
        var expanded by mutableStateOf(false)
        composeRule.setContent {
            HermesMobileTheme {
                ProvideHermesTranscriptDesignSystem {
                    HermesTranscriptComposer(
                        draft = "Inspect logs",
                        presentation = editableComposerPresentation(),
                        isInterrupting = false,
                        onDraftChanged = {},
                        onSend = {},
                        onStop = {},
                        guidanceActionVisible = true,
                        guidanceExpanded = expanded,
                        onGuidanceAction = { expanded = !expanded },
                    )
                }
            }
        }

        composeRule.onNodeWithContentDescription("Expand guidance")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Collapsed",
                ),
            )
            .performClick()
        composeRule.onNodeWithContentDescription("Collapse guidance")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Expanded",
                ),
            )
        composeRule.onNodeWithTag("message-input").assertIsEnabled()
    }

    @Test
    fun idleVoiceActionIsAccessibleAndKeepsTheDraftEditable() {
        composeRule.setContent {
            HermesMobileTheme {
                ProvideHermesTranscriptDesignSystem {
                    HermesTranscriptComposer(
                        draft = "Inspect logs",
                        presentation = editableComposerPresentation(),
                        isInterrupting = false,
                        voiceInputState = SessionVoiceInputState(),
                        onDraftChanged = {},
                        onSend = {},
                        onStop = {},
                        onVoiceAction = {},
                    )
                }
            }
        }

        composeRule.onNodeWithTag("message-input").assertIsEnabled()
        composeRule.onNodeWithTag("voice-input-button")
            .assertIsDisplayed()
            .assertIsEnabled()
            .assertHeightIsAtLeast(48.dp)
        composeRule.onNodeWithContentDescription("Start voice input").assertIsDisplayed()
    }

    @Test
    fun listeningLocksManualEditingAndOffersAnAccessibleCancelAction() {
        var cancelRequests = 0
        composeRule.setContent {
            HermesMobileTheme {
                ProvideHermesTranscriptDesignSystem {
                    HermesTranscriptComposer(
                        draft = "Inspect logs partial words",
                        presentation = editableComposerPresentation(),
                        isInterrupting = false,
                        voiceInputState = SessionVoiceInputState(
                            phase = SessionVoiceInputPhase.LISTENING,
                            baseDraft = "Inspect logs",
                            partialTranscript = "partial words",
                        ),
                        onDraftChanged = {},
                        onSend = {},
                        onStop = {},
                        onVoiceAction = { cancelRequests += 1 },
                    )
                }
            }
        }

        composeRule.onNodeWithTag("message-input").assertIsNotEnabled()
        composeRule.onNodeWithText("Listening…").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Cancel voice input")
            .assertIsDisplayed()
            .performClick()
        composeRule.runOnIdle { check(cancelRequests == 1) }
    }

    @Test
    fun permissionFailureIsVisibleWithoutExposingRecognizerDiagnostics() {
        composeRule.setContent {
            HermesMobileTheme {
                ProvideHermesTranscriptDesignSystem {
                    HermesTranscriptComposer(
                        draft = "Inspect logs",
                        presentation = editableComposerPresentation(),
                        isInterrupting = false,
                        voiceInputState = SessionVoiceInputState(
                            phase = SessionVoiceInputPhase.ERROR,
                            baseDraft = "Inspect logs",
                            failure = SessionVoiceInputFailure.PERMISSION_DENIED,
                        ),
                        onDraftChanged = {},
                        onSend = {},
                        onStop = {},
                        onVoiceAction = {},
                    )
                }
            }
        }

        composeRule.onNodeWithText("Microphone permission was denied.").assertIsDisplayed()
        composeRule.onNodeWithTag("voice-input-feedback").assertIsDisplayed()
        composeRule.onNodeWithTag("message-input").assertIsEnabled()
    }

    @Test
    fun narrowLargeFontComposerKeepsActionsInsideBoundsWithoutOverlap() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                HermesMobileTheme {
                    ProvideHermesTranscriptDesignSystem {
                        Box(
                            modifier = androidx.compose.ui.Modifier
                                .width(320.dp)
                                .testTag("composer-root"),
                        ) {
                            HermesTranscriptComposer(
                                draft = "Queue this next",
                                presentation = TranscriptComposerPresentation(
                                    primaryAction = TranscriptComposerPrimaryAction.Queue,
                                    inputEnabled = true,
                                    primaryEnabled = true,
                                    keyboardSendEnabled = true,
                                    stopActionVisible = true,
                                    stopEnabled = true,
                                ),
                                isInterrupting = false,
                                onDraftChanged = {},
                                onSend = {},
                                onStop = {},
                                guidanceActionVisible = true,
                                guidanceExpanded = false,
                            )
                        }
                    }
                }
            }
        }

        val root = composeRule.onNodeWithTag("composer-root").fetchSemanticsNode().boundsInRoot
        val message = composeRule.onNodeWithTag("message-input")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val voice = composeRule.onNodeWithTag("voice-input-button")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val guide = composeRule.onNodeWithTag("guidance-toggle")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val queue = composeRule.onNodeWithTag("queue-button")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val stop = composeRule.onNodeWithTag("stop-button")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot

        listOf(message, voice, guide, queue, stop).forEach { bounds ->
            check(bounds.left >= root.left)
            check(bounds.right <= root.right)
            check(bounds.top >= root.top)
            check(bounds.bottom <= root.bottom)
        }
        check(message.right <= voice.left)
        check(guide.right <= queue.left)
        check(queue.right <= stop.left)
        composeRule.onNodeWithContentDescription("Expand guidance").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Message").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Start voice input").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Queue").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Stop").assertIsDisplayed()
    }

    @Test
    fun normalWidthComposerKeepsSendAndGuideInsideBoundsWithoutOverlap() {
        composeRule.setContent {
            HermesMobileTheme {
                ProvideHermesTranscriptDesignSystem {
                    Box(
                        modifier = androidx.compose.ui.Modifier
                            .width(412.dp)
                            .testTag("composer-root"),
                    ) {
                        HermesTranscriptComposer(
                            draft = "Send this turn",
                            presentation = editableComposerPresentation(),
                            isInterrupting = false,
                            onDraftChanged = {},
                            onSend = {},
                            onStop = {},
                            guidanceActionVisible = true,
                            guidanceExpanded = false,
                        )
                    }
                }
            }
        }

        val root = composeRule.onNodeWithTag("composer-root").fetchSemanticsNode().boundsInRoot
        val message = composeRule.onNodeWithTag("message-input")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val voice = composeRule.onNodeWithTag("voice-input-button")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val guide = composeRule.onNodeWithTag("guidance-toggle")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot
        val send = composeRule.onNodeWithTag("send-button")
            .assertHeightIsAtLeast(48.dp)
            .fetchSemanticsNode().boundsInRoot

        listOf(message, voice, guide, send).forEach { bounds ->
            check(bounds.left >= root.left)
            check(bounds.right <= root.right)
            check(bounds.top >= root.top)
            check(bounds.bottom <= root.bottom)
        }
        check(message.right <= voice.left)
        check(guide.right <= send.left)
        composeRule.onNodeWithContentDescription("Message").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Send").assertIsDisplayed()
    }

    private fun editableComposerPresentation() = TranscriptComposerPresentation(
        primaryAction = TranscriptComposerPrimaryAction.Send,
        inputEnabled = true,
        primaryEnabled = true,
        keyboardSendEnabled = true,
        stopActionVisible = false,
        stopEnabled = false,
    )
}
