package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.hermesmobile.R

internal enum class LongRunningWorkKind {
    TODO,
    SUBAGENT,
}

internal data class LongRunningWorkItemPresentation(
    val turnKey: String,
    val sectionKey: ConversationDisclosureStateKey,
    val kind: LongRunningWorkKind,
    val progressNumerator: Int,
    val progressDenominator: Int,
    val currentLabel: String,
    val status: HermesTranscriptStatus,
)

internal data class LongRunningWorkPresentation(
    val items: List<LongRunningWorkItemPresentation>,
)

internal fun longRunningWorkPresentation(
    running: Boolean,
    turns: List<ConversationTurnUiModel>,
): LongRunningWorkPresentation? {
    if (!running) return null
    return turns.asReversed().firstNotNullOfOrNull { turn ->
        val sections = turn.renderSections()
        val todo = sections
            .filterIsInstance<HermesConversationSection.Todo>()
            .lastOrNull { section ->
                section.items.any { it.status == HermesConversationTodoStatus.IN_PROGRESS }
            }
        val subagents = sections
            .filterIsInstance<HermesConversationSection.Subagents>()
            .lastOrNull { section ->
                section.subagents.any { it.status.isActiveLongRunningStatus() }
            }
        val items = buildList {
            todo?.let { section ->
                val active = section.items.first {
                    it.status == HermesConversationTodoStatus.IN_PROGRESS
                }
                add(
                    LongRunningWorkItemPresentation(
                        turnKey = turn.key,
                        sectionKey = ConversationDisclosureStateKey(
                            turnKey = turn.key,
                            section = ConversationDisclosureSection.TODO,
                        ),
                        kind = LongRunningWorkKind.TODO,
                        progressNumerator = section.items.count {
                            it.status == HermesConversationTodoStatus.COMPLETED
                        },
                        progressDenominator = section.items.size,
                        currentLabel = active.content,
                        status = HermesTranscriptStatus.Running,
                    ),
                )
            }
            subagents?.let { section ->
                val activeIndex = section.subagents.indexOfFirst {
                    it.status.isActiveLongRunningStatus()
                }
                val active = section.subagents[activeIndex]
                add(
                    LongRunningWorkItemPresentation(
                        turnKey = turn.key,
                        sectionKey = ConversationDisclosureStateKey(
                            turnKey = turn.key,
                            section = ConversationDisclosureSection.SUBAGENTS,
                        ),
                        kind = LongRunningWorkKind.SUBAGENT,
                        progressNumerator = activeIndex + 1,
                        progressDenominator = section.subagents.size,
                        currentLabel = active.goal.ifBlank { "Subagent" },
                        status = active.status.toLongRunningDockStatus(),
                    ),
                )
            }
        }
        items.takeIf { it.isNotEmpty() }
            ?.let(::LongRunningWorkPresentation)
    }
}

@Composable
internal fun HermesLongRunningWorkDock(
    presentation: LongRunningWorkPresentation?,
    expanded: Boolean,
    selectedKind: LongRunningWorkKind?,
    onExpandedChange: (Boolean) -> Unit,
    onItemClick: (LongRunningWorkItemPresentation) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (presentation == null) return
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val expandedState = stringResource(
        if (expanded) R.string.long_running_expanded else R.string.long_running_collapsed,
    )
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag("long-running-dock"),
        shape = RoundedCornerShape(11.dp),
        color = colors.activeBackground,
        border = BorderStroke(1.dp, colors.borderRail),
        tonalElevation = 0.dp,
    ) {
        Column {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp)
                    .clickable { onExpandedChange(!expanded) }
                    .semantics(mergeDescendants = true) {
                        role = Role.Button
                        stateDescription = expandedState
                    }
                    .padding(horizontal = 10.dp)
                    .testTag("long-running-toggle"),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = stringResource(
                            R.string.long_running_title,
                            presentation.items.size,
                        ).uppercase(),
                        style = typography.meta,
                        color = colors.statusForeground,
                        fontWeight = FontWeight.Bold,
                        letterSpacing = 0.8.sp,
                    )
                    Text(
                        text = stringResource(R.string.long_running_summary),
                        style = typography.meta,
                        color = colors.muted,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                Text(
                    text = if (expanded) "⌄" else "›",
                    style = typography.process,
                    color = colors.muted,
                )
            }
            if (expanded) {
                HorizontalDivider(color = colors.borderRail)
                presentation.items.forEachIndexed { index, item ->
                    if (index > 0) HorizontalDivider(color = colors.borderRail.copy(alpha = 0.7f))
                    LongRunningWorkRow(
                        item = item,
                        selected = selectedKind == item.kind,
                        onClick = { onItemClick(item) },
                    )
                }
            }
        }
    }
}

