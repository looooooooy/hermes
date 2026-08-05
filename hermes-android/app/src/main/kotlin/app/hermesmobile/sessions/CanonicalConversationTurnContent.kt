package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import app.hermesmobile.R
import java.util.Locale
import kotlin.math.roundToInt

@Composable
internal fun HermesCanonicalConversationTurnContent(
    turn: ConversationTurnUiModel,
    modifier: Modifier = Modifier,
    disclosureState: ConversationDisclosureState? = null,
    onDisclosureToggle: ((ConversationDisclosureStateKey) -> Unit)? = null,
) {
    val sections = turn.renderSections()
    val policy = remember { ConversationDisclosurePolicy() }
    var localDisclosureState by remember(turn.key) { mutableStateOf(ConversationDisclosureState()) }
    val currentDisclosureState = disclosureState ?: localDisclosureState
    val lastProcessIndex = sections.indexOfLast { it.isDisclosureProcessSection() }

    ProvideHermesTranscriptDesignSystem {
        val metrics = HermesTranscriptThemeTokens.metrics
        Column(
            modifier = modifier.fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(metrics.sectionGap),
        ) {
            sections.forEachIndexed { index, section ->
                key(section.renderIdentityKey()) {
                    HermesCanonicalSection(
                        turnKey = turn.key,
                        section = section,
                        processBranchLast = index == lastProcessIndex,
                        disclosureMode = section.disclosureSection()?.let { disclosureSection ->
                            currentDisclosureState.mode(
                                ConversationDisclosureStateKey(turn.key, disclosureSection),
                                policy,
                            )
                        },
                        onDisclosureToggle = section.disclosureSection()?.let { disclosureSection ->
                            {
                                val stateKey = ConversationDisclosureStateKey(turn.key, disclosureSection)
                                if (onDisclosureToggle == null) {
                                    localDisclosureState = localDisclosureState.toggled(stateKey, policy)
                                } else {
                                    onDisclosureToggle(stateKey)
                                }
                            }
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun HermesCanonicalSection(
    turnKey: String,
    section: HermesConversationSection,
    processBranchLast: Boolean,
    disclosureMode: ConversationDisclosureMode?,
    onDisclosureToggle: (() -> Unit)?,
) {
    val modifier = Modifier.testTag("conversation-section:${section.key}")
    when (section) {
        is HermesConversationSection.UserPrompt -> HermesTranscriptPrompt(
            text = section.prompt.text,
            modifier = modifier,
        )

        is HermesConversationSection.Todo -> HermesTodoSection(
            section = section,
            mode = requireNotNull(disclosureMode),
            onToggle = requireNotNull(onDisclosureToggle),
            branchLast = processBranchLast,
            modifier = modifier,
        )

        is HermesConversationSection.Thinking -> HermesThinkingSection(
            section = section,
            mode = requireNotNull(disclosureMode),
            onToggle = requireNotNull(onDisclosureToggle),
            branchLast = processBranchLast,
            modifier = modifier,
        )

        is HermesConversationSection.ToolGroup -> HermesToolGroupSection(
            section = section,
            mode = requireNotNull(disclosureMode),
            onToggle = requireNotNull(onDisclosureToggle),
            branchLast = processBranchLast,
            modifier = modifier,
        )

        is HermesConversationSection.Subagents -> HermesSubagentsSection(
            section = section,
            mode = requireNotNull(disclosureMode),
            onToggle = requireNotNull(onDisclosureToggle),
            branchLast = processBranchLast,
            modifier = modifier,
        )

        is HermesConversationSection.Activity -> HermesActivitySection(
            section = section,
            mode = requireNotNull(disclosureMode),
            modifier = modifier,
        )

        is HermesConversationSection.ResponseBoundary -> HermesTranscriptResponseBoundary(
            modifier = modifier,
            label = stringResource(R.string.response_label),
        )

        is HermesConversationSection.AssistantResponse -> HermesAssistantResponseSection(
            section = section,
            modifier = modifier,
        )

        is HermesConversationSection.Event -> HermesEventSection(section, modifier)
        is HermesConversationSection.Diff -> HermesDiffSection(section, modifier)
        is HermesConversationSection.Error -> HermesErrorSection(section, modifier)
        is HermesConversationSection.TokenSummary -> HermesTokenSummarySection(section, modifier)
        is HermesConversationSection.PendingInput -> HermesPendingInputSection(section, modifier)
    }
}

@Composable
private fun HermesTodoSection(
    section: HermesConversationSection.Todo,
    mode: ConversationDisclosureMode,
    onToggle: () -> Unit,
    branchLast: Boolean,
    modifier: Modifier,
) {
    HermesProcessDisclosure(
        title = stringResource(R.string.todo_count, section.items.size),
        sectionStatus = section.status,
        mode = mode,
        onToggle = onToggle,
        branchLast = branchLast,
        modifier = modifier,
    ) { childRails ->
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            section.items.forEachIndexed { index, item ->
                val presentation = item.status.toLongRunningItemPresentation()
                val statusLabel = stringResource(presentation.labelResource)
                HermesProcessLeaf(
                    label = item.content,
                    status = presentation.status,
                    stateDescription = statusLabel,
                    trailingLabel = statusLabel,
                    ancestorContinuations = childRails,
                    branchLast = index == section.items.lastIndex,
                    modifier = Modifier.testTag("todo:${item.key}"),
                )
            }
        }
    }
}

@Composable
private fun HermesThinkingSection(
    section: HermesConversationSection.Thinking,
    mode: ConversationDisclosureMode,
    onToggle: () -> Unit,
    branchLast: Boolean,
    modifier: Modifier,
) {
    val presentation = remember(section.text, section.status) {
        presentHermesThinking(section)
    }
    HermesProcessDisclosure(
        title = stringResource(R.string.thinking_label),
        sectionStatus = section.status,
        mode = mode,
        onToggle = onToggle,
        branchLast = branchLast,
        modifier = modifier,
    ) { _ ->
        HermesMarkdownContent(
            markdown = presentation.markdown,
            streaming = presentation.streaming,
        )
    }
}

internal data class HermesThinkingPresentation(
    val markdown: String,
    val streaming: Boolean,
)

internal fun presentHermesThinking(
    section: HermesConversationSection.Thinking,
): HermesThinkingPresentation = HermesThinkingPresentation(
    markdown = section.text,
    streaming = section.status == HermesConversationSectionStatus.STREAMING,
)

@Composable
private fun HermesToolGroupSection(
    section: HermesConversationSection.ToolGroup,
    mode: ConversationDisclosureMode,
    onToggle: () -> Unit,
    branchLast: Boolean,
    modifier: Modifier,
) {
    HermesProcessDisclosure(
        title = stringResource(R.string.tool_calls_count, section.tools.size),
        sectionStatus = section.status,
        mode = mode,
        onToggle = onToggle,
        branchLast = branchLast,
        modifier = modifier,
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            section.tools.forEach { tool ->
                HermesCanonicalTool(tool = tool)
            }
        }
    }
}

@Composable
private fun HermesCanonicalTool(
    tool: ConversationToolUiModel,
) {
    val call = HermesMessagePresentation.toolCall(
        name = tool.name,
        arguments = tool.arguments,
        context = tool.context,
    )
    val callLabel = tool.callLabel?.takeIf(String::isNotBlank) ?: call.label
    val duration = tool.durationSeconds?.let(::formatCanonicalDuration)
    val label = listOfNotNull(callLabel, duration?.let { "($it)" }).joinToString(" ")
    val accessibility = tool.status.toTranscriptToolAccessibility()
    val lifecycleDescription = stringResource(accessibility.stateDescriptionResource)
    val argumentPayload = HermesMessagePresentation.payloadText(tool.arguments)
    val argumentDetails = tool.argumentDetails
        .ifEmpty { call.details }
        .ifEmpty { argumentPayload.details }
    val argumentText = if (argumentDetails.isNotEmpty()) {
        argumentDetails.joinToString("\n") { "${it.label}: ${it.value}" }
    } else {
        argumentPayload.text.trimEnd()
    }
    val resultPayloads = listOf(tool.output, tool.result, tool.summary, tool.diff)
        .filterNotNull()
        .filter(String::isNotBlank)
        .map(HermesMessagePresentation::payloadText)
    val resultText = (
        resultPayloads.map(HermesReadablePayload::text) +
            (tool.resultDetails + resultPayloads.flatMap(HermesReadablePayload::details))
                .distinct()
                .map { "${it.label}: ${it.value}" }
        )
        .map(String::trimEnd)
        .filter(String::isNotBlank)
        .distinct()
        .joinToString("\n")
    val errorText = HermesMessagePresentation.payloadText(tool.error).visibleText().trimEnd()

    Column(
        verticalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        HermesToolInstrumentHeader(
            label = label,
            status = accessibility.status,
            stateDescription = lifecycleDescription,
            modifier = Modifier.testTag("tool:${tool.key}"),
        )
        if (argumentText.isNotBlank()) {
            HermesToolDetail(
                detailKey = "${tool.key}:arguments",
                title = stringResource(R.string.tool_arguments),
                body = argumentText,
                isError = false,
                initiallyExpanded = false,
            )
        }
        val visibleResult = if (tool.status == ConversationToolStatus.ERROR) {
            listOf(resultText, errorText).filter(String::isNotBlank).distinct().joinToString("\n")
        } else {
            resultText
        }
        if (visibleResult.isNotBlank()) {
            HermesToolDetail(
                detailKey = "${tool.key}:result",
                title = if (tool.status == ConversationToolStatus.ERROR) {
                    stringResource(R.string.tool_error_detail)
                } else {
                    stringResource(R.string.tool_result)
                },
                body = visibleResult,
                isError = tool.status == ConversationToolStatus.ERROR,
            )
        }
    }
}

@Composable
private fun HermesToolInstrumentHeader(
    label: String,
    status: HermesTranscriptStatus,
    stateDescription: String,
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    val tone = colors.statusColors(status)
    val presentation = remember(label, stateDescription) {
        presentHermesToolHeader(label, stateDescription)
    }
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .height(metrics.minimumTouchTarget)
            .semantics(mergeDescendants = true) {
                this.stateDescription = presentation.stateDescription
            },
        shape = MaterialTheme.shapes.small,
        color = colors.statusBackground,
        border = BorderStroke(1.dp, colors.borderRail.copy(alpha = 0.72f)),
        tonalElevation = 0.dp,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            HermesTranscriptStatusDot(
                status = status,
                color = tone.foreground,
            )
            Text(
                text = label,
                modifier = Modifier.weight(1f),
                style = HermesTranscriptThemeTokens.typography.process,
                color = colors.tool,
                maxLines = presentation.maxLines,
                overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis,
            )
            Text(
                text = presentation.visibleTexts.last(),
                style = HermesTranscriptThemeTokens.typography.meta,
                color = tone.foreground,
                maxLines = 1,
            )
        }
    }
}

internal data class HermesToolHeaderPresentation(
    val visibleTexts: List<String>,
    val stateDescription: String,
    val maxLines: Int,
)

internal fun presentHermesToolHeader(
    label: String,
    lifecycleDescription: String,
): HermesToolHeaderPresentation = HermesToolHeaderPresentation(
    visibleTexts = listOf(label, lifecycleDescription),
    stateDescription = lifecycleDescription,
    maxLines = 1,
)

@Composable
private fun HermesSubagentsSection(
    section: HermesConversationSection.Subagents,
    mode: ConversationDisclosureMode,
    onToggle: () -> Unit,
    branchLast: Boolean,
    modifier: Modifier,
) {
    HermesProcessDisclosure(
        title = stringResource(R.string.subagents_count, section.subagents.size),
        sectionStatus = section.status,
        mode = mode,
        onToggle = onToggle,
        branchLast = branchLast,
        modifier = modifier,
    ) { childRails ->
        val hasMoaContent = section.moaReferences.isNotEmpty() || section.moaProgress != null
        val tree = remember(section.subagents, hasMoaContent) {
            presentHermesSubagentTree(
                subagents = section.subagents,
                hasTrailingRootContent = hasMoaContent,
            )
        }
        val trailingLeafCount = (if (tree.omittedCount > 0) 1 else 0) +
            section.moaReferences.size +
            if (section.moaProgress == null) 0 else 1
        var trailingLeafIndex = 0
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            tree.nodes.forEach { node ->
                val subagent = node.subagent
                val presentation = subagent.status.toLongRunningItemPresentation()
                val statusLabel = stringResource(presentation.labelResource)
                HermesProcessLeaf(
                    label = subagent.goal.ifBlank { "Subagent" },
                    status = presentation.status,
                    stateDescription = statusLabel,
                    trailingLabel = statusLabel,
                    ancestorContinuations = childRails + node.ancestorContinuations,
                    branchLast = node.branchLast,
                    modifier = Modifier.testTag("subagent:${subagent.key}"),
                )
                val details = buildList {
                    subagent.model?.takeIf(String::isNotBlank)?.let { add(it) }
                    subagent.summary?.takeIf(String::isNotBlank)?.let { add(it) }
                    subagent.taskIndex?.let { index ->
                        val display = index + 1
                        add(if (subagent.taskCount == null) "Task $display" else "Task $display/${subagent.taskCount}")
                    }
                    subagent.durationSeconds?.let { add(formatCanonicalDuration(it)) }
                    subagent.tokenSummary?.let(::formatTokenSummary)?.takeIf(String::isNotBlank)?.let(::add)
                    subagent.apiCalls?.let { add("$it API calls") }
                }
                if (details.isNotEmpty()) {
                    HermesProcessText(
                        text = details.joinToString(" · "),
                        ancestorContinuations = childRails +
                            node.ancestorContinuations +
                            !node.branchLast,
                        branchLast = true,
                    )
                }
            }
            if (tree.omittedCount > 0) {
                trailingLeafIndex += 1
                val label = stringResource(R.string.subagents_omitted, tree.omittedCount)
                HermesProcessLeaf(
                    label = label,
                    status = HermesTranscriptStatus.Unknown,
                    stateDescription = label,
                    ancestorContinuations = childRails,
                    branchLast = trailingLeafIndex == trailingLeafCount,
                )
            }
            section.moaReferences.forEach { reference ->
                trailingLeafIndex += 1
                Column {
                    HermesProcessLeaf(
                        label = reference.label.ifBlank { stringResource(R.string.moa_reference) },
                        status = HermesTranscriptStatus.Complete,
                        stateDescription = stringResource(R.string.moa_reference),
                        ancestorContinuations = childRails,
                        branchLast = trailingLeafIndex == trailingLeafCount,
                    )
                    HermesProcessText(
                        text = reference.text,
                        ancestorContinuations = childRails +
                            (trailingLeafIndex < trailingLeafCount),
                        branchLast = true,
                    )
                }
            }
            section.moaProgress?.let { progress ->
                trailingLeafIndex += 1
                val progressText = buildList {
                    progress.phase?.takeIf(String::isNotBlank)?.let(::add)
                    progress.aggregator?.takeIf(String::isNotBlank)?.let(::add)
                    if (progress.refsDone != null || progress.refsTotal != null) {
                        add("${progress.refsDone ?: 0}/${progress.refsTotal ?: "?"} references")
                    }
                }.joinToString(" · ")
                HermesProcessLeaf(
                    label = progressText.ifBlank { stringResource(R.string.moa_progress) },
                    status = section.status.toTranscriptStatus(),
                    stateDescription = stringResource(R.string.moa_progress),
                    ancestorContinuations = childRails,
                    branchLast = trailingLeafIndex == trailingLeafCount,
                )
            }
        }
    }
}

@Composable
private fun HermesActivitySection(
    section: HermesConversationSection.Activity,
    mode: ConversationDisclosureMode,
    modifier: Modifier,
) {
    if (mode == ConversationDisclosureMode.HIDDEN && section.tone == HermesConversationActivityTone.INFO) {
        return
    }
    val colors = HermesTranscriptThemeTokens.colors
    val activityLabel = stringResource(R.string.activity_label)
    val color = when (section.tone) {
        HermesConversationActivityTone.INFO -> colors.muted
        HermesConversationActivityTone.WARNING -> colors.warning
        HermesConversationActivityTone.ERROR -> colors.error
    }
    Text(
        text = section.text,
        modifier = modifier
            .fillMaxWidth()
            .semantics { contentDescription = activityLabel },
        style = HermesTranscriptThemeTokens.typography.meta,
        color = color,
    )
}

@Composable
private fun HermesAssistantResponseSection(
    section: HermesConversationSection.AssistantResponse,
    modifier: Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(IntrinsicSize.Min),
        verticalAlignment = Alignment.Top,
    ) {
        HermesTranscriptResponseSpine()
        HermesMarkdownContent(
            markdown = section.text,
            modifier = Modifier.weight(1f),
            streaming = section.status == HermesConversationSectionStatus.STREAMING,
        )
    }
}

@Composable
private fun HermesEventSection(
    section: HermesConversationSection.Event,
    modifier: Modifier,
) {
    Text(
        text = "◈ ${section.text}",
        modifier = modifier.fillMaxWidth(),
        style = HermesTranscriptThemeTokens.typography.meta,
        fontStyle = FontStyle.Italic,
        color = HermesTranscriptThemeTokens.colors.muted,
    )
}

@Composable
private fun HermesDiffSection(
    section: HermesConversationSection.Diff,
    modifier: Modifier,
) {
    var expanded by remember(section.key) {
        mutableStateOf(HermesPreformattedContentKind.DIFF.defaultExpanded)
    }
    val colors = HermesTranscriptThemeTokens.colors
    val annotatedDiff = remember(
        section.text,
        colors.success,
        colors.error,
        colors.accent,
        colors.muted,
    ) {
        buildHermesDiffAnnotatedString(
            text = section.text,
            additionColor = colors.success,
            deletionColor = colors.error,
            hunkColor = colors.accent,
            fileHeaderColor = colors.muted,
        )
    }
    HermesPreformattedContent(
        source = section.text,
        annotatedSource = annotatedDiff,
        kind = HermesPreformattedContentKind.DIFF,
        modifier = modifier,
        label = stringResource(R.string.diff_label),
        copyLabel = stringResource(R.string.copy_output),
        copyButtonTag = "copy-diff:${section.key}",
        scrollTag = "diff-scroll:${section.key}",
        expanded = expanded,
        onExpandedChange = { expanded = it },
        expandContentDescription = "Expand Diff",
        collapseContentDescription = "Collapse Diff",
        textColor = colors.text,
        containerColor = colors.statusBackground,
    )
}

@Composable
private fun HermesErrorSection(
    section: HermesConversationSection.Error,
    modifier: Modifier,
) {
    Text(
        text = section.message,
        modifier = modifier
            .fillMaxWidth()
            .semantics { contentDescription = "Error" },
        style = HermesTranscriptThemeTokens.typography.process,
        color = HermesTranscriptThemeTokens.colors.error,
    )
}

@Composable
private fun HermesTokenSummarySection(
    section: HermesConversationSection.TokenSummary,
    modifier: Modifier,
) {
    Text(
        text = listOf(
            stringResource(R.string.token_summary_label),
            formatTokenSummary(section.summary),
        ).filter(String::isNotBlank).joinToString(" · "),
        modifier = modifier.fillMaxWidth(),
        style = HermesTranscriptThemeTokens.typography.meta,
        color = HermesTranscriptThemeTokens.colors.muted,
    )
}

@Composable
private fun HermesPendingInputSection(
    section: HermesConversationSection.PendingInput,
    modifier: Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val inputRequired = stringResource(R.string.input_required)
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .semantics { contentDescription = inputRequired },
        shape = MaterialTheme.shapes.medium,
        color = colors.statusBackground,
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text(
                text = inputRequired,
                style = HermesTranscriptThemeTokens.typography.meta,
                fontWeight = FontWeight.SemiBold,
                color = colors.accent,
            )
            Text(
                text = section.input.prompt,
                style = HermesTranscriptThemeTokens.typography.body,
                color = colors.text,
            )
            section.input.choices.forEach { choice ->
                Text(
                    text = choice,
                    style = HermesTranscriptThemeTokens.typography.process,
                    color = colors.muted,
                )
            }
        }
    }
}

