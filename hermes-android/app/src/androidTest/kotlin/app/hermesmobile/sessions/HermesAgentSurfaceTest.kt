package app.hermesmobile.sessions

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import app.hermesmobile.ui.theme.HermesMobileTheme
import org.junit.Rule
import org.junit.Test

class HermesAgentSurfaceTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun guideAndQueueReuseOneAuthoritativeInputAndSubmissionControl() {
        var guidanceMode by mutableStateOf(false)
        var queueDraft by mutableStateOf("Queue this next")
        var guidanceDraft by mutableStateOf("Verify authorization")
        var queueSubmissions = 0
        var guidanceSubmissions = 0

        composeRule.setContent {
            val activeDraft = if (guidanceMode) guidanceDraft else queueDraft
            HermesMobileTheme(darkTheme = true) {
                ProvideHermesTranscriptDesignSystem {
                    HermesAgentComposer(
                        draft = activeDraft,
                        presentation = TranscriptComposerPresentation(
                            primaryAction = if (guidanceMode) {
                                TranscriptComposerPrimaryAction.Guide
                            } else {
                                TranscriptComposerPrimaryAction.Queue
                            },
                            inputEnabled = true,
                            primaryEnabled = activeDraft.isNotBlank(),
                            keyboardSendEnabled = true,
                            stopActionVisible = true,
                            stopEnabled = true,
                        ),
                        isInterrupting = false,
                        guidanceMode = guidanceMode,
                        guidanceActionVisible = true,
                        onDraftChanged = {
                            if (guidanceMode) guidanceDraft = it else queueDraft = it
                        },
                        onSubmit = {
                            if (guidanceMode) guidanceSubmissions += 1 else queueSubmissions += 1
                        },
                        onStop = {},
                        onGuideMode = { guidanceMode = true },
                        onQueueMode = { guidanceMode = false },
                    )
                }
            }
        }

        composeRule.onAllNodesWithTag("message-input").assertCountEquals(1)
        composeRule.onNodeWithTag("message-input").assertTextEquals("Queue this next")
        composeRule.onNodeWithTag("queue-mode-toggle").assertIsSelected()
        composeRule.onNodeWithTag("queue-button").assertIsDisplayed()
        composeRule.onNodeWithText("Next message: queue after the active turn").assertIsDisplayed()

        composeRule.onNodeWithTag("guidance-toggle").performClick()
        composeRule.onAllNodesWithTag("message-input").assertCountEquals(1)
        composeRule.onNodeWithTag("message-input").assertTextEquals("Verify authorization")
        composeRule.onNodeWithTag("guidance-toggle").assertIsSelected()
        composeRule.onNodeWithTag("guidance-submit-button").performClick()
        composeRule.runOnIdle {
            check(guidanceSubmissions == 1)
            check(queueSubmissions == 0)
        }

        composeRule.onNodeWithTag("queue-mode-toggle").performClick()
        composeRule.onAllNodesWithTag("message-input").assertCountEquals(1)
        composeRule.onNodeWithTag("message-input").assertTextEquals("Queue this next")
        composeRule.onNodeWithTag("queue-button").performClick()
        composeRule.runOnIdle {
            check(guidanceSubmissions == 1)
            check(queueSubmissions == 1)
        }
    }

    @Test
    fun composerActionsMeetTheMinimumTouchTarget() {
        composeRule.setContent {
            HermesMobileTheme(darkTheme = true) {
                ProvideHermesTranscriptDesignSystem {
                    HermesAgentComposer(
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
                        guidanceMode = false,
                        guidanceActionVisible = true,
                        onDraftChanged = {},
                        onSubmit = {},
                        onStop = {},
                        onGuideMode = {},
                        onQueueMode = {},
                    )
                }
            }
        }

        listOf(
            "message-input",
            "guidance-toggle",
            "queue-mode-toggle",
            "voice-input-button",
            "queue-button",
            "stop-button",
        ).forEach { tag ->
            composeRule.onNodeWithTag(tag).assertHeightIsAtLeast(48.dp)
        }
    }

    @Test
    fun longRunningDockPreservesTodoAndSubagentAsDistinctLocators() {
        var expanded by mutableStateOf(true)
        var selectedKind by mutableStateOf<LongRunningWorkKind?>(null)
        var clickedKind: LongRunningWorkKind? = null
        val presentation = LongRunningWorkPresentation(
            items = listOf(
                LongRunningWorkItemPresentation(
                    turnKey = "turn-1",
                    sectionKey = ConversationDisclosureStateKey(
                        turnKey = "turn-1",
                        section = ConversationDisclosureSection.TODO,
                    ),
                    kind = LongRunningWorkKind.TODO,
                    progressNumerator = 1,
                    progressDenominator = 2,
                    currentLabel = "Review Subagent presentation",
                    status = HermesTranscriptStatus.Running,
                ),
                LongRunningWorkItemPresentation(
                    turnKey = "turn-1",
                    sectionKey = ConversationDisclosureStateKey(
                        turnKey = "turn-1",
                        section = ConversationDisclosureSection.SUBAGENTS,
                    ),
                    kind = LongRunningWorkKind.SUBAGENT,
                    progressNumerator = 1,
                    progressDenominator = 2,
                    currentLabel = "Composer review",
                    status = HermesTranscriptStatus.Running,
                ),
            ),
        )

        composeRule.setContent {
            HermesMobileTheme(darkTheme = true) {
                ProvideHermesTranscriptDesignSystem {
                    HermesLongRunningWorkDock(
                        presentation = presentation,
                        expanded = expanded,
                        selectedKind = selectedKind,
                        onExpandedChange = { expanded = it },
                        onItemClick = {
                            selectedKind = it.kind
                            clickedKind = it.kind
                        },
                    )
                }
            }
        }

        composeRule.onNodeWithTag("long-running-toggle")
            .assertHeightIsAtLeast(48.dp)
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Expanded",
                ),
            )
        composeRule.onNodeWithTag("long-running-todo")
            .assertHeightIsAtLeast(48.dp)
            .assertIsDisplayed()
        composeRule.onNodeWithTag("long-running-subagent")
            .assertHeightIsAtLeast(48.dp)
            .assertIsDisplayed()
        composeRule.onNodeWithText("TODO · 1/2").assertIsDisplayed()
        composeRule.onNodeWithText("SUBAGENT · 1/2").assertIsDisplayed()

        composeRule.onNodeWithTag("long-running-todo").performClick().assertIsSelected()
        composeRule.runOnIdle { check(clickedKind == LongRunningWorkKind.TODO) }

        composeRule.onNodeWithTag("long-running-toggle").performClick()
        composeRule.onNodeWithTag("long-running-toggle").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.StateDescription,
                "Collapsed",
            ),
        )
        composeRule.onNodeWithTag("long-running-todo").assertDoesNotExist()
        composeRule.onNodeWithTag("long-running-subagent").assertDoesNotExist()
    }
}