@Composable
private fun LongRunningWorkRow(
    item: LongRunningWorkItemPresentation,
    selected: Boolean,
    onClick: () -> Unit,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val statusColor = when (item.kind) {
        LongRunningWorkKind.TODO -> colors.accent
        LongRunningWorkKind.SUBAGENT -> colors.warning
    }
    val statusLabel = stringResource(
        when (item.status) {
            HermesTranscriptStatus.Pending -> R.string.long_running_waiting
            HermesTranscriptStatus.Running -> if (item.kind == LongRunningWorkKind.TODO) {
                R.string.long_running_active
            } else {
                R.string.long_running_running
            }
            HermesTranscriptStatus.Complete -> R.string.long_running_complete
            HermesTranscriptStatus.Interrupted -> R.string.long_running_interrupted
            HermesTranscriptStatus.Error -> R.string.long_running_failed
            HermesTranscriptStatus.Idle,
            HermesTranscriptStatus.Warning,
            HermesTranscriptStatus.Critical,
            HermesTranscriptStatus.Unknown,
            -> R.string.long_running_running
        },
    )
    val progressLabel = stringResource(
        when (item.kind) {
            LongRunningWorkKind.TODO -> R.string.long_running_todo_progress
            LongRunningWorkKind.SUBAGENT -> R.string.long_running_subagent_progress
        },
        item.progressNumerator,
        item.progressDenominator,
    )
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = 48.dp)
            .background(if (selected) colors.statusBackground else colors.activeBackground)
            .clickable(onClick = onClick)
            .semantics(mergeDescendants = true) {
                role = Role.Button
                this.selected = selected
                stateDescription = statusLabel
                liveRegion = LiveRegionMode.Polite
            }
            .padding(horizontal = 10.dp, vertical = 5.dp)
            .testTag(
                when (item.kind) {
                    LongRunningWorkKind.TODO -> "long-running-todo"
                    LongRunningWorkKind.SUBAGENT -> "long-running-subagent"
                },
            ),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Surface(
            modifier = Modifier.size(7.dp),
            shape = when (item.kind) {
                LongRunningWorkKind.TODO -> CircleShape
                LongRunningWorkKind.SUBAGENT -> RoundedCornerShape(2.dp)
            },
            color = statusColor,
        ) {}
        Spacer(Modifier.width(8.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = progressLabel.uppercase(),
                style = typography.meta,
                color = colors.statusForeground,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
            )
            Text(
                text = item.currentLabel,
                style = typography.meta,
                color = colors.muted,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Spacer(Modifier.width(8.dp))
        Text(
            text = statusLabel.uppercase(),
            style = typography.meta,
            color = statusColor,
            fontWeight = FontWeight.Bold,
            maxLines = 1,
        )
    }
}

private fun HermesConversationSectionStatus.isActiveLongRunningStatus(): Boolean =
    this == HermesConversationSectionStatus.RUNNING ||
        this == HermesConversationSectionStatus.STREAMING

private fun HermesConversationSectionStatus.toLongRunningDockStatus(): HermesTranscriptStatus =
    when (this) {
        HermesConversationSectionStatus.PENDING -> HermesTranscriptStatus.Pending
        HermesConversationSectionStatus.RUNNING,
        HermesConversationSectionStatus.STREAMING,
        -> HermesTranscriptStatus.Running
        HermesConversationSectionStatus.COMPLETE -> HermesTranscriptStatus.Complete
        HermesConversationSectionStatus.INTERRUPTED -> HermesTranscriptStatus.Interrupted
        HermesConversationSectionStatus.ERROR -> HermesTranscriptStatus.Error
    }
