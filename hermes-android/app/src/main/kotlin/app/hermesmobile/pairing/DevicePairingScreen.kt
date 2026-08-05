package app.hermesmobile.pairing

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.hermesmobile.R
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingScope

private val PairingPanelShape = RoundedCornerShape(8.dp)

@Composable
fun DevicePairingScreen(
    state: DevicePairingUiState,
    onBack: () -> Unit,
    onPairingCodeChanged: (String) -> Unit,
    onWorkspaceIdChanged: (String) -> Unit,
    onAgentIdChanged: (String) -> Unit,
    onDeviceDisplayNameChanged: (String) -> Unit,
    onRequestControlScopeChanged: (Boolean) -> Unit,
    onFingerprintVerificationChanged: (Boolean) -> Unit,
    onClaim: () -> Unit,
    onConfirm: () -> Unit,
    onRejectFingerprint: () -> Unit,
    onCancel: () -> Unit,
    onRevoke: () -> Unit,
    onReset: () -> Unit,
    onRetryPending: () -> Unit = {},
) {
    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = { PairingTopBar(onBack) },
    ) { contentPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(contentPadding)
                .verticalScroll(rememberScrollState())
                .navigationBarsPadding()
                .padding(horizontal = 16.dp, vertical = 18.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            when (state.phase) {
                DevicePairingPhase.ENTER_CODE -> PairingEntry(
                    state = state,
                    onPairingCodeChanged = onPairingCodeChanged,
                    onWorkspaceIdChanged = onWorkspaceIdChanged,
                    onAgentIdChanged = onAgentIdChanged,
                    onDeviceDisplayNameChanged = onDeviceDisplayNameChanged,
                    onRequestControlScopeChanged = onRequestControlScopeChanged,
                    onClaim = onClaim,
                )

                DevicePairingPhase.REVIEW -> PairingReview(
                    state = state,
                    onFingerprintVerificationChanged = onFingerprintVerificationChanged,
                    onConfirm = onConfirm,
                    onRejectFingerprint = onRejectFingerprint,
                    onCancel = onCancel,
                )

                DevicePairingPhase.AWAITING_CONNECTOR_PROOF,
                DevicePairingPhase.ACTIVE,
                -> PairingActivation(
                    state = state,
                    onCancel = onCancel,
                    onRevoke = onRevoke,
                )

                DevicePairingPhase.CLAIMING,
                DevicePairingPhase.CONFIRMING,
                DevicePairingPhase.CANCELLING,
                DevicePairingPhase.REVOKING,
                -> PairingProgress(state.phase)

                DevicePairingPhase.DELIVERY_UNKNOWN ->
                    PairingPendingRetry(state, onRetryPending)

                DevicePairingPhase.CANCELLED,
                DevicePairingPhase.REVOKED,
                DevicePairingPhase.BLOCKED,
                DevicePairingPhase.EXPIRED,
                DevicePairingPhase.CLAIM_RATE_LIMITED,
                -> PairingTerminalState(state, onReset)

                DevicePairingPhase.AUTHENTICATION_REQUIRED,
                DevicePairingPhase.ERROR,
                -> if (state.canRetryPending) {
                    PairingPendingRetry(state, onRetryPending)
                } else {
                    PairingTerminalState(state, onReset)
                }
            }
        }
    }
}

