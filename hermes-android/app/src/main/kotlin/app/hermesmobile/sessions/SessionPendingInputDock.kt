package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.error
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.paneTitle
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.hermesmobile.R
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import app.hermesmobile.sessions.control.PendingInputInteractionState
import java.util.Locale

@Composable
internal fun HermesPendingInputDock(
    pendingInput: SessionPendingInput,
    interaction: PendingInputInteractionState,
    mutationEnabled: Boolean,
    onChoice: (String) -> Unit,
    onOtherChanged: (String) -> Unit,
    onSubmit: () -> Unit,
    onConfirm: () -> Unit,
    onCancelConfirmation: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val presentation = pendingInputDockPresentation(
        pendingInput = pendingInput,
        interaction = interaction,
        mutationEnabled = mutationEnabled,
    )
    val paneTitle = stringResource(presentation.mode.eyebrowResource())
    val accentColor = when (presentation.mode) {
        PendingInputDockMode.Restored -> colors.warning
        else -> colors.accent
    }
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(max = 400.dp)
            .semantics { this.paneTitle = paneTitle }
            .testTag("pending-input-dock"),
        shape = RoundedCornerShape(0.dp),
        color = colors.statusBackground,
        tonalElevation = 0.dp,
    ) {
        Column(
            modifier = Modifier
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                text = paneTitle.uppercase(Locale.ROOT),
                modifier = Modifier.semantics { heading() },
                style = typography.meta,
                color = accentColor,
                fontWeight = FontWeight.Bold,
                letterSpacing = 0.7.sp,
            )
            when (pendingInput) {
                is SessionPendingInput.Approval -> HermesApprovalPendingContent(
                    pendingInput = pendingInput,
                    interaction = interaction,
                    presentation = presentation,
                    onChoice = { choiceId ->
                        onChoice(choiceId)
                        val submitsImmediately = pendingInput.choices
                            .firstOrNull { it.wireValue == choiceId }
                            ?.let(::approvalChoicePresentation)
                            ?.submitsImmediately == true
                        if (submitsImmediately) {
                            onSubmit()
                        }
                    },
                    onConfirm = onConfirm,
                    onCancelConfirmation = onCancelConfirmation,
                )

                is SessionPendingInput.Clarify -> HermesClarifyPendingContent(
                    pendingInput = pendingInput,
                    interaction = interaction,
                    presentation = presentation,
                    onChoice = onChoice,
                    onOtherChanged = onOtherChanged,
                    onSubmit = onSubmit,
                )
            }
            HermesPendingInputFeedback(interaction.outcome)
            if (presentation.retryEnabled) {
                Button(
                    onClick = onSubmit,
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
                        .testTag("retry-pending-input"),
                    shape = RoundedCornerShape(8.dp),
                    contentPadding = PaddingValues(horizontal = 14.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = colors.accent,
                        contentColor = colors.background,
                    ),
                ) {
                    Text(
                        text = stringResource(R.string.pending_input_retry_same_request),
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
        }
    }
}

@Composable
private fun HermesApprovalPendingContent(
    pendingInput: SessionPendingInput.Approval,
    interaction: PendingInputInteractionState,
    presentation: PendingInputDockPresentation,
    onChoice: (String) -> Unit,
    onConfirm: () -> Unit,
    onCancelConfirmation: () -> Unit,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    Text(
        text = pendingInput.title,
        style = typography.body,
        color = colors.text,
        fontWeight = FontWeight.SemiBold,
    )
    pendingInput.description.takeIf(String::isNotBlank)?.let { description ->
        Text(description, style = typography.process, color = colors.muted)
    }
    pendingInput.command.takeIf(String::isNotBlank)?.let { command ->
        HermesPreformattedContent(
            source = command,
            kind = HermesPreformattedContentKind.COMMAND,
            scrollTag = "pending-command-scroll",
            textStyle = typography.code,
            textColor = colors.tool,
            containerColor = colors.background,
        )
    }
    if (presentation.showsChoices) {
        pendingInput.choices.forEach { choice ->
            val selected = interaction.selectedChoiceId == choice.wireValue
            val destructive = choice == SessionApprovalChoice.DENY
            HermesApprovalChoiceButton(
                choice = choice,
                selected = selected,
                destructive = destructive,
                enabled = presentation.editingEnabled,
                onClick = { onChoice(choice.wireValue) },
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
    if (presentation.showsConfirmation) {
        Surface(
            modifier = Modifier
                .fillMaxWidth()
                .testTag("pending-confirmation"),
            shape = RoundedCornerShape(0.dp),
            color = colors.warning.copy(alpha = 0.10f),
        ) {
            Column(
                modifier = Modifier.padding(10.dp),
                verticalArrangement = Arrangement.spacedBy(9.dp),
            ) {
                Text(
                    text = stringResource(R.string.approval_always_confirmation),
                    style = typography.process,
                    color = colors.warning,
                    fontWeight = FontWeight.SemiBold,
                )
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    OutlinedButton(
                        onClick = onCancelConfirmation,
                        enabled = presentation.editingEnabled,
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
                            .testTag("cancel-pending-confirmation"),
                        shape = RoundedCornerShape(8.dp),
                        contentPadding = PaddingValues(horizontal = 12.dp),
                    ) {
                        Text(stringResource(R.string.cancel))
                    }
                    Button(
                        onClick = onConfirm,
                        enabled = presentation.editingEnabled,
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
                            .testTag("confirm-pending-choice"),
                        shape = RoundedCornerShape(8.dp),
                        contentPadding = PaddingValues(horizontal = 12.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = colors.accent,
                            contentColor = colors.background,
                        ),
                    ) {
                        Text(stringResource(R.string.confirm), fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
}

@Composable
private fun HermesApprovalChoiceButton(
    choice: SessionApprovalChoice,
    selected: Boolean,
    destructive: Boolean,
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val contentColor = if (destructive) colors.error else colors.text
    val choicePresentation = approvalChoicePresentation(choice)
    val accessibilityState = if (choicePresentation.requiresConfirmation) {
        stringResource(R.string.approval_confirmation_required)
    } else {
        null
    }
    Row(
        modifier = modifier
            .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
            .background(
                color = if (selected) colors.accent.copy(alpha = 0.08f) else Color.Transparent,
            )
            .drawBehind {
                val strokeWidth = 1.dp.toPx()
                drawLine(
                    color = when {
                        selected -> colors.accent
                        destructive -> colors.error.copy(alpha = 0.55f)
                        else -> colors.borderRail.copy(alpha = 0.72f)
                    },
                    start = Offset(0f, size.height - strokeWidth / 2f),
                    end = Offset(size.width, size.height - strokeWidth / 2f),
                    strokeWidth = strokeWidth,
                )
            }
            .clickable(
                enabled = enabled,
                role = Role.Button,
                onClick = onClick,
            )
            .semantics {
                this.selected = selected
                accessibilityState?.let { this.stateDescription = it }
            }
            .testTag("pending-choice:${choice.wireValue}")
            .padding(horizontal = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = stringResource(choice.labelResource()),
            modifier = Modifier.weight(1f),
            style = HermesTranscriptThemeTokens.typography.process,
            color = if (enabled) contentColor else contentColor.copy(alpha = 0.45f),
        )
        HermesSelectionDot(selected = selected, enabled = enabled)
    }
}

@Composable
private fun HermesClarifyPendingContent(
    pendingInput: SessionPendingInput.Clarify,
    interaction: PendingInputInteractionState,
    presentation: PendingInputDockPresentation,
    onChoice: (String) -> Unit,
    onOtherChanged: (String) -> Unit,
    onSubmit: () -> Unit,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    Text(
        text = pendingInput.question,
        style = typography.body,
        color = colors.text,
        fontWeight = FontWeight.SemiBold,
    )
    Text(
        text = stringResource(R.string.clarify_exact_answer_hint),
        style = typography.process,
        color = colors.muted,
    )
    Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
        pendingInput.choices.forEach { choice ->
            val selected = interaction.selectedChoiceId == choice.id
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
                    .background(
                        color = if (selected) colors.accent.copy(alpha = 0.08f) else Color.Transparent,
                    )
                    .drawBehind {
                        val strokeWidth = 1.dp.toPx()
                        drawLine(
                            color = if (selected) colors.accent else colors.borderRail.copy(alpha = 0.72f),
                            start = Offset(0f, size.height - strokeWidth / 2f),
                            end = Offset(size.width, size.height - strokeWidth / 2f),
                            strokeWidth = strokeWidth,
                        )
                    }
                    .selectable(
                        selected = selected,
                        enabled = presentation.editingEnabled,
                        role = Role.RadioButton,
                        onClick = { onChoice(choice.id) },
                    )
                    .semantics { this.selected = selected }
                    .testTag("pending-choice:${choice.id}")
                    .padding(horizontal = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = choice.label,
                    modifier = Modifier.weight(1f),
                    style = typography.process,
                    color = colors.text,
                )
                HermesSelectionDot(
                    selected = selected,
                    enabled = presentation.editingEnabled,
                )
            }
        }
    }
    if (pendingInput.allowOther) {
        val otherInputLabel = stringResource(R.string.pending_other_input_label)
        Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text(
                text = stringResource(R.string.other_answer),
                style = typography.meta,
                color = colors.muted,
            )
            Surface(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(8.dp),
                color = colors.background,
                border = BorderStroke(1.dp, colors.borderRail.copy(alpha = 0.72f)),
            ) {
                BasicTextField(
                    value = interaction.otherDraft,
                    onValueChange = onOtherChanged,
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
                        .semantics { contentDescription = otherInputLabel }
                        .padding(horizontal = 12.dp, vertical = 12.dp)
                        .testTag("pending-other-input"),
                    enabled = presentation.editingEnabled,
                    maxLines = 4,
                    textStyle = MaterialTheme.typography.bodyMedium.copy(
                        color = if (presentation.editingEnabled) colors.text else colors.muted,
                    ),
                    cursorBrush = SolidColor(colors.accent),
                )
            }
        }
    }
    if (presentation.mode != PendingInputDockMode.Restored) {
        Button(
            onClick = onSubmit,
            enabled = presentation.submitEnabled,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = PENDING_INPUT_MIN_TOUCH_TARGET_DP.dp)
                .testTag("submit-pending-answer"),
            shape = RoundedCornerShape(8.dp),
            contentPadding = PaddingValues(horizontal = 14.dp),
            colors = ButtonDefaults.buttonColors(
                containerColor = colors.accent,
                contentColor = colors.background,
            ),
        ) {
            Text(
                text = stringResource(
                    if (interaction.inFlightClientRequestId == null) {
                        R.string.submit_answer
                    } else {
                        R.string.submitting_answer
                    },
                ),
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun HermesSelectionDot(
    selected: Boolean,
    enabled: Boolean,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val tone = if (enabled) colors.accent else colors.muted.copy(alpha = 0.55f)
    Surface(
        modifier = Modifier.size(16.dp),
        shape = RoundedCornerShape(50),
        color = Color.Transparent,
        border = BorderStroke(1.dp, if (selected) tone else colors.borderRail),
    ) {
        if (selected) {
            Surface(
                modifier = Modifier.padding(4.dp),
                shape = RoundedCornerShape(50),
                color = tone,
            ) {}
        }
    }
}


@Composable
private fun HermesPendingInputFeedback(outcome: PendingInputInteractionOutcome?) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val message = when (outcome) {
        is PendingInputInteractionOutcome.Failed -> outcome.summary
        PendingInputInteractionOutcome.DeliveryUnknown ->
            stringResource(R.string.pending_input_delivery_unknown)
        PendingInputInteractionOutcome.RetryAvailable ->
            stringResource(R.string.pending_input_retry_identity_preserved)
        PendingInputInteractionOutcome.ResolvedElsewhere ->
            stringResource(R.string.pending_input_resolved_elsewhere)
        null,
        PendingInputInteractionOutcome.Accepted,
        -> null
    }
    val color = when (outcome) {
        is PendingInputInteractionOutcome.Failed -> colors.error
        PendingInputInteractionOutcome.DeliveryUnknown,
        PendingInputInteractionOutcome.RetryAvailable,
        -> colors.warning
        else -> colors.muted
    }
    val feedbackSemantics = pendingInputFeedbackSemantics(outcome)
    message?.let {
        HorizontalDivider(color = color.copy(alpha = 0.35f))
        Text(
            text = it,
            modifier = Modifier
                .fillMaxWidth()
                .semantics {
                    this.liveRegion = when (feedbackSemantics?.announcement) {
                        PendingInputFeedbackAnnouncement.Assertive -> LiveRegionMode.Assertive
                        PendingInputFeedbackAnnouncement.Polite,
                        null,
                        -> LiveRegionMode.Polite
                    }
                    if (feedbackSemantics?.isError == true) error(it)
                }
                .testTag("pending-input-feedback"),
            style = typography.process,
            color = color,
        )
    }
}

private fun PendingInputDockMode.eyebrowResource(): Int = when (this) {
    PendingInputDockMode.Approval -> R.string.input_required_approval
    PendingInputDockMode.Clarify -> R.string.input_required_clarify
    PendingInputDockMode.Restored -> R.string.input_required_restored
}

private fun SessionApprovalChoice.labelResource(): Int = when (this) {
    SessionApprovalChoice.ALLOW_ONCE -> R.string.approval_allow_once
    SessionApprovalChoice.ALLOW_SESSION -> R.string.approval_allow_session
    SessionApprovalChoice.ALLOW_ALWAYS -> R.string.approval_allow_always
    SessionApprovalChoice.DENY -> R.string.approval_deny
}
