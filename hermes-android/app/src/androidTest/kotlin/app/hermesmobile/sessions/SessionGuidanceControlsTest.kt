package app.hermesmobile.sessions

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import app.hermesmobile.ui.theme.HermesMobileTheme
import org.junit.Rule
import org.junit.Test

class SessionGuidanceControlsTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun expandedGuidancePanelExposesDistinctInputAndSubmitSemantics() {
        composeRule.setContent {
            HermesMobileTheme {
                SessionGuidanceControls(
                    state = SessionGuidanceState(draft = "Check authorization"),
                    visible = true,
                    expanded = true,
                    inputEnabled = true,
                    onDraftChanged = {},
                    onSubmit = {},
                )
            }
        }

        composeRule.onNodeWithContentDescription("Supplemental guidance instruction").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Submit guidance").assertIsDisplayed()
    }
}
