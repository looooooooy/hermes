package app.hermesmobile.sessions

import androidx.compose.ui.platform.ClipboardManager
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.junit.Rule
import org.junit.Test

class HermesMarkdownContentTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun structuredMarkdownRendersAndCodeCanBeCopied() {
        lateinit var clipboard: ClipboardManager
        composeRule.setContent {
            clipboard = LocalClipboardManager.current
            HermesMarkdownContent(
                markdown = "## Live answer\n\n- first\n- second\n\n```kotlin\nval answer = 42\n```",
            )
        }

        composeRule.onNodeWithText("Live answer").assertIsDisplayed()
        composeRule.onNodeWithText("first").assertIsDisplayed()
        composeRule.onNodeWithText("second").assertIsDisplayed()
        composeRule.onNodeWithText("kotlin").assertIsDisplayed()
        composeRule.onNodeWithText("val answer = 42").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand code").performClick()
        composeRule.onNodeWithText("val answer = 42").assertIsDisplayed()
        composeRule.onNodeWithTag("code-scroll-0")
            .assertIsDisplayed()
            .assert(SemanticsMatcher.keyIsDefined(SemanticsProperties.HorizontalScrollAxisRange))
        composeRule.onNodeWithText("Copy").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Copy code").assertIsDisplayed()
        composeRule.onNodeWithTag("copy-code-0").performClick()

        composeRule.runOnIdle {
            check(clipboard.getText()?.text == "val answer = 42")
        }
    }

    @Test
    fun unfinishedFenceIsStillPresentedAsCode() {
        composeRule.setContent {
            HermesMarkdownContent(markdown = "```json\n{\"running\": true}")
        }

        composeRule.onNodeWithText("json").assertIsDisplayed()
        composeRule.onNodeWithText("{\"running\": true}").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand code").performClick()
        composeRule.onNodeWithText("{\"running\": true}").assertIsDisplayed()
    }

    @Test
    fun codeDisclosureSurvivesSettlementButNotUnrelatedReplacement() {
        val markdown = mutableStateOf("```kotlin\nval answer = 42")
        composeRule.setContent {
            HermesMarkdownContent(markdown = markdown.value, streaming = true)
        }

        composeRule.onNodeWithContentDescription("Expand code").performClick()
        composeRule.onNodeWithText("val answer = 42").assertIsDisplayed()

        composeRule.runOnIdle {
            markdown.value = "```kotlin\nval answer = 42\n```\n\nNext"
        }
        composeRule.onNodeWithText("val answer = 42").assertIsDisplayed()

        composeRule.runOnIdle {
            markdown.value = "```kotlin\nval replacement = 7\n```"
        }
        composeRule.onNodeWithText("val replacement = 7").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand code").assertIsDisplayed()
    }

    @Test
    fun inlineMarkdownRendersReadableTextWithoutSyntaxMarkers() {
        composeRule.setContent {
            HermesMarkdownContent(
                markdown = "Use **bold**, *care*, `pwd`, and [docs](https://example.com).",
            )
        }

        composeRule.onNodeWithText("Use bold, care, pwd, and docs.").assertIsDisplayed()
    }

    @Test
    fun inlineMarkdownIsConsistentAcrossStructuredBlocks() {
        composeRule.setContent {
            HermesMarkdownContent(
                markdown = "## Live **answer**\n\n- Run `tests`\n\n> Keep *context*\n\n" +
                    "| **Tool** | State |\n| --- | --- |\n| terminal | `Running` |",
            )
        }

        composeRule.onNodeWithText("Live answer").assertIsDisplayed()
        composeRule.onNodeWithText("Run tests").assertIsDisplayed()
        composeRule.onNodeWithText("Keep context").assertIsDisplayed()
        composeRule.onNodeWithText("Tool").assertIsDisplayed()
        composeRule.onNodeWithText("Running").assertIsDisplayed()
    }

    @Test
    fun terminalControlSequencesAreSanitizedBeforeRendering() {
        composeRule.setContent {
            HermesMarkdownContent(
                markdown = "before \u001B[31mred\u001B[0m " +
                    "\u001B]8;;https://example.com\u001B\\docs\u001B]8;;\u001B\\ after",
            )
        }

        composeRule.onNodeWithText("before red docs after").assertIsDisplayed()
    }
}
