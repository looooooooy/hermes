package app.hermesmobile.sessions

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertHeightIsEqualTo
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Rule
import org.junit.Test

class ConversationTurnContentTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun canonicalSectionsRenderNativeProcessHierarchyWithoutTerminalRails() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-canonical",
                        userPrompt = null,
                        thinking = "",
                        statusText = "",
                        tools = emptyList(),
                        response = "",
                        status = ConversationTurnStatus.STREAMING,
                        sections = CanonicalConversationSections.of(
                            HermesConversationSection.Todo(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-canonical:todo",
                                    status = HermesConversationSectionStatus.RUNNING,
                                ),
                                items = listOf(
                                    HermesConversationTodoItem(
                                        key = "inspect",
                                        content = "Inspect the projector",
                                        status = HermesConversationTodoStatus.COMPLETED,
                                    ),
                                    HermesConversationTodoItem(
                                        key = "render",
                                        content = "Render canonical sections",
                                        status = HermesConversationTodoStatus.IN_PROGRESS,
                                    ),
                                ),
                            ),
                            HermesConversationSection.Subagents(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-canonical:subagents",
                                    status = HermesConversationSectionStatus.RUNNING,
                                ),
                                subagents = listOf(
                                    HermesConversationSubagent(
                                        key = "child-1",
                                        goal = "Review the renderer",
                                        status = HermesConversationSectionStatus.RUNNING,
                                    ),
                                ),
                                moaReferences = listOf(
                                    HermesConversationMoaReference(
                                        key = "ref-1",
                                        label = "Advisor 1",
                                        text = "Keep stable section identity",
                                    ),
                                ),
                            ),
                            HermesConversationSection.ResponseBoundary(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-canonical:response-boundary",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                            ),
                            HermesConversationSection.AssistantResponse(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-canonical:response",
                                    status = HermesConversationSectionStatus.STREAMING,
                                ),
                                text = "Canonical **answer**",
                            ),
                            HermesConversationSection.Diff(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-canonical:diff",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                                text = "- old\n+ new",
                            ),
                        ),
                    ),
                )
            }
        }

        composeRule.onNodeWithText("Todo (2)").assertIsDisplayed()
        composeRule.onNodeWithText("Render canonical sections").assertIsDisplayed()
        composeRule.onNodeWithTag("todo:inspect").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.StateDescription,
                "Complete",
            ),
        )
        composeRule.onNodeWithTag("todo:render").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.StateDescription,
                "Running",
            ),
        )
        composeRule.onNodeWithText("Subagents / MoA (1)").assertIsDisplayed()
        composeRule.onNodeWithText("Review the renderer").assertIsDisplayed()
        composeRule.onNodeWithTag("subagent:child-1").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.StateDescription,
                "Running",
            ),
        )
        composeRule.onNodeWithText("Advisor 1").assertIsDisplayed()
        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
        composeRule.onNodeWithText("Canonical answer ▍").assertIsDisplayed()
        composeRule.onNodeWithText("- old\n+ new").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand Diff").performClick()
        composeRule.onNodeWithText("- old\n+ new").assertIsDisplayed()
        composeRule.onNodeWithTag("diff-scroll:turn-canonical:diff").assertIsDisplayed()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
        composeRule.onNodeWithText("├─").assertDoesNotExist()
    }

    @Test
    fun canonicalSectionsRenderInAuthoritativeChronologicalOrder() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-chronological",
                        userPrompt = null,
                        thinking = "Thinking after tool.",
                        statusText = "",
                        tools = emptyList(),
                        response = "Narration before tool.\n\nFinal response.",
                        status = ConversationTurnStatus.COMPLETE,
                        sections = CanonicalConversationSections.of(
                            HermesConversationSection.AssistantResponse(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-chronological:narration",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                                text = "Narration before tool.",
                            ),
                            HermesConversationSection.ToolGroup(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-chronological:tool",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                                tools = listOf(
                                    ConversationToolUiModel(
                                        key = "tool-order",
                                        toolId = "call-order",
                                        name = "terminal",
                                        callLabel = "Terminal(\"pwd\")",
                                        status = ConversationToolStatus.COMPLETE,
                                    ),
                                ),
                            ),
                            HermesConversationSection.Thinking(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-chronological:thinking",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                                text = "Thinking after tool.",
                            ),
                            HermesConversationSection.ResponseBoundary(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-chronological:response-boundary",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                            ),
                            HermesConversationSection.AssistantResponse(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-chronological:final-response",
                                    status = HermesConversationSectionStatus.COMPLETE,
                                ),
                                text = "Final response.",
                            ),
                        ),
                    ),
                )
            }
        }

        val narrationTop = composeRule.onNodeWithText("Narration before tool.")
            .fetchSemanticsNode().boundsInRoot.top
        val toolTop = composeRule.onNodeWithTag("tool:tool-order")
            .fetchSemanticsNode().boundsInRoot.top
        val thinkingTop = composeRule.onNodeWithText("Thinking after tool.")
            .fetchSemanticsNode().boundsInRoot.top
        val responseTop = composeRule.onNodeWithText("Final response.")
            .fetchSemanticsNode().boundsInRoot.top

        assertTrue(narrationTop < toolTop)
        assertTrue(toolTop < thinkingTop)
        assertTrue(thinkingTop < responseTop)
        assertEquals(
            composeRule.onNodeWithTag("conversation-section:turn-chronological:tool")
                .fetchSemanticsNode().boundsInRoot.left,
            composeRule.onNodeWithTag("tool:tool-order")
                .fetchSemanticsNode().boundsInRoot.left,
            0.5f,
        )
        assertEquals(
            composeRule.onNodeWithTag("conversation-section:turn-chronological:thinking")
                .fetchSemanticsNode().boundsInRoot.left,
            composeRule.onNodeWithText("Thinking after tool.")
                .fetchSemanticsNode().boundsInRoot.left,
            0.5f,
        )
    }

    @Test
    fun thinkingUsesStructuredStreamingMarkdownWithoutShowingSyntaxMarkers() {
        val markdown = "## Plan\n\nUse **cached blocks**."
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-thinking-markdown",
                        userPrompt = null,
                        thinking = markdown,
                        statusText = "",
                        tools = emptyList(),
                        response = "",
                        status = ConversationTurnStatus.STREAMING,
                        sections = CanonicalConversationSections.of(
                            HermesConversationSection.Thinking(
                                metadata = HermesConversationSectionMetadata(
                                    key = "turn-thinking-markdown:thinking",
                                    status = HermesConversationSectionStatus.STREAMING,
                                ),
                                text = markdown,
                            ),
                        ),
                    ),
                )
            }
        }

        composeRule.onNodeWithText("Plan").assertIsDisplayed()
        composeRule.onNodeWithText("Use cached blocks. ▍").assertIsDisplayed()
        composeRule.onNodeWithText(markdown).assertDoesNotExist()
    }

    @Test
    fun completedTurnUsesCompactProcessRowsAndCollapsedDetails() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-1",
                        userPrompt = ConversationPromptUiModel(
                            key = "prompt-1",
                            text = "Inspect the workspace",
                        ),
                        thinking = "I should inspect the current directory.",
                        statusText = "",
                        tools = listOf(
                            ConversationToolUiModel(
                                key = "tool-1",
                                toolId = "call-1",
                                name = "terminal",
                                callLabel = "Terminal(\"pwd\")",
                                arguments = "{\"command\":\"pwd\",\"workdir\":\"/workspace\"}",
                                argumentDetails = listOf(
                                    ConversationToolDetailUiModel("Command", "pwd"),
                                    ConversationToolDetailUiModel("Workdir", "/workspace"),
                                ),
                                output = "/workspace",
                                resultDetails = listOf(
                                    ConversationToolDetailUiModel("Exit code", "0"),
                                ),
                                durationSeconds = 0.4,
                                status = ConversationToolStatus.COMPLETE,
                            ),
                        ),
                        response = "The workspace is **ready**.",
                        status = ConversationTurnStatus.COMPLETE,
                    ),
                )
            }
        }

        composeRule.onNodeWithText("❯").assertIsDisplayed()
        composeRule.onNodeWithText("Inspect the workspace").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Thinking").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithTag("tool:tool-1")
            .assertIsDisplayed()
            .assertHeightIsEqualTo(48.dp)
            .assertTextEquals("Terminal(\"pwd\") (0.4s)", "Complete")
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Complete",
                ),
            )
            .assert(SemanticsMatcher.keyNotDefined(SemanticsProperties.ContentDescription))
        composeRule.onNodeWithContentDescription("Tool complete").assertDoesNotExist()
        composeRule.onNodeWithText("Args:\nCommand: pwd\nWorkdir: /workspace").assertDoesNotExist()
        composeRule.onNodeWithText("/workspace\nExit code: 0").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Expand Args").performClick()
        composeRule.onNodeWithText("Command: pwd\nWorkdir: /workspace").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Collapse Result").assertIsDisplayed()
        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
        composeRule.onNodeWithText("├─ ▾ Thinking").assertDoesNotExist()
        composeRule.onNodeWithText("└─ ▾ Tool calls (1)").assertDoesNotExist()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
        composeRule.onNodeWithText("The workspace is ready.").assertIsDisplayed()
        composeRule.onNodeWithText("I should inspect the current directory.").assertIsDisplayed()
        composeRule.onNodeWithText(
            "{\"command\":\"pwd\",\"workdir\":\"/workspace\"}",
        ).assertDoesNotExist()
        composeRule.onNodeWithText("Input").assertDoesNotExist()
        composeRule.onNodeWithText("Show details").assertDoesNotExist()
    }

    @Test
    fun streamingCursorStaysAttachedToPartialResponse() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-live",
                        userPrompt = null,
                        thinking = "",
                        statusText = "Working",
                        tools = emptyList(),
                        response = "Partial **answer**",
                        status = ConversationTurnStatus.STREAMING,
                    ),
                )
            }
        }

        composeRule.onNodeWithText("Partial answer ▍").assertIsDisplayed()
        composeRule.onNodeWithText("▍").assertDoesNotExist()
        composeRule.onNodeWithText("RESPONSE").assertIsDisplayed()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
    }

    @Test
    fun displayEventUsesHermesDiamondMarkerWithoutPromptGutter() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "event-1",
                        userPrompt = null,
                        thinking = "",
                        statusText = "",
                        tools = emptyList(),
                        response = "",
                        status = ConversationTurnStatus.COMPLETE,
                        eventText = "model changed",
                    ),
                )
            }
        }

        composeRule.onNodeWithText("◈ model changed").assertIsDisplayed()
        composeRule.onNodeWithText("❯ ").assertDoesNotExist()
        composeRule.onNodeWithText("┊ ").assertDoesNotExist()
    }

    @Test
    fun runningToolUsesLifecycleSemanticsAndVisibleInstrumentStatus() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-running",
                        userPrompt = null,
                        thinking = "",
                        statusText = "",
                        tools = listOf(
                            ConversationToolUiModel(
                                key = "tool-running",
                                toolId = "call-running",
                                name = "browser",
                                callLabel = "Browser(\"https://example.com\")",
                                status = ConversationToolStatus.RUNNING,
                            ),
                        ),
                        response = "",
                        status = ConversationTurnStatus.STREAMING,
                    ),
                )
            }
        }

        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithText("Running").assertIsDisplayed()
        composeRule.onNodeWithTag("tool:tool-running")
            .assertIsDisplayed()
            .assertHeightIsEqualTo(48.dp)
            .assertTextEquals("Browser(\"https://example.com\")", "Running")
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Running",
                ),
            )
            .assert(SemanticsMatcher.keyNotDefined(SemanticsProperties.ContentDescription))
        composeRule.onNodeWithContentDescription("Tool running").assertDoesNotExist()
    }

    @Test
    fun unknownToolUsesVisibleNeutralLifecycleAndAccessibilityState() {
        val unknown = ConversationToolStatus.entries.firstOrNull { it.name == "UNKNOWN" }
        assertNotNull("UNKNOWN tool lifecycle must be representable", unknown)

        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-unknown",
                        userPrompt = null,
                        thinking = "",
                        statusText = "",
                        tools = listOf(
                            ConversationToolUiModel(
                                key = "tool-unknown",
                                toolId = "call-unknown",
                                name = "terminal",
                                callLabel = "Terminal(\"detached process\")",
                                output = "Partial output remains visible.",
                                status = unknown!!,
                            ),
                        ),
                        response = "",
                        status = ConversationTurnStatus.INCOMPLETE,
                    ),
                )
            }
        }

        composeRule.onNodeWithTag("tool:tool-unknown")
            .assertIsDisplayed()
            .assertTextEquals("Terminal(\"detached process\")", "Unknown")
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Unknown",
                ),
            )
        composeRule.onNodeWithText("Partial output remains visible.").assertIsDisplayed()
    }

    @Test
    fun failedToolUsesHermesErrorSubtreeAndVisibleInstrumentStatus() {
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-error",
                        userPrompt = null,
                        thinking = "",
                        statusText = "",
                        tools = listOf(
                            ConversationToolUiModel(
                                key = "tool-error",
                                toolId = "call-error",
                                name = "terminal",
                                callLabel = "Terminal(\"restricted command\")",
                                durationSeconds = 12.4,
                                status = ConversationToolStatus.ERROR,
                                error = "Permission denied",
                            ),
                        ),
                        response = "",
                        status = ConversationTurnStatus.ERROR,
                    ),
                )
            }
        }

        composeRule.onNodeWithTag("tool:tool-error")
            .assertIsDisplayed()
            .assertHeightIsEqualTo(48.dp)
            .assertTextEquals("Terminal(\"restricted command\") (12s)", "Failed")
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Failed",
                ),
            )
            .assert(SemanticsMatcher.keyNotDefined(SemanticsProperties.ContentDescription))
        composeRule.onNodeWithContentDescription("Tool failed").assertDoesNotExist()
        composeRule.onNodeWithText("Permission denied").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Collapse Error").assertIsDisplayed()
    }

    @Test
    fun longToolOutputOffersBoundedPreviewFullViewAndCopy() {
        val output = (1..40).joinToString("\n") { "output line $it" }
        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = ConversationTurnUiModel(
                        key = "turn-long-tool-output",
                        userPrompt = null,
                        thinking = "",
                        statusText = "",
                        tools = listOf(
                            ConversationToolUiModel(
                                key = "tool-long-output",
                                toolId = "tool-long-output",
                                name = "terminal",
                                callLabel = "Terminal(\"long command\")",
                                output = output,
                                status = ConversationToolStatus.COMPLETE,
                            ),
                        ),
                        response = "",
                        status = ConversationTurnStatus.COMPLETE,
                    ),
                )
            }
        }

        composeRule.onNodeWithText("output line 1\noutput line 2", substring = true).assertExists()
        composeRule.onNodeWithText("Copy output").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Copy output").assertIsDisplayed()
        composeRule.onNodeWithText("Show full output").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Show full output").performClick()
        composeRule.onNodeWithText(output, substring = true).assertExists()
        composeRule.onNodeWithTag("tool-detail-scroll:tool-long-output:result").assertIsDisplayed()
    }

    @Test
    fun userCollapsedToolsStayCollapsedAcrossStreamingUpdatesAndReappearance() {
        val firstTool = ConversationToolUiModel(
            key = "tool-1",
            toolId = "call-1",
            name = "terminal",
            callLabel = "Terminal(\"pwd\")",
            argumentDetails = listOf(ConversationToolDetailUiModel("Command", "pwd")),
            status = ConversationToolStatus.COMPLETE,
        )
        var turn by mutableStateOf(
            ConversationTurnUiModel(
                key = "stable-turn",
                userPrompt = null,
                thinking = "",
                statusText = "",
                tools = listOf(firstTool),
                response = "",
                status = ConversationTurnStatus.STREAMING,
            ),
        )
        composeRule.setContent {
            MaterialTheme { ConversationTurnContent(turn = turn) }
        }

        composeRule.onNodeWithContentDescription("Tool calls (1)").performClick()
        composeRule.onNodeWithText("Args:\nCommand: pwd").assertDoesNotExist()

        composeRule.runOnIdle {
            turn = turn.copy(
                thinking = "Streaming text changed",
                tools = listOf(firstTool.copy(output = "partial output")),
            )
        }

        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithText("Result:\npartial output").assertDoesNotExist()

        composeRule.runOnIdle {
            turn = turn.copy(
                tools = turn.tools + firstTool.copy(
                    key = "tool-2",
                    toolId = "call-2",
                    callLabel = "Terminal(\"date\")",
                ),
            )
        }

        composeRule.onNodeWithContentDescription("Tool calls (2)").assertIsDisplayed()
        composeRule.onNodeWithText("Args:\nCommand: pwd").assertDoesNotExist()

        composeRule.runOnIdle {
            turn = turn.copy(tools = emptyList())
        }

        composeRule.onNodeWithText("Tool calls (2)").assertDoesNotExist()

        composeRule.runOnIdle {
            turn = turn.copy(tools = listOf(firstTool))
        }

        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithText("Args:\nCommand: pwd").assertDoesNotExist()
    }

    @Test
    fun processSectionsExpandButToolDetailsStayCollapsedWhenTheyAppear() {
        val tool = ConversationToolUiModel(
            key = "appearing-tool",
            toolId = "appearing-call",
            name = "terminal",
            callLabel = "Terminal(\"pwd\")",
            argumentDetails = listOf(ConversationToolDetailUiModel("Command", "pwd")),
            status = ConversationToolStatus.RUNNING,
        )
        var turn by mutableStateOf(
            ConversationTurnUiModel(
                key = "stable-pending-turn",
                userPrompt = null,
                thinking = "",
                statusText = "Working",
                tools = emptyList(),
                response = "",
                status = ConversationTurnStatus.STREAMING,
            ),
        )
        composeRule.setContent {
            MaterialTheme { ConversationTurnContent(turn = turn) }
        }

        composeRule.onNodeWithText("Thinking").assertDoesNotExist()
        composeRule.onNodeWithText("Tool calls (1)").assertDoesNotExist()
        composeRule.onNodeWithText("· Working").assertDoesNotExist()

        composeRule.runOnIdle {
            turn = turn.copy(
                thinking = "Inspecting the workspace",
                tools = listOf(tool),
            )
        }

        composeRule.onNodeWithContentDescription("Thinking")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Expanded",
                ),
            )
        composeRule.onNodeWithText("Inspecting the workspace ▍").assertIsDisplayed()
        composeRule.onNodeWithContentDescription("Tool calls (1)").assertIsDisplayed()
        composeRule.onNodeWithText("Command: pwd").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Expand Args").assertIsDisplayed()
    }

    @Test
    fun subagentsRenderAsAStableParentChildTree() {
        val childBeforeParent = subagent(
            key = "child-before-parent",
            goal = "Child discovered before parent",
            parentKey = "parent",
        )
        val independent = subagent(
            key = "independent",
            goal = "Independent root",
        )
        val parent = subagent(
            key = "parent",
            goal = "Parent coordinator",
        )
        val secondChild = subagent(
            key = "second-child",
            goal = "Second child",
            parentKey = "parent",
        )

        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = subagentTurn(
                        childBeforeParent,
                        independent,
                        parent,
                        secondChild,
                    ),
                )
            }
        }

        val independentBounds = composeRule.onNodeWithText(
            independent.goal,
            useUnmergedTree = true,
        )
            .fetchSemanticsNode().boundsInRoot
        val parentBounds = composeRule.onNodeWithText(
            parent.goal,
            useUnmergedTree = true,
        )
            .fetchSemanticsNode().boundsInRoot
        val firstChildBounds = composeRule.onNodeWithText(
            childBeforeParent.goal,
            useUnmergedTree = true,
        )
            .fetchSemanticsNode().boundsInRoot
        val secondChildBounds = composeRule.onNodeWithText(
            secondChild.goal,
            useUnmergedTree = true,
        )
            .fetchSemanticsNode().boundsInRoot

        assertTrue(independentBounds.top < parentBounds.top)
        assertTrue(parentBounds.top < firstChildBounds.top)
        assertTrue(firstChildBounds.top < secondChildBounds.top)
        assertTrue(firstChildBounds.left > parentBounds.left)
        assertEquals(firstChildBounds.left, secondChildBounds.left, 0.5f)
    }

    @Test
    fun subagentTreeIsCycleSafeAndClampsVisualDepth() {
        val cycleA = subagent("cycle-a", "Cycle A", parentKey = "cycle-b")
        val cycleB = subagent("cycle-b", "Cycle B", parentKey = "cycle-a")
        val chain = (0..11).map { index ->
            subagent(
                key = "chain-$index",
                goal = "Chain $index",
                parentKey = if (index == 0) null else "chain-${index - 1}",
            )
        }

        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = subagentTurn(*(chain + cycleA + cycleB).toTypedArray()),
                )
            }
        }

        val chainLeftPositions = chain.map { item ->
            composeRule.onNodeWithText(item.goal, useUnmergedTree = true)
                .assertIsDisplayed()
                .fetchSemanticsNode().boundsInRoot.left
        }
        val cycleALeft = composeRule.onNodeWithText(cycleA.goal, useUnmergedTree = true)
            .assertIsDisplayed()
            .fetchSemanticsNode().boundsInRoot.left
        val cycleBLeft = composeRule.onNodeWithText(cycleB.goal, useUnmergedTree = true)
            .assertIsDisplayed()
            .fetchSemanticsNode().boundsInRoot.left

        assertEquals(9, chainLeftPositions.distinct().size)
        assertTrue(cycleBLeft > cycleALeft)
    }

    @Test
    fun subagentTreeBoundsVisibleNodeCountAndReportsOmittedWork() {
        val subagents = (0 until 132).map { index ->
            subagent(
                key = "bounded-$index",
                goal = "Bounded subagent $index",
            )
        }

        composeRule.setContent {
            MaterialTheme {
                ConversationTurnContent(
                    turn = subagentTurn(*subagents.toTypedArray()),
                )
            }
        }

        composeRule.onNodeWithTag("subagent:bounded-127").assertExists()
        composeRule.onNodeWithTag("subagent:bounded-128").assertDoesNotExist()
        composeRule.onNodeWithText("4 more subagents not shown").assertExists()
    }

    private fun subagentTurn(
        vararg subagents: HermesConversationSubagent,
    ): ConversationTurnUiModel = ConversationTurnUiModel(
        key = "turn-subagent-tree",
        userPrompt = null,
        thinking = "",
        statusText = "",
        tools = emptyList(),
        response = "",
        status = ConversationTurnStatus.STREAMING,
        sections = CanonicalConversationSections.of(
            HermesConversationSection.Subagents(
                metadata = HermesConversationSectionMetadata(
                    key = "turn-subagent-tree:subagents",
                    status = HermesConversationSectionStatus.RUNNING,
                ),
                subagents = subagents.toList(),
                moaReferences = emptyList(),
            ),
        ),
    )

    private fun subagent(
        key: String,
        goal: String,
        parentKey: String? = null,
    ): HermesConversationSubagent = HermesConversationSubagent(
        key = key,
        goal = goal,
        status = HermesConversationSectionStatus.RUNNING,
        parentKey = parentKey,
    )
}
