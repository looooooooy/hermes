package app.hermesmobile.sessions

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.hermesmobile.R
import app.hermesmobile.sessions.control.ControlLossReason
import app.hermesmobile.sessions.control.ControlMode

@Composable
internal fun HermesSessionTopBar(
    state: SessionBrowserUiState,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    actionLabel: String? = null,
    actionEnabled: Boolean = true,
    onAction: (() -> Unit)? = null,
    secondaryActionLabel: String? = null,
    onSecondaryAction: (() -> Unit)? = null,
) {
    ProvideHermesTranscriptDesignSystem {
        val colors = HermesTranscriptThemeTokens.colors
        val typography = HermesTranscriptThemeTokens.typography
        val presentation = state.chromePresentation()
        val backLabel = stringResource(R.string.back)
        Surface(
            modifier = modifier
                .fillMaxWidth()
                .background(colors.background),
            color = colors.background,
            contentColor = colors.text,
            shadowElevation = 0.dp,
        ) {
            Column(modifier = Modifier.windowInsetsPadding(WindowInsets.statusBars)) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 52.dp)
                        .padding(start = 4.dp, end = 14.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    TextButton(
                        onClick = onBack,
                        modifier = Modifier
                            .size(48.dp)
                            .semantics { contentDescription = backLabel },
                    ) {
                        Text(
                            text = "‹",
                            style = MaterialTheme.typography.headlineSmall,
                            color = colors.text,
                        )
                    }
                    Column(
                        modifier = Modifier.weight(1f),
                        verticalArrangement = Arrangement.Center,
                    ) {
                        Text(
                            text = state.selectedSession?.title
                                ?: stringResource(R.string.sessions_title),
                            style = typography.process,
                            color = colors.text,
                            fontWeight = FontWeight.SemiBold,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        state.selectedSession?.sessionKey?.value?.let { sessionKey ->
                            Text(
                                text = stringResource(R.string.session_identity, sessionKey),
                                style = typography.meta,
                                color = colors.muted,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                    if (
                        secondaryActionLabel != null &&
                        onSecondaryAction != null
                    ) {
                        TextButton(onClick = onSecondaryAction) {
                            Text(
                                text = secondaryActionLabel,
                                style = typography.process,
                                color = colors.text,
                            )
                        }
                    }
                    if (actionLabel != null && onAction != null) {
                        TextButton(
                            onClick = onAction,
                            enabled = actionEnabled,
                        ) {
                            Text(
                                text = actionLabel,
                                style = typography.process,
                                color = colors.accent,
                            )
                        }
                    } else {
                        SessionChromeBadgeLine(presentation.badge)
                    }
                }
                HorizontalDivider(color = colors.borderRail.copy(alpha = 0.72f))
            }
        }
    }
}

@Composable
internal fun HermesSessionStatusStrip(
    state: SessionBrowserUiState,
    onRetryControl: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val presentation = state.chromePresentation()
    if (!presentation.showsStatusStrip) return

    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val headline = when (presentation.badge) {
        SessionChromeBadge.Restored -> stringResource(R.string.control_restored)
        SessionChromeBadge.Acquiring -> stringResource(R.string.acquiring_control)
        SessionChromeBadge.ControlUnavailable -> if (presentation.showsControlLossReason) {
            state.controlStatusText()
        } else {
            state.connectionStatusText() ?: stringResource(R.string.control_unavailable)
        }
        SessionChromeBadge.Disconnected ->
            state.connectionStatusText() ?: stringResource(R.string.realtime_disconnected)
        SessionChromeBadge.Controller -> stringResource(R.string.controller_mode)
        SessionChromeBadge.Observer -> if (
            state.controlStatus == RealtimeControlStatus.SERVER_UPGRADE_REQUIRED
        ) {
            stringResource(R.string.server_upgrade_required)
        } else {
            state.connectionStatusText() ?: stringResource(R.string.observer_mode)
        }
    }
    val detail = if (
        presentation.badge == SessionChromeBadge.ControlUnavailable &&
        !presentation.showsControlLossReason
    ) {
        state.connectionStatusText()?.takeUnless { it == headline }
    } else {
        state.realtimeMessage
            ?.takeIf(String::isNotBlank)
            ?: when (presentation.badge) {
            SessionChromeBadge.Restored ->
                stringResource(R.string.pending_input_retry_identity_preserved)
            else -> state.connectionStatusText()?.takeUnless { it == headline }
            }
        }
    val tone = when (presentation.badge) {
        SessionChromeBadge.Restored -> colors.success
        SessionChromeBadge.Acquiring,
        SessionChromeBadge.ControlUnavailable,
        SessionChromeBadge.Disconnected,
        -> colors.warning
        SessionChromeBadge.Controller -> colors.accent
        SessionChromeBadge.Observer -> colors.muted
    }

    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(colors.statusBackground)
            .padding(horizontal = 16.dp, vertical = 10.dp)
            .testTag("session-status-strip"),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Surface(
            modifier = Modifier.size(8.dp),
            shape = RoundedCornerShape(50),
            color = tone,
        ) {}
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = headline,
                style = typography.process,
                color = colors.text,
                fontWeight = FontWeight.Medium,
            )
            detail?.let {
                Spacer(Modifier.height(2.dp))
                Text(
                    text = it,
                    modifier = Modifier.testTag("realtime-connection-status"),
                    style = typography.meta,
                    color = colors.muted,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        if (presentation.canRetryControl) {
            TextButton(
                onClick = onRetryControl,
                modifier = Modifier.testTag("retry-control"),
            ) {
                Text(stringResource(R.string.try_again), color = colors.accent)
            }
        }
    }
    HorizontalDivider(color = colors.borderRail)
}

@Composable
internal fun HermesCurrentExecutionStrip(
    presentation: CurrentExecutionPresentation?,
    modifier: Modifier = Modifier,
) {
    if (presentation == null) return
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val label = stringResource(R.string.currently_executing)
    val detail = presentation.detail ?: stringResource(presentation.kind.detailResource())
    val running = stringResource(R.string.long_running_running)
    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(colors.activeBackground)
            .padding(horizontal = 16.dp, vertical = 9.dp)
            .semantics(mergeDescendants = true) {
                liveRegion = LiveRegionMode.Polite
                stateDescription = "$label: $detail"
            }
            .testTag("current-execution-strip"),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Spacer(
            modifier = Modifier
                .width(3.dp)
                .height(29.dp)
                .background(colors.accent, RoundedCornerShape(2.dp)),
        )
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = label.uppercase(),
                style = typography.meta,
                color = colors.accent,
                fontWeight = FontWeight.Bold,
                letterSpacing = 0.8.sp,
            )
            Spacer(Modifier.height(2.dp))
            Text(
                text = detail,
                style = typography.process,
                color = colors.statusForeground,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Text(
            text = running,
            style = typography.meta,
            color = colors.muted,
            maxLines = 1,
        )
    }
    HorizontalDivider(color = colors.borderRail)
}

private fun CurrentExecutionKind.detailResource(): Int = when (this) {
    CurrentExecutionKind.TODO -> R.string.current_execution_todo
    CurrentExecutionKind.THINKING -> R.string.thinking_label
    CurrentExecutionKind.TOOL -> R.string.current_execution_tool
    CurrentExecutionKind.SUBAGENT -> R.string.current_execution_subagent
    CurrentExecutionKind.RESPONSE -> R.string.current_execution_response
    CurrentExecutionKind.ACTIVITY,
    CurrentExecutionKind.WORKING,
    -> R.string.working
}

@Composable
private fun SessionChromeBadgeLine(badge: SessionChromeBadge) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val tone = when (badge) {
        SessionChromeBadge.Controller,
        SessionChromeBadge.Restored,
        -> colors.success
        SessionChromeBadge.Acquiring,
        SessionChromeBadge.ControlUnavailable,
        SessionChromeBadge.Disconnected,
        -> colors.warning
        SessionChromeBadge.Observer -> colors.muted
    }
    val label = when (badge) {
        SessionChromeBadge.Controller -> stringResource(R.string.controller_mode)
        SessionChromeBadge.Observer -> stringResource(R.string.observer_mode)
        SessionChromeBadge.Restored -> stringResource(R.string.restored)
        SessionChromeBadge.Acquiring -> stringResource(R.string.realtime_connecting)
        SessionChromeBadge.ControlUnavailable -> stringResource(R.string.control_unavailable)
        SessionChromeBadge.Disconnected -> stringResource(R.string.realtime_disconnected)
    }
    Row(
        modifier = Modifier.padding(start = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Surface(
            modifier = Modifier.size(7.dp),
            shape = RoundedCornerShape(50),
            color = tone,
        ) {}
        Text(
            text = label,
            style = typography.meta,
            color = tone,
            maxLines = 1,
        )
    }
}

private fun SessionBrowserUiState.chromePresentation(): SessionChromePresentation =
    sessionChromePresentation(
        controlMode = control.mode,
        connectionStatus = realtimeConnectionStatus,
        pendingOutcome = pendingInteraction.outcome,
        hasStatusMessage = !realtimeMessage.isNullOrBlank(),
        requiresServerUpgrade =
            controlStatus == RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
        hasControlCapability = controlAvailableMethods.isNotEmpty(),
    )

@Composable
private fun SessionBrowserUiState.controlStatusText(): String = when (val mode = control.mode) {
    ControlMode.Acquiring -> stringResource(R.string.acquiring_control)
    is ControlMode.Controller -> stringResource(R.string.controller_mode)
    is ControlMode.Conflict -> stringResource(
        R.string.control_conflict,
        controllerOwnerDisplayLabel(mode),
    )
    is ControlMode.Lost -> when (mode.reason) {
        ControlLossReason.LEASE_EXPIRED -> stringResource(R.string.control_lease_expired)
        ControlLossReason.CONNECTION_LOST -> stringResource(R.string.control_connection_lost)
        ControlLossReason.RELEASED -> stringResource(R.string.control_released)
        ControlLossReason.REJECTED -> stringResource(R.string.control_rejected)
    }
    ControlMode.Observer -> stringResource(R.string.observer_mode)
    ControlMode.Disconnected -> when (controlStatus) {
        RealtimeControlStatus.SERVER_UPGRADE_REQUIRED ->
            stringResource(R.string.server_upgrade_required)
        RealtimeControlStatus.OBSERVER -> stringResource(R.string.observer_mode)
        RealtimeControlStatus.CONTROLLER -> stringResource(R.string.controller_mode)
    }
}

@Composable
private fun SessionBrowserUiState.connectionStatusText(): String? = when (realtimeConnectionStatus) {
    RealtimeConnectionStatus.IDLE -> null
    RealtimeConnectionStatus.CONNECTING -> stringResource(R.string.realtime_connecting)
    RealtimeConnectionStatus.LIVE -> stringResource(R.string.realtime_live)
    RealtimeConnectionStatus.RECONNECTING -> stringResource(R.string.realtime_reconnecting)
    RealtimeConnectionStatus.DISCONNECTED -> stringResource(R.string.realtime_disconnected)
    RealtimeConnectionStatus.UNSUPPORTED -> stringResource(R.string.realtime_unsupported)
    RealtimeConnectionStatus.ERROR -> stringResource(R.string.realtime_error)
}
