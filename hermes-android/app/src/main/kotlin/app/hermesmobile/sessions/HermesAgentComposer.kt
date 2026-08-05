package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.error
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.hermesmobile.R

private const val COMPOSER_ACTION_SPACING_DP = 2
private const val COMPOSER_MODE_BUTTON_WIDTH_DP = 56
internal const val COMPOSER_VOICE_BUTTON_WIDTH_DP = 64
private const val COMPOSER_STOP_BUTTON_WIDTH_DP = 48
private const val COMPOSER_PRIMARY_BUTTON_WIDTH_DP = 48

internal fun composerActionRowMinimumWidthDp(
    guidanceActionVisible: Boolean,
    stopActionVisible: Boolean,
): Int {
    val controlWidths = buildList {
        if (guidanceActionVisible) {
            add(COMPOSER_MODE_BUTTON_WIDTH_DP)
            add(COMPOSER_MODE_BUTTON_WIDTH_DP)
        }
        add(COMPOSER_VOICE_BUTTON_WIDTH_DP)
        if (stopActionVisible) add(COMPOSER_STOP_BUTTON_WIDTH_DP)
        add(COMPOSER_PRIMARY_BUTTON_WIDTH_DP)
    }
    // The weighted spacer is an additional Row child, so spacedBy contributes
    // one gap per visible control.
    return controlWidths.sum() + (controlWidths.size * COMPOSER_ACTION_SPACING_DP)
}

/**
 * The one authoritative Hermes composer surface.
 *
 * Queue and Guide select delivery semantics for the same input field; they do
 * not create a second editor or a second conversation surface.
 */