@Composable
private fun HermesProcessDisclosure(
    title: String,
    sectionStatus: HermesConversationSectionStatus,
    mode: ConversationDisclosureMode,
    onToggle: () -> Unit,
    branchLast: Boolean,
    modifier: Modifier,
    content: @Composable (List<Boolean>) -> Unit,
) {
    if (mode == ConversationDisclosureMode.HIDDEN) return
    val expanded = mode == ConversationDisclosureMode.EXPANDED
    Column(modifier = modifier.fillMaxWidth()) {
        HermesTranscriptDisclosureHeader(
            title = title,
            expanded = expanded,
            onExpandedChange = { onToggle() },
            status = sectionStatus.toTranscriptStatus(),
            branchLast = branchLast,
        )
        if (expanded) {
            content(listOf(!branchLast))
        }
    }
}

@Composable
private fun HermesProcessLeaf(
    label: String,
    status: HermesTranscriptStatus,
    stateDescription: String,
    trailingLabel: String? = null,
    ancestorContinuations: List<Boolean>,
    branchLast: Boolean,
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val tone = colors.statusColors(status)
    val accessibilityStateDescription = stateDescription
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(IntrinsicSize.Min)
            .semantics(mergeDescendants = true) {
                this.stateDescription = accessibilityStateDescription
            },
        verticalAlignment = Alignment.Top,
    ) {
        HermesTranscriptProcessRail(
            ancestorContinuations = ancestorContinuations,
            branchLast = branchLast,
            nodeStatus = status,
        )
        Spacer(Modifier.width(6.dp))
        Text(
            text = label,
            modifier = Modifier.weight(1f),
            style = HermesTranscriptThemeTokens.typography.process,
            color = tone.foreground,
        )
        trailingLabel?.let { trailing ->
            Spacer(Modifier.width(8.dp))
            Text(
                text = trailing,
                style = HermesTranscriptThemeTokens.typography.meta,
                color = tone.foreground,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun HermesProcessText(
    text: String,
    ancestorContinuations: List<Boolean>,
    branchLast: Boolean,
    showProcessRail: Boolean = true,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .height(IntrinsicSize.Min),
        verticalAlignment = Alignment.Top,
    ) {
        if (showProcessRail) {
            HermesTranscriptProcessRail(
                ancestorContinuations = ancestorContinuations,
                branchLast = branchLast,
                showNode = false,
            )
        }
        SelectionContainer(modifier = Modifier.weight(1f)) {
            Text(
                text = text,
                style = HermesTranscriptThemeTokens.typography.process,
                color = HermesTranscriptThemeTokens.colors.muted,
            )
        }
    }
}

@Composable
private fun HermesToolDetail(
    detailKey: String,
    title: String,
    body: String,
    isError: Boolean,
    initiallyExpanded: Boolean = HermesPreformattedContentKind.TOOL_OUTPUT.defaultExpanded,
) {
    var detailExpanded by remember(detailKey) {
        mutableStateOf(initiallyExpanded)
    }
    var showFull by remember(detailKey) { mutableStateOf(false) }
    val presentation = remember(body, showFull) {
        toolDetailPresentation(body = body, expanded = showFull)
    }
    HermesPreformattedContent(
        source = presentation.visibleBody,
        copySource = body,
        kind = HermesPreformattedContentKind.TOOL_OUTPUT,
        label = title,
        copyLabel = stringResource(R.string.copy_output),
        copyButtonTag = "tool-detail-copy:$detailKey",
        scrollTag = "tool-detail-scroll:$detailKey",
        expanded = detailExpanded,
        onExpandedChange = { detailExpanded = it },
        expandContentDescription = "Expand $title",
        collapseContentDescription = "Collapse $title",
        extraActionContentDescription = if (detailExpanded && presentation.canExpand) {
            if (showFull) stringResource(R.string.collapse_output) else stringResource(R.string.show_full_output)
        } else {
            null
        },
        extraActionTag = "tool-detail-toggle:$detailKey",
        onExtraAction = if (detailExpanded && presentation.canExpand) {
            { showFull = !showFull }
        } else {
            null
        },
        textColor = if (isError) {
            HermesTranscriptThemeTokens.colors.error
        } else {
            HermesTranscriptThemeTokens.colors.tool
        },
    )
}

private fun HermesConversationSection.isDisclosureProcessSection(): Boolean = when (kind) {
    HermesConversationSectionKind.TODO,
    HermesConversationSectionKind.THINKING,
    HermesConversationSectionKind.TOOL_GROUP,
    HermesConversationSectionKind.SUBAGENTS,
    -> true

    else -> false
}

private fun HermesConversationSection.disclosureSection(): ConversationDisclosureSection? = when (kind) {
    HermesConversationSectionKind.TODO -> ConversationDisclosureSection.TODO
    HermesConversationSectionKind.THINKING -> ConversationDisclosureSection.THINKING
    HermesConversationSectionKind.TOOL_GROUP -> ConversationDisclosureSection.TOOLS
    HermesConversationSectionKind.SUBAGENTS -> ConversationDisclosureSection.SUBAGENTS
    HermesConversationSectionKind.ACTIVITY -> ConversationDisclosureSection.ACTIVITY
    else -> null
}

private fun HermesConversationSectionStatus.toTranscriptStatus(): HermesTranscriptStatus = when (this) {
    HermesConversationSectionStatus.PENDING -> HermesTranscriptStatus.Pending
    HermesConversationSectionStatus.RUNNING,
    HermesConversationSectionStatus.STREAMING,
    -> HermesTranscriptStatus.Running

    HermesConversationSectionStatus.COMPLETE -> HermesTranscriptStatus.Complete
    HermesConversationSectionStatus.INTERRUPTED -> HermesTranscriptStatus.Interrupted
    HermesConversationSectionStatus.ERROR -> HermesTranscriptStatus.Error
}

internal data class HermesLongRunningItemPresentation(
    val status: HermesTranscriptStatus,
    val labelResource: Int,
)

internal fun HermesConversationTodoStatus.toLongRunningItemPresentation():
    HermesLongRunningItemPresentation = when (this) {
    HermesConversationTodoStatus.PENDING -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Pending,
        R.string.long_running_waiting,
    )
    HermesConversationTodoStatus.IN_PROGRESS -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Running,
        R.string.long_running_running,
    )
    HermesConversationTodoStatus.COMPLETED -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Complete,
        R.string.long_running_complete,
    )
    HermesConversationTodoStatus.CANCELLED -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Interrupted,
        R.string.long_running_cancelled,
    )
}

