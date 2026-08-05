package app.hermesmobile.sessions

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertWidthIsEqualTo
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import org.junit.Rule
import org.junit.Test

class HermesTranscriptPrimitivesTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun promptIsFlatSelectableContentWithA48DpSemanticBoundary() {
        composeRule.setContent {
            MaterialTheme {
                ProvideHermesTranscriptDesignSystem(darkTheme = true) {
                    HermesTranscriptPrompt(text = "Inspect the current workspace")
                }
            }
        }

        composeRule.onNodeWithContentDescription("User prompt")
            .assertIsDisplayed()
            .assertHeightIsAtLeast(48.dp)
        composeRule.onNodeWithText("❯").assertIsDisplayed()
        composeRule.onNodeWithText("Inspect the current workspace").assertIsDisplayed()
    }

    @Test
    fun processRailUsesNativeGeometryAtThe24DpGutterWithoutTreeGlyphText() {
        composeRule.setContent {
            MaterialTheme {
                ProvideHermesTranscriptDesignSystem(darkTheme = true) {
                    Box(Modifier.height(48.dp)) {
                        HermesTranscriptProcessRail(
                            ancestorContinuations = emptyList(),
                            branchLast = true,
                            modifier = Modifier.testTag("process-rail"),
                        )
                    }
                }
            }
        }

        composeRule.onNodeWithTag("process-rail")
            .assertIsDisplayed()
            .assertWidthIsEqualTo(24.dp)
        composeRule.onNodeWithText("├─").assertDoesNotExist()
        composeRule.onNodeWithText("└─").assertDoesNotExist()
        composeRule.onNodeWithText("│").assertDoesNotExist()
    }

    @Test
    fun responseBoundaryIsAnExplicitCompactNonInteractiveTranscriptSection() {
        composeRule.setContent {
            MaterialTheme {
                ProvideHermesTranscriptDesignSystem(darkTheme = false) {
                    HermesTranscriptResponseBoundary()
                }
            }
        }

        composeRule.onNodeWithContentDescription("Response boundary")
            .assertIsDisplayed()
            .assertHeightIsAtLeast(16.dp)
        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
    }

    @Test
    fun disclosureHeaderExposesExpandedStateAndA48DpClickTarget() {
        var expanded by mutableStateOf(true)
        composeRule.setContent {
            MaterialTheme {
                ProvideHermesTranscriptDesignSystem(darkTheme = true) {
                    HermesTranscriptDisclosureHeader(
                        title = "Thinking",
                        expanded = expanded,
                        onExpandedChange = { expanded = it },
                    )
                }
            }
        }

        composeRule.onNodeWithContentDescription("Thinking")
            .assertIsDisplayed()
            .assertHasClickAction()
            .assertHeightIsAtLeast(48.dp)
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Expanded",
                ),
            )
            .performClick()

        composeRule.onNodeWithContentDescription("Thinking").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.StateDescription,
                "Collapsed",
            ),
        )
    }
}
