package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
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
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.error
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics

import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import app.hermesmobile.R

@Composable
internal fun SessionGuidanceControls(
    state: SessionGuidanceState,
    visible: Boolean,
    expanded: Boolean,
    inputEnabled: Boolean,
    onDraftChanged: (String) -> Unit,
    onSubmit: () -> Unit,
    modifier: Modifier = Modifier,
) {
    if (!visible || !expanded) return
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val accessibility = guidanceAccessibilityPresentation(expanded)
    val inputLabel = stringResource(accessibility.inputLabelResource)
    val submitLabel = stringResource(accessibility.submitLabelResource)
    val inputFocusRequester = remember { FocusRequester() }

    LaunchedEffect(expanded, inputEnabled) {
        if (expanded && inputEnabled) inputFocusRequester.requestFocus()
    }

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 3.dp),
    ) {
        if (expanded) {
            Surface(
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("guidance-panel"),
                shape = RoundedCornerShape(8.dp),
                color = colors.statusBackground,
                border = BorderStroke(1.dp, colors.borderRail.copy(alpha = 0.72f)),
                tonalElevation = 0.dp,
            ) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = stringResource(R.string.guidance_title),
                        style = typography.process,
                        color = colors.text,
                    )
                    Text(
                        text = stringResource(R.string.guidance_description),
                        style = typography.meta,
                        color = colors.muted,
                    )
                    Surface(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 48.dp),
                        shape = RoundedCornerShape(8.dp),
                        color = colors.background,
                        border = BorderStroke(1.dp, colors.borderRail),
                        tonalElevation = 0.dp,
                    ) {
                        BasicTextField(
                            value = state.draft,
                            onValueChange = onDraftChanged,
                            modifier = Modifier
                                .fillMaxWidth()
                                .focusRequester(inputFocusRequester)
                                .semantics { contentDescription = inputLabel }
                                .testTag("guidance-input"),
                            enabled = inputEnabled,
                            textStyle = typography.body.copy(
                                color = if (inputEnabled) colors.text else colors.muted,
                            ),
                            cursorBrush = SolidColor(colors.accent),
                            minLines = 1,
                            maxLines = 4,
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                            keyboardActions = KeyboardActions(
                                onSend = {
                                    if (inputEnabled && state.draft.isNotBlank()) onSubmit()
                                },
                            ),
                            decorationBox = { innerTextField ->
                                Box(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(horizontal = 12.dp, vertical = 12.dp),
                                    contentAlignment = Alignment.CenterStart,
                                ) {
                                    if (state.draft.isEmpty()) {
                                        Text(
                                            text = stringResource(R.string.guidance_placeholder),
                                            style = typography.body,
                                            color = colors.muted.copy(alpha = 0.72f),
                                        )
                                    }
                                    innerTextField()
                                }
                            },
                        )
                    }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.End,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Button(
                            onClick = onSubmit,
                            enabled = inputEnabled && state.draft.isNotBlank(),
                            modifier = Modifier
                                .heightIn(min = 48.dp)
                                .semantics { contentDescription = submitLabel }
                                .testTag("guidance-submit-button"),
                            shape = RoundedCornerShape(8.dp),
                            contentPadding = PaddingValues(horizontal = 16.dp),
                            colors = ButtonDefaults.buttonColors(
                                containerColor = colors.accent,
                                contentColor = colors.background,
                            ),
                        ) {
                            if (state.phase == SessionGuidancePhase.SUBMITTING) {
                                CircularProgressIndicator(
                                    color = colors.background,
                                    strokeWidth = 2.dp,
                                    modifier = Modifier.padding(end = 8.dp),
                                )
                            }
                            Text(stringResource(R.string.guidance_submit))
                        }
                    }
                    SessionGuidanceFeedback(state)
                }
            }
        }
    }
}

internal data class GuidanceAccessibilityPresentation(
    val toggleLabelResource: Int,
    val toggleStateResource: Int,
    val inputLabelResource: Int,
    val submitLabelResource: Int,
)

internal fun guidanceAccessibilityPresentation(
    expanded: Boolean,
): GuidanceAccessibilityPresentation = GuidanceAccessibilityPresentation(
    toggleLabelResource = if (expanded) R.string.guidance_collapse else R.string.guidance_expand,
    toggleStateResource = if (expanded) R.string.guidance_expanded else R.string.guidance_collapsed,
    inputLabelResource = R.string.guidance_input_label,
    submitLabelResource = R.string.guidance_submit_label,
)

@Composable
internal fun SessionGuidanceFeedback(
    state: SessionGuidanceState,
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val message = when (state.phase) {
        SessionGuidancePhase.IDLE -> null
        SessionGuidancePhase.SUBMITTING -> stringResource(R.string.guidance_submitting)
        SessionGuidancePhase.ACCEPTED -> stringResource(R.string.guidance_accepted)
        SessionGuidancePhase.DELIVERY_UNKNOWN -> stringResource(R.string.guidance_delivery_unknown)
        SessionGuidancePhase.FAILED -> state.failureSummary ?: stringResource(R.string.guidance_failed)
    } ?: return
    val failed = state.phase == SessionGuidancePhase.FAILED
    Text(
        text = message,
        modifier = modifier
            .fillMaxWidth()
            .semantics {
                liveRegion = LiveRegionMode.Polite
                if (failed) error(message)
            }
            .testTag("guidance-feedback"),
        style = typography.meta,
        color = when (state.phase) {
            SessionGuidancePhase.FAILED -> colors.error
            SessionGuidancePhase.DELIVERY_UNKNOWN -> colors.warning
            SessionGuidancePhase.ACCEPTED -> colors.success
            else -> colors.muted
        },
    )
}