internal fun HermesConversationSectionStatus.toLongRunningItemPresentation():
    HermesLongRunningItemPresentation = when (this) {
    HermesConversationSectionStatus.PENDING -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Pending,
        R.string.long_running_waiting,
    )
    HermesConversationSectionStatus.RUNNING,
    HermesConversationSectionStatus.STREAMING,
    -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Running,
        R.string.long_running_running,
    )
    HermesConversationSectionStatus.COMPLETE -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Complete,
        R.string.long_running_complete,
    )
    HermesConversationSectionStatus.INTERRUPTED -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Interrupted,
        R.string.long_running_interrupted,
    )
    HermesConversationSectionStatus.ERROR -> HermesLongRunningItemPresentation(
        HermesTranscriptStatus.Error,
        R.string.long_running_failed,
    )
}

internal data class HermesToolAccessibility(
    val status: HermesTranscriptStatus,
    val stateDescriptionResource: Int,
)

internal fun ConversationToolStatus.toTranscriptToolAccessibility(): HermesToolAccessibility = when (this) {
    ConversationToolStatus.RUNNING -> HermesToolAccessibility(
        status = HermesTranscriptStatus.Running,
        stateDescriptionResource = R.string.tool_running,
    )

    ConversationToolStatus.COMPLETE -> HermesToolAccessibility(
        status = HermesTranscriptStatus.Complete,
        stateDescriptionResource = R.string.tool_complete,
    )

    ConversationToolStatus.ERROR -> HermesToolAccessibility(
        status = HermesTranscriptStatus.Error,
        stateDescriptionResource = R.string.tool_failed,
    )

    ConversationToolStatus.INTERRUPTED -> HermesToolAccessibility(
        status = HermesTranscriptStatus.Interrupted,
        stateDescriptionResource = R.string.tool_interrupted,
    )

    ConversationToolStatus.UNKNOWN -> HermesToolAccessibility(
        status = HermesTranscriptStatus.Unknown,
        stateDescriptionResource = R.string.tool_unknown,
    )
}

private fun formatTokenSummary(summary: HermesConversationTokenSummary): String = buildList {
    summary.inputTokens?.let { add("Input $it") }
    summary.outputTokens?.let { add("Output $it") }
    summary.reasoningTokens?.let { add("Reasoning $it") }
    summary.toolTokens?.let { add("Tools $it") }
    summary.totalTokens?.let { add("Total $it") }
}.joinToString(" · ")

private fun formatCanonicalDuration(seconds: Double): String {
    val safe = seconds.coerceAtLeast(0.0)
    return if (safe < 10.0) {
        String.format(Locale.US, "%.1fs", safe)
    } else {
        "${safe.roundToInt()}s"
    }
}