@Composable
private fun PairingTopBar(onBack: () -> Unit) {
    Surface(color = MaterialTheme.colorScheme.surface) {
        Column(modifier = Modifier.windowInsetsPadding(WindowInsets.statusBars)) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 52.dp)
                    .padding(start = 4.dp, end = 16.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TextButton(onClick = onBack, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(
                        text = "‹",
                        style = MaterialTheme.typography.headlineSmall,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                }
                Text(
                    text = stringResource(R.string.device_pairing_title),
                    modifier = Modifier.weight(1f),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                PairingEyebrow(stringResource(R.string.owner_label))
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        }
    }
}

@Composable
private fun PairingEntry(
    state: DevicePairingUiState,
    onPairingCodeChanged: (String) -> Unit,
    onWorkspaceIdChanged: (String) -> Unit,
    onAgentIdChanged: (String) -> Unit,
    onDeviceDisplayNameChanged: (String) -> Unit,
    onRequestControlScopeChanged: (Boolean) -> Unit,
    onClaim: () -> Unit,
) {
    PairingHeader(
        eyebrow = stringResource(R.string.pairing_claim_eyebrow),
        title = stringResource(R.string.pair_connector_title),
        detail = stringResource(R.string.pair_connector_description),
    )
    PairingPanel {
        PairingField(
            value = state.pairingCodeInput,
            onValueChange = onPairingCodeChanged,
            label = stringResource(R.string.pairing_code_label),
            placeholder = stringResource(R.string.pairing_code_example),
            keyboardOptions = KeyboardOptions(
                capitalization = KeyboardCapitalization.Characters,
                keyboardType = KeyboardType.Ascii,
            ),
            modifier = Modifier.testTag("pairing-code-input"),
        )
        PairingField(
            value = state.workspaceIdInput,
            onValueChange = onWorkspaceIdChanged,
            label = stringResource(R.string.workspace_id_label),
            modifier = Modifier.testTag("pairing-workspace-input"),
        )
        PairingField(
            value = state.agentIdInput,
            onValueChange = onAgentIdChanged,
            label = stringResource(R.string.agent_id_label),
            modifier = Modifier.testTag("pairing-agent-input"),
        )
        PairingField(
            value = state.deviceDisplayNameInput,
            onValueChange = onDeviceDisplayNameChanged,
            label = stringResource(R.string.device_name_label),
            modifier = Modifier.testTag("pairing-device-name-input"),
        )
        PairingScopeRow(
            checked = state.requestControlScope,
            onCheckedChange = onRequestControlScopeChanged,
        )
        Text(
            text = stringResource(R.string.pairing_control_scope_disclaimer),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Button(
            onClick = onClaim,
            enabled = state.canClaim,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
                .testTag("pairing-claim"),
        ) {
            Text(stringResource(R.string.claim_pairing))
        }
    }
}

@Composable
private fun PairingReview(
    state: DevicePairingUiState,
    onFingerprintVerificationChanged: (Boolean) -> Unit,
    onConfirm: () -> Unit,
    onRejectFingerprint: () -> Unit,
    onCancel: () -> Unit,
) {
    val view = state.ownerView ?: return
    PairingHeader(
        eyebrow = stringResource(R.string.verify_connector_eyebrow),
        title = stringResource(R.string.verify_connector_title),
        detail = stringResource(R.string.connector_metadata_display_only),
    )
    PairingPanel {
        Text(
            text = view.connector.displayName,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Text(
            text = "${view.connector.platformFamily} · ${view.connector.version}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        PairingDetailRow(
            label = stringResource(R.string.key_algorithm_label),
            value = view.connector.keyAlgorithm,
        )
        PairingDetailRow(
            label = stringResource(R.string.pairing_offer_id_label),
            value = view.pairingOfferId,
        )
        PairingDetailRow(
            label = stringResource(R.string.pairing_session_id_label),
            value = view.pairingSessionId.value,
        )
        PairingDetailRow(
            label = stringResource(R.string.workspace_id_label),
            value = view.binding.workspaceId,
        )
        PairingDetailRow(
            label = stringResource(R.string.agent_id_label),
            value = view.binding.agentId,
        )
        PairingDetailRow(
            label = stringResource(R.string.pairing_device_id_label),
            value = view.binding.deviceId.value,
        )
        PairingDetailRow(
            label = stringResource(R.string.pairing_credential_id_label),
            value = view.binding.credentialId,
        )
        PairingScopeSummary(view)
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        PairingEyebrow(stringResource(R.string.public_key_fingerprint_label))
        Text(
            text = view.credentialFingerprint,
            modifier = Modifier.testTag("pairing-fingerprint"),
            fontFamily = FontFamily.Monospace,
            fontSize = 12.sp,
            lineHeight = 18.sp,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Text(
            text = stringResource(R.string.pairing_expires_at, view.expiresAt),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = stringResource(R.string.pairing_does_not_grant_controller),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.primary,
        )
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable {
                    onFingerprintVerificationChanged(!state.fingerprintVerified)
                },
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Checkbox(
                checked = state.fingerprintVerified,
                onCheckedChange = onFingerprintVerificationChanged,
            )
            Text(
                text = stringResource(R.string.fingerprint_verified_label),
                modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }
        Button(
            onClick = onConfirm,
            enabled = state.canConfirm,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
                .testTag("pairing-confirm"),
        ) {
            Text(stringResource(R.string.confirm_pairing))
        }
        OutlinedButton(
            onClick = onRejectFingerprint,
            enabled = state.canCancel,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(stringResource(R.string.fingerprint_mismatch))
        }
        TextButton(
            onClick = onCancel,
            enabled = state.canCancel,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(stringResource(R.string.cancel_pairing))
        }
    }
}

@Composable
private fun PairingActivation(
    state: DevicePairingUiState,
    onCancel: () -> Unit,
    onRevoke: () -> Unit,
) {
    val view = state.ownerView ?: return
    var revokeConfirmationVisible by remember(view.binding.deviceId.value) {
        mutableStateOf(false)
    }
    val active = state.phase == DevicePairingPhase.ACTIVE
    PairingHeader(
        eyebrow = if (active) {
            stringResource(R.string.paired_eyebrow)
        } else {
            stringResource(R.string.proof_required_eyebrow)
        },
        title = if (active) {
            stringResource(R.string.device_paired)
        } else {
            stringResource(R.string.waiting_connector_proof)
        },
        detail = if (active) {
            stringResource(R.string.device_paired_description)
        } else {
            stringResource(R.string.waiting_connector_proof_description)
        },
    )
    PairingPanel {
        Text(
            text = view.connector.displayName,
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface,
            fontWeight = FontWeight.SemiBold,
        )
        PairingScopeSummary(view)
        Text(
            text = stringResource(R.string.pairing_expires_at, view.expiresAt),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = stringResource(R.string.pairing_does_not_grant_controller),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.primary,
        )
        if (!active) {
            OutlinedButton(
                onClick = onCancel,
                enabled = state.canCancel,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(stringResource(R.string.cancel_pending_pairing))
            }
        }
        if (revokeConfirmationVisible) {
            Surface(
                shape = PairingPanelShape,
                color = MaterialTheme.colorScheme.error.copy(alpha = 0.08f),
                border = BorderStroke(
                    1.dp,
                    MaterialTheme.colorScheme.error.copy(alpha = 0.4f),
                ),
            ) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = stringResource(R.string.revoke_device_confirmation),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                    Button(
                        onClick = onRevoke,
                        enabled = state.canRevoke,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(stringResource(R.string.confirm_revoke))
                    }
                    TextButton(
                        onClick = { revokeConfirmationVisible = false },
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(stringResource(R.string.keep_device))
                    }
                }
            }
        } else {
            OutlinedButton(
                onClick = { revokeConfirmationVisible = true },
                enabled = state.canRevoke,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(stringResource(R.string.revoke_device))
            }
        }
    }
}

@Composable
private fun PairingProgress(phase: DevicePairingPhase) {
    val label = when (phase) {
        DevicePairingPhase.CLAIMING -> stringResource(R.string.claiming_pairing)
        DevicePairingPhase.CONFIRMING -> stringResource(R.string.confirming_pairing)
        DevicePairingPhase.CANCELLING -> stringResource(R.string.cancelling_pairing)
        DevicePairingPhase.REVOKING -> stringResource(R.string.revoking_device)
        else -> stringResource(R.string.working)
    }
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 48.dp)
            .semantics { liveRegion = LiveRegionMode.Polite },
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        CircularProgressIndicator()
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun PairingTerminalState(
    state: DevicePairingUiState,
    onReset: () -> Unit,
) {
    val failure = state.failure
    val title = when (state.phase) {
        DevicePairingPhase.CANCELLED -> stringResource(R.string.pairing_cancelled)
        DevicePairingPhase.REVOKED -> stringResource(R.string.device_revoked)
        DevicePairingPhase.BLOCKED -> stringResource(R.string.device_activation_blocked)
        DevicePairingPhase.EXPIRED -> stringResource(R.string.pairing_expired)
        DevicePairingPhase.CLAIM_RATE_LIMITED -> {
            val seconds = (failure as? PairingFailure.Contract)?.retryAfterSeconds
            if (seconds != null) {
                pluralStringResource(
                    R.plurals.pairing_attempts_paused,
                    seconds,
                    seconds,
                )
            } else {
                stringResource(R.string.pairing_attempts_paused_unknown)
            }
        }
        DevicePairingPhase.AUTHENTICATION_REQUIRED ->
            stringResource(R.string.pairing_authentication_required)
        DevicePairingPhase.ERROR -> if (
            (failure as? PairingFailure.Contract)?.code ==
            PairingErrorCode.PAIRING_CLAIM_UNAVAILABLE
        ) {
            stringResource(R.string.pairing_claim_unavailable)
        } else {
            stringResource(R.string.pairing_failed)
        }
        else -> stringResource(R.string.pairing_failed)
    }
    val detail = when (state.phase) {
        DevicePairingPhase.REVOKED -> state.revokedAt?.let {
            stringResource(R.string.pairing_revoked_at, it)
        }
        DevicePairingPhase.ERROR -> if (
            (failure as? PairingFailure.Contract)?.code ==
            PairingErrorCode.PAIRING_CLAIM_UNAVAILABLE
        ) {
            stringResource(R.string.pairing_claim_unavailable_description)
        } else {
            stringResource(R.string.pairing_failed_description)
        }
        DevicePairingPhase.CLAIM_RATE_LIMITED ->
            stringResource(R.string.pairing_rate_limited_description)
        DevicePairingPhase.EXPIRED ->
            stringResource(R.string.pairing_expired_description)
        DevicePairingPhase.CANCELLED ->
            stringResource(R.string.pairing_cancelled_description)
        DevicePairingPhase.BLOCKED ->
            stringResource(R.string.device_activation_blocked_description)
        DevicePairingPhase.AUTHENTICATION_REQUIRED ->
            stringResource(R.string.pairing_authentication_required_description)
        else -> null
    }
    PairingHeader(
        eyebrow = stringResource(R.string.pairing_status_eyebrow),
        title = title,
        detail = detail,
    )
    OutlinedButton(
        onClick = onReset,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Text(stringResource(R.string.pair_another_device))
    }
}

@Composable
private fun PairingPendingRetry(
    state: DevicePairingUiState,
    onRetryPending: () -> Unit,
) {
    val title = if (state.phase == DevicePairingPhase.AUTHENTICATION_REQUIRED) {
        stringResource(R.string.pairing_authentication_required)
    } else {
        when (state.pendingOperation) {
            PairingOperationKind.CLAIM -> stringResource(R.string.pairing_claim_result_unknown)
            PairingOperationKind.CONFIRM ->
                stringResource(R.string.pairing_confirm_result_unknown)
            PairingOperationKind.CANCEL -> stringResource(R.string.pairing_cancel_result_unknown)
            PairingOperationKind.REVOKE -> stringResource(R.string.pairing_revoke_result_unknown)
            null -> stringResource(R.string.pairing_result_unknown)
        }
    }
    val action = when (state.pendingOperation) {
        PairingOperationKind.CLAIM -> stringResource(R.string.retry_same_pairing_claim)
        PairingOperationKind.CONFIRM -> stringResource(R.string.retry_same_pairing_confirm)
        PairingOperationKind.CANCEL -> stringResource(R.string.retry_same_pairing_cancel)
        PairingOperationKind.REVOKE -> stringResource(R.string.retry_same_pairing_revoke)
        null -> stringResource(R.string.retry_same_pairing_operation)
    }
    PairingHeader(
        eyebrow = stringResource(R.string.pairing_status_eyebrow),
        title = title,
        detail = if (state.phase == DevicePairingPhase.AUTHENTICATION_REQUIRED) {
            stringResource(R.string.pairing_reauthenticate_retry_description)
        } else {
            stringResource(R.string.pairing_delivery_unknown_description)
        },
    )
    OutlinedButton(
        onClick = onRetryPending,
        enabled = state.canRetryPending,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Text(action)
    }
}

@Composable
private fun PairingScopeRow(
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Column {
        PairingEyebrow(stringResource(R.string.authorized_scopes_label))
        Text(
            text = stringResource(R.string.observe_sessions_scope),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable { onCheckedChange(!checked) },
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Checkbox(checked = checked, onCheckedChange = onCheckedChange)
            Text(
                text = stringResource(R.string.request_control_permission),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }
    }
}

@Composable
private fun PairingScopeSummary(view: PairingOwnerView) {
    PairingEyebrow(stringResource(R.string.authorized_scopes_label))
    if (PairingScope.SESSION_OBSERVE in view.binding.scopes) {
        Text(
            text = stringResource(R.string.observe_sessions_scope),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
    }
    if (PairingScope.SESSION_CONTROL_REQUEST in view.binding.scopes) {
        Text(
            text = stringResource(R.string.request_control_scope),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
    }
}

@Composable
private fun PairingField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    modifier: Modifier = Modifier,
    placeholder: String = "",
    keyboardOptions: KeyboardOptions = KeyboardOptions.Default,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        placeholder = { if (placeholder.isNotEmpty()) Text(placeholder) },
        modifier = modifier.fillMaxWidth(),
        singleLine = true,
        keyboardOptions = keyboardOptions,
        textStyle = MaterialTheme.typography.bodyMedium.copy(
            fontFamily = FontFamily.Monospace,
        ),
    )
}

@Composable
private fun PairingHeader(
    eyebrow: String,
    title: String,
    detail: String?,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .widthIn(max = 560.dp),
    ) {
        PairingEyebrow(eyebrow)
        Spacer(Modifier.height(6.dp))
        Text(
            text = title,
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onBackground,
        )
        detail?.let {
            Spacer(Modifier.height(6.dp))
            Text(
                text = it,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun PairingPanel(content: @Composable ColumnScope.() -> Unit) {
    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .widthIn(max = 560.dp),
        shape = PairingPanelShape,
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
    ) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            content = content,
        )
    }
}

@Composable
private fun PairingDetailRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            text = label,
            modifier = Modifier.weight(0.36f),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = value,
            modifier = Modifier.weight(0.64f),
            style = MaterialTheme.typography.bodySmall,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun PairingEyebrow(text: String) {
    Text(
        text = text.uppercase(),
        fontFamily = FontFamily.Monospace,
        fontSize = 10.sp,
        letterSpacing = 0.7.sp,
        fontWeight = FontWeight.Bold,
        color = MaterialTheme.colorScheme.primary,
    )
}