@Composable
internal fun HermesAgentComposer(
    draft: String,
    presentation: TranscriptComposerPresentation,
    isInterrupting: Boolean,
    guidanceMode: Boolean,
    guidanceActionVisible: Boolean,
    onDraftChanged: (String) -> Unit,
    onSubmit: () -> Unit,
    onStop: () -> Unit,
    onGuideMode: () -> Unit,
    onQueueMode: () -> Unit,
    modifier: Modifier = Modifier,
    guidanceState: SessionGuidanceState = SessionGuidanceState(),
    voiceInputState: SessionVoiceInputState = SessionVoiceInputState(),
    onVoiceAction: () -> Unit = {},
) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val inputFocusRequester = remember { FocusRequester() }
    LaunchedEffect(guidanceMode) {
        if (guidanceMode) inputFocusRequester.requestFocus()
    }
    val voiceInputActive = voiceInputState.phase == SessionVoiceInputPhase.LISTENING ||
        voiceInputState.phase == SessionVoiceInputPhase.REQUESTING_PERMISSION
    val voiceActionEnabled = presentation.inputEnabled &&
        voiceInputState.phase != SessionVoiceInputPhase.REQUESTING_PERMISSION
    val voiceDescription = stringResource(
        if (voiceInputState.phase == SessionVoiceInputPhase.LISTENING) {
            R.string.voice_input_cancel
        } else {
            R.string.voice_input_start
        },
    )
    val inputDescription = stringResource(
        if (guidanceMode) R.string.guidance_input_label else R.string.message_input_label,
    )
    val primaryDescription = stringResource(
        when (presentation.primaryAction) {
            TranscriptComposerPrimaryAction.Send -> R.string.send
            TranscriptComposerPrimaryAction.Queue -> R.string.queue_prompt
            TranscriptComposerPrimaryAction.Guide -> R.string.guidance_submit_label
        },
    )
    val stopDescription = stringResource(
        if (isInterrupting) R.string.stopping else R.string.stop,
    )
    val guideModeDescription = stringResource(R.string.guidance_mode_description)
    val queueModeDescription = stringResource(R.string.queue_mode_description)

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 9.dp),
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth()
                .testTag("agent-composer"),
            shape = RoundedCornerShape(13.dp),
            color = colors.statusBackground,
            border = BorderStroke(
                1.dp,
                if (guidanceMode) colors.accent.copy(alpha = 0.78f) else colors.borderRail,
            ),
            tonalElevation = 0.dp,
        ) {
            Column {
                if (guidanceMode) {
                    Text(
                        text = stringResource(R.string.guidance_title).uppercase(),
                        modifier = Modifier.padding(start = 12.dp, end = 12.dp, top = 9.dp),
                        style = typography.meta,
                        color = colors.accent,
                        fontWeight = FontWeight.Bold,
                    )
                }
                BasicTextField(
                    value = draft,
                    onValueChange = onDraftChanged,
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = if (guidanceMode) 58.dp else 66.dp, max = 124.dp)
                        .focusRequester(inputFocusRequester)
                        .semantics { contentDescription = inputDescription }
                        .testTag("message-input"),
                    enabled = presentation.inputEnabled && !voiceInputActive,
                    textStyle = typography.body.copy(
                        color = if (presentation.inputEnabled) colors.text else colors.muted,
                    ),
                    cursorBrush = SolidColor(colors.accent),
                    minLines = 2,
                    maxLines = 5,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(
                        onSend = {
                            if (presentation.keyboardSendEnabled) onSubmit()
                        },
                    ),
                    decorationBox = { innerTextField ->
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            contentAlignment = Alignment.TopStart,
                        ) {
                            if (draft.isEmpty()) {
                                Text(
                                    text = stringResource(
                                        if (guidanceMode) {
                                            R.string.guidance_placeholder
                                        } else {
                                            R.string.message_placeholder
                                        },
                                    ),
                                    style = typography.body,
                                    color = colors.muted.copy(alpha = 0.72f),
                                )
                            }
                            innerTextField()
                        }
                    },
                )
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 52.dp)
                        .padding(horizontal = 6.dp, vertical = 2.dp)
                        .testTag("composer-action-row"),
                    horizontalArrangement = Arrangement.spacedBy(COMPOSER_ACTION_SPACING_DP.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    if (guidanceActionVisible) {
                        ComposerModeButton(
                            text = stringResource(R.string.guidance_action),
                            contentDescription = guideModeDescription,
                            selected = guidanceMode,
                            enabled = !voiceInputActive,
                            onClick = onGuideMode,
                            testTag = "guidance-toggle",
                        )
                        ComposerModeButton(
                            text = stringResource(R.string.queue_prompt),
                            contentDescription = queueModeDescription,
                            selected = !guidanceMode,
                            enabled = !voiceInputActive,
                            onClick = onQueueMode,
                            testTag = "queue-mode-toggle",
                        )
                    }
                    Spacer(Modifier.weight(1f))
                    TextButton(
                        onClick = onVoiceAction,
                        enabled = voiceActionEnabled,
                        modifier = Modifier
                            .width(COMPOSER_VOICE_BUTTON_WIDTH_DP.dp)
                            .heightIn(min = 48.dp)
                            .semantics { contentDescription = voiceDescription }
                            .testTag("voice-input-button"),
                        contentPadding = PaddingValues(horizontal = 2.dp),
                    ) {
                        Text(
                            text = stringResource(
                                if (voiceInputState.phase == SessionVoiceInputPhase.LISTENING) {
                                    R.string.cancel
                                } else {
                                    R.string.voice_input_action
                                },
                            ),
                            style = typography.meta,
                            color = if (voiceInputActive) colors.warning else colors.accent,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    if (presentation.stopActionVisible) {
                        TextButton(
                            onClick = onStop,
                            enabled = presentation.stopEnabled,
                            modifier = Modifier
                                .width(COMPOSER_STOP_BUTTON_WIDTH_DP.dp)
                                .heightIn(min = 48.dp)
                                .semantics { contentDescription = stopDescription }
                                .testTag("stop-button"),
                            contentPadding = PaddingValues(0.dp),
                        ) {
                            if (isInterrupting) {
                                CircularProgressIndicator(
                                    modifier = Modifier.width(16.dp),
                                    strokeWidth = 2.dp,
                                    color = colors.warning,
                                )
                            } else {
                                Surface(
                                    modifier = Modifier.width(10.dp).heightIn(min = 10.dp),
                                    shape = RoundedCornerShape(2.dp),
                                    color = colors.warning,
                                ) {}
                            }
                        }
                    }
                    Button(
                        onClick = onSubmit,
                        enabled = presentation.primaryEnabled && !voiceInputActive,
                        modifier = Modifier
                            .width(COMPOSER_PRIMARY_BUTTON_WIDTH_DP.dp)
                            .heightIn(min = 48.dp)
                            .semantics { contentDescription = primaryDescription }
                            .testTag(
                                when (presentation.primaryAction) {
                                    TranscriptComposerPrimaryAction.Send -> "send-button"
                                    TranscriptComposerPrimaryAction.Queue -> "queue-button"
                                    TranscriptComposerPrimaryAction.Guide -> "guidance-submit-button"
                                },
                            ),
                        shape = RoundedCornerShape(9.dp),
                        contentPadding = PaddingValues(0.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = colors.accent,
                            contentColor = colors.background,
                        ),
                    ) {
                        Text(
                            text = "↑",
                            style = typography.body,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
            }
        }
        if (guidanceActionVisible) {
            Text(
                text = stringResource(
                    if (guidanceMode) R.string.guidance_delivery_hint else R.string.queue_delivery_hint,
                ),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(start = 4.dp, top = 5.dp)
                    .testTag("composer-delivery-hint"),
                style = typography.meta,
                color = colors.muted,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        if (guidanceMode || guidanceState.phase != SessionGuidancePhase.IDLE) {
            SessionGuidanceFeedback(
                state = guidanceState,
                modifier = Modifier.padding(start = 4.dp, top = 5.dp),
            )
        }
        voiceInputState.messageResource()?.let { messageResource ->
            val message = stringResource(messageResource)
            Text(
                text = message,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(start = 4.dp, top = 5.dp)
                    .semantics {
                        liveRegion = LiveRegionMode.Polite
                        if (voiceInputState.phase == SessionVoiceInputPhase.ERROR) error(message)
                    }
                    .testTag("voice-input-feedback"),
                style = typography.meta,
                color = if (voiceInputState.phase == SessionVoiceInputPhase.ERROR) {
                    colors.error
                } else {
                    colors.muted
                },
            )
        }
    }
}

@Composable
private fun ComposerModeButton(
    text: String,
    contentDescription: String,
    selected: Boolean,
    enabled: Boolean,
    onClick: () -> Unit,
    testTag: String,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val selectedDescription = stringResource(
        if (selected) R.string.composer_mode_selected else R.string.composer_mode_not_selected,
    )
    Surface(
        onClick = onClick,
        enabled = enabled,
        modifier = Modifier
            .width(COMPOSER_MODE_BUTTON_WIDTH_DP.dp)
            .heightIn(min = 48.dp)
            .semantics {
                this.contentDescription = contentDescription
                this.selected = selected
                stateDescription = selectedDescription
            }
            .testTag(testTag),
        shape = RoundedCornerShape(8.dp),
        color = if (selected) colors.accent.copy(alpha = 0.1f) else colors.statusBackground,
        contentColor = if (selected) colors.accent else colors.muted,
        tonalElevation = 0.dp,
    ) {
        Box(contentAlignment = Alignment.Center) {
            Text(
                text = text,
                style = HermesTranscriptThemeTokens.typography.meta,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
