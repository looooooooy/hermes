package app.hermesmobile.sessions

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.unit.dp
import org.junit.Rule
import org.junit.Test

class HermesStyleVisualFixtureTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun keepHermesTurnVisibleForVisualReview() {
        composeRule.setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    ConversationTurnContent(
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 32.dp),
                        turn = ConversationTurnUiModel(
                            key = "visual-turn",
                            userPrompt = ConversationPromptUiModel(
                                key = "visual-prompt",
                                text = "Inspect the workspace and summarize its state.",
                            ),
                            thinking = "I should inspect the current directory and verify the build output.",
                            statusText = "",
                            tools = listOf(
                                ConversationToolUiModel(
                                    key = "visual-tool",
                                    toolId = "visual-call",
                                    name = "terminal",
                                    callLabel = "Terminal(\"./gradlew test\")",
                                    argumentDetails = listOf(
                                        ConversationToolDetailUiModel("Command", "./gradlew test"),
                                        ConversationToolDetailUiModel("Workdir", "/workspace"),
                                    ),
                                    output = "BUILD SUCCESSFUL",
                                    resultDetails = listOf(
                                        ConversationToolDetailUiModel("Exit code", "0"),
                                    ),
                                    durationSeconds = 2.7,
                                    status = ConversationToolStatus.COMPLETE,
                                ),
                            ),
                            response = "The workspace is **ready**.\n\n- Tests passed\n- Build completed",
                            status = ConversationTurnStatus.COMPLETE,
                        ),
                    )
                }
            }
        }
        composeRule.waitForIdle()
        Thread.sleep(15_000)
    }
}
