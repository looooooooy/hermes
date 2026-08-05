package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.error
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.hermesmobile.R

@Composable
internal fun HermesTranscriptComposer(
    draft: String,
    presentation: TranscriptComposerPresentation,
    isInterrupting: Boolean,
    onDraftChanged: (String) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
    guidanceActionVisible: Boolean = false,
    guidanceExpanded: Boolean = false,
    onGuidanceAction: () -> Unit = {},
    voiceInputState: SessionVoiceInputState = SessionVoiceInputState(),
    onVoiceAction: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val voiceInputActive = voiceInputState.phase == SessionVoiceInputPhase.LISTENING ||
        voiceInputState.phase == SessionVoiceInputPhase.REQUESTING_PERMISSION
    val voiceActionEnabled = presentation.inputEnabled &&
        voiceInputState.phase != SessionVoiceInputPhase.REQUESTING_PERMISSION
    val voiceActionDescription = stringResource(
        if (voiceInputState.phase == SessionVoiceInputPhase.LISTENING) {
            R.string.voice_input_cancel
        } else {
            R.string.voice_input_start
        },
    )
    val guidanceActionDescription = stringResource(
        if (guidanceExpanded) R.string.guidance_collapse else R.string.guidance_expand,
    )
    val guidanceActionState = stringResource(
        if (guidanceExpanded) R.string.guidance_expanded else R.string.guidance_collapsed,
    )
    val primaryActionDescription = stringResource(
        when (presentation.primaryAction) {
            TranscriptComposerPrimaryAction.Send -> R.string.send
            TranscriptComposerPrimaryAction.Queue -> R.string.queue_prompt
            TranscriptComposerPrimaryAction.Guide -> R.string.guidance_submit_label
        },
    )
    val stopActionDescription = stringResource(
        if (isInterrupting) R.string.stopping else R.string.stop,
    )
    val messageInputDescription = stringResource(R.string.message_input_label)
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 9.dp),
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp),
            shape = RoundedCornerShape(9.dp),
            color = colors.statusBackground,
            border = BorderStroke(1.dp, colors.borderRail.copy(alpha = 0.65f)),
            tonalElevation = 0.dp,
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                BasicTextField(
                    value = draft,
                    onValueChange = onDraftChanged,
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 48.dp)
                        .semantics { contentDescription = messageInputDescription }
                        .testTag("message-input"),
                    enabled = presentation.inputEnabled && !voiceInputActive,
                    textStyle = MaterialTheme.typography.bodyMedium.copy(
                        color = if (presentation.inputEnabled) colors.text else colors.muted,
                    ),
                    cursorBrush = SolidColor(colors.accent),
                    minLines = 1,
                    maxLines = 5,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(
                        onSend = {
                            if (presentation.keyboardSendEnabled) onSend()
                        },
                    ),
                    decorationBox = { innerTextField ->
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(start = 12.dp, top = 12.dp, bottom = 12.dp),
                            contentAlignment = Alignment.CenterStart,
                        ) {
                            if (draft.isEmpty()) {
                                Text(
                                    text = stringResource(R.string.message_placeholder),
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = colors.muted.copy(alpha = 0.72f),
                                )
                            }
                            innerTextField()
                        }
                    },
                )
                TextButton(
                    onClick = onVoiceAction,
                    enabled = voiceActionEnabled,
                    modifier = Modifier
                        .heightIn(min = 48.dp)
                        .semantics { contentDescription = voiceActionDescription }
                        .testTag("voice-input-button"),
                    contentPadding = PaddingValues(horizontal = 10.dp),
                ) {
                    Text(
                        text = stringResource(
                            if (voiceInputState.phase == SessionVoiceInputPhase.LISTENING) {
                                R.string.cancel
                            } else {
                                R.string.voice_input_action
                            },
                        ),
                        color = if (voiceInputActive) colors.warning else colors.accent,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .testTag("composer-action-row"),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (guidanceActionVisible) {
                TextButton(
                    onClick = onGuidanceAction,
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 48.dp)
                        .semantics {
                            contentDescription = guidanceActionDescription
                            stateDescription = guidanceActionState
                        }
                        .testTag("guidance-toggle"),
                    contentPadding = PaddingValues(horizontal = 8.dp),
                ) {
                    Text(
                        text = stringResource(R.string.guidance_action),
                        style = HermesTranscriptThemeTokens.typography.process,
                        color = colors.accent,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            Button(
                onClick = onSend,
                enabled = presentation.primaryEnabled && !voiceInputActive,
                modifier = Modifier
                    .weight(1f)
                    .heightIn(min = 48.dp)
                    .semantics { contentDescription = primaryActionDescription }
                    .testTag(
                        when (presentation.primaryAction) {
                            TranscriptComposerPrimaryAction.Send -> "send-button"
                            TranscriptComposerPrimaryAction.Queue -> "queue-button"
                            TranscriptComposerPrimaryAction.Guide -> "guidance-submit-button"
                        },
                    ),
                shape = RoundedCornerShape(9.dp),
                contentPadding = PaddingValues(horizontal = 8.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = colors.accent,
                    contentColor = colors.background,
                ),
            ) {
                Text(
                    text = primaryActionDescription,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            if (presentation.stopActionVisible) {
                OutlinedButton(
                    onClick = onStop,
                    enabled = presentation.stopEnabled,
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 48.dp)
                        .semantics { contentDescription = stopActionDescription }
                        .testTag("stop-button"),
                    border = BorderStroke(1.dp, colors.warning),
                    shape = RoundedCornerShape(9.dp),
                    contentPadding = PaddingValues(horizontal = 8.dp),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = colors.warning),
                ) {
                    if (isInterrupting) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            strokeWidth = 2.dp,
                            color = colors.warning,
                        )
                        Spacer(Modifier.width(6.dp))
                    }
                    Text(
                        text = stopActionDescription,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
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
                style = HermesTranscriptThemeTokens.typography.meta,
                color = if (voiceInputState.phase == SessionVoiceInputPhase.ERROR) {
                    colors.error
                } else {
                    colors.muted
                },
            )
        }
    }
}

internal fun SessionVoiceInputState.messageResource(): Int? = when (phase) {
    SessionVoiceInputPhase.IDLE -> null
    SessionVoiceInputPhase.REQUESTING_PERMISSION -> R.string.voice_input_requesting_permission
    SessionVoiceInputPhase.LISTENING -> R.string.voice_input_listening
    SessionVoiceInputPhase.ERROR -> when (failure) {
        SessionVoiceInputFailure.PERMISSION_DENIED -> R.string.voice_input_permission_denied
        SessionVoiceInputFailure.SERVICE_UNAVAILABLE -> R.string.voice_input_unavailable
        SessionVoiceInputFailure.NO_MATCH -> R.string.voice_input_no_match
        SessionVoiceInputFailure.AUDIO -> R.string.voice_input_audio_error
        SessionVoiceInputFailure.NETWORK -> R.string.voice_input_network_error
        SessionVoiceInputFailure.RECOGNIZER_BUSY -> R.string.voice_input_busy
        SessionVoiceInputFailure.CLIENT,
        SessionVoiceInputFailure.UNKNOWN,
        null,
        -> R.string.voice_input_failed
    }
}
