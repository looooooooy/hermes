package app.hermesmobile.connection

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.hermesmobile.R

private val ConnectionPanelShape = RoundedCornerShape(8.dp)
private val ConnectionControlShape = RoundedCornerShape(7.dp)

@Composable
fun ConnectionScreen(
    state: ConnectionUiState,
    onEndpointChanged: (String) -> Unit,
    onConnect: () -> Unit,
    onSignIn: () -> Unit,
    onPasswordSignIn: (String, String) -> Unit = { _, _ -> },
) {
    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = { HermesConnectionTopBar() },
    ) { contentPadding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(contentPadding),
            contentAlignment = Alignment.TopCenter,
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .widthIn(max = 560.dp)
                    .verticalScroll(rememberScrollState())
                    .navigationBarsPadding()
                    .padding(horizontal = 16.dp, vertical = 18.dp),
            ) {
                ConnectionEyebrow(text = "GATEWAY")
                Spacer(Modifier.height(6.dp))
                Text(
                    text = stringResource(R.string.execution_endpoint),
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onBackground,
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    text = stringResource(R.string.execution_endpoint_description),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(18.dp))
                Surface(
                    shape = ConnectionPanelShape,
                    color = MaterialTheme.colorScheme.surface,
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                ) {
                    Column(Modifier.padding(12.dp)) {
                        HermesConnectionField(
                            value = state.endpointInput,
                            onValueChange = onEndpointChanged,
                            label = stringResource(R.string.server_address),
                            placeholder = stringResource(R.string.server_address_example),
                            enabled = !state.isChecking && !state.isAuthenticating,
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                            isError = state.phase == ConnectionPhase.INVALID_ENDPOINT,
                            supportingText = if (state.phase == ConnectionPhase.INVALID_ENDPOINT) {
                                state.message
                            } else {
                                null
                            },
                            modifier = Modifier.testTag("endpoint-input"),
                        )
                        Spacer(Modifier.height(12.dp))
                        HermesConnectionAction(
                            label = if (state.isChecking) {
                                stringResource(R.string.checking_connection)
                            } else {
                                stringResource(R.string.check_connection)
                            },
                            enabled = state.canConnect,
                            loading = state.isChecking,
                            onClick = onConnect,
                        )
                    }
                }

                if (state.phase != ConnectionPhase.IDLE &&
                    state.phase != ConnectionPhase.INVALID_ENDPOINT
                ) {
                    Spacer(Modifier.height(16.dp))
                    ConnectionStatus(
                        state = state,
                        onSignIn = onSignIn,
                        onPasswordSignIn = onPasswordSignIn,
                    )
                }
            }
        }
    }
}

@Composable
private fun HermesConnectionTopBar() {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surface)
            .windowInsetsPadding(WindowInsets.statusBars),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 52.dp)
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = stringResource(R.string.app_name),
                modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                text = "CONNECTION",
                fontFamily = FontFamily.Monospace,
                fontSize = 10.sp,
                letterSpacing = 0.7.sp,
                color = MaterialTheme.colorScheme.primary,
            )
        }
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
    }
}

@Composable
private fun ConnectionStatus(
    state: ConnectionUiState,
    onSignIn: () -> Unit,
    onPasswordSignIn: (String, String) -> Unit,
) {
    val isFailure = state.phase == ConnectionPhase.UNAVAILABLE
    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .semantics { liveRegion = LiveRegionMode.Polite },
        shape = ConnectionPanelShape,
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(
            1.dp,
            if (isFailure) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.outline,
        ),
    ) {
        Column(Modifier.padding(14.dp)) {
            ConnectionEyebrow(
                text = when (state.phase) {
                    ConnectionPhase.CHECKING -> "VERIFYING"
                    ConnectionPhase.AUTHENTICATION_REQUIRED -> "AUTHENTICATION"
                    ConnectionPhase.AUTHENTICATING -> "SIGNING IN"
                    ConnectionPhase.READY -> "READY"
                    ConnectionPhase.UNAVAILABLE -> "UNAVAILABLE"
                    ConnectionPhase.IDLE,
                    ConnectionPhase.INVALID_ENDPOINT,
                    -> "GATEWAY"
                },
                isError = isFailure,
            )
            Spacer(Modifier.height(8.dp))
            when (state.phase) {
                ConnectionPhase.CHECKING -> StatusHeading(
                    text = stringResource(R.string.verifying_hermes),
                    loading = true,
                )

                ConnectionPhase.AUTHENTICATION_REQUIRED -> {
                    StatusHeading(
                        text = state.hermesVersion?.let {
                            stringResource(R.string.hermes_version_reachable, it)
                        } ?: stringResource(R.string.hermes_reachable),
                    )
                    EndpointDetails(state)
                    Spacer(Modifier.height(16.dp))
                    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
                    Spacer(Modifier.height(14.dp))
                    Text(
                        text = stringResource(R.string.sign_in_securely),
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        text = if (state.supportsNativePkce) {
                            stringResource(R.string.system_browser_pkce)
                        } else if (state.supportsPassword) {
                            stringResource(R.string.username_password_https)
                        } else {
                            stringResource(R.string.secure_sign_in_required)
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    if (state.supportsNativePkce) {
                        Spacer(Modifier.height(12.dp))
                        HermesConnectionAction(
                            label = stringResource(R.string.continue_in_browser),
                            enabled = state.canSignIn,
                            onClick = onSignIn,
                        )
                    }
                    if (state.supportsPassword) {
                        PasswordSignInForm(
                            enabled = state.canPasswordSignIn,
                            onPasswordSignIn = onPasswordSignIn,
                        )
                    }
                    state.message?.let { message ->
                        Spacer(Modifier.height(10.dp))
                        Text(
                            text = message,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                }

                ConnectionPhase.AUTHENTICATING -> {
                    StatusHeading(
                        text = stringResource(R.string.waiting_for_browser_sign_in),
                        loading = true,
                    )
                    EndpointDetails(state)
                }

                ConnectionPhase.READY -> {
                    StatusHeading(text = stringResource(R.string.ready_for_sessions))
                    EndpointDetails(state)
                }

                ConnectionPhase.UNAVAILABLE -> {
                    StatusHeading(
                        text = stringResource(R.string.connection_failed),
                        isError = true,
                    )
                    Spacer(Modifier.height(6.dp))
                    Text(
                        text = state.message.orEmpty(),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }

                ConnectionPhase.IDLE,
                ConnectionPhase.INVALID_ENDPOINT,
                -> Unit
            }
        }
    }
}

@Composable
private fun PasswordSignInForm(
    enabled: Boolean,
    onPasswordSignIn: (String, String) -> Unit,
) {
    var username by remember { mutableStateOf(DEFAULT_HERMES_USERNAME) }
    var password by remember { mutableStateOf("") }

    Spacer(Modifier.height(14.dp))
    HermesConnectionField(
        value = username,
        onValueChange = { username = it },
        label = stringResource(R.string.username),
        enabled = enabled,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Text),
        modifier = Modifier.testTag("username-input"),
    )
    Spacer(Modifier.height(10.dp))
    HermesConnectionField(
        value = password,
        onValueChange = { password = it },
        label = stringResource(R.string.password),
        enabled = enabled,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        visualTransformation = PasswordVisualTransformation(),
        modifier = Modifier.testTag("password-input"),
    )
    Spacer(Modifier.height(12.dp))
    HermesConnectionAction(
        label = stringResource(R.string.sign_in),
        enabled = enabled && username.isNotBlank() && password.isNotEmpty(),
        onClick = {
            val submittedPassword = password
            password = ""
            onPasswordSignIn(username, submittedPassword)
        },
        modifier = Modifier.testTag("password-sign-in"),
    )
}

@Composable
private fun HermesConnectionField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    modifier: Modifier = Modifier,
    placeholder: String = "",
    enabled: Boolean = true,
    keyboardOptions: KeyboardOptions = KeyboardOptions.Default,
    visualTransformation: VisualTransformation = VisualTransformation.None,
    isError: Boolean = false,
    supportingText: String? = null,
) {
    val borderColor = when {
        isError -> MaterialTheme.colorScheme.error
        else -> MaterialTheme.colorScheme.outline
    }
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = label.uppercase(),
            fontFamily = FontFamily.Monospace,
            fontSize = 10.sp,
            letterSpacing = 0.55.sp,
            color = if (isError) {
                MaterialTheme.colorScheme.error
            } else {
                MaterialTheme.colorScheme.onSurfaceVariant
            },
        )
        Spacer(Modifier.height(6.dp))
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            modifier = modifier.fillMaxWidth(),
            enabled = enabled,
            singleLine = true,
            keyboardOptions = keyboardOptions,
            visualTransformation = visualTransformation,
            textStyle = MaterialTheme.typography.bodyMedium.copy(
                color = MaterialTheme.colorScheme.onSurface,
                fontFamily = FontFamily.Monospace,
            ),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            decorationBox = { innerTextField ->
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 48.dp)
                        .background(MaterialTheme.colorScheme.surfaceVariant, ConnectionControlShape)
                        .border(1.dp, borderColor, ConnectionControlShape)
                        .padding(horizontal = 12.dp, vertical = 12.dp),
                    contentAlignment = Alignment.CenterStart,
                ) {
                    if (value.isEmpty() && placeholder.isNotEmpty()) {
                        Text(
                            text = placeholder,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    innerTextField()
                }
            },
        )
        supportingText?.takeIf(String::isNotBlank)?.let { message ->
            Spacer(Modifier.height(5.dp))
            Text(
                text = message,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
    }
}

@Composable
private fun HermesConnectionAction(
    label: String,
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    loading: Boolean = false,
) {
    val actionModifier = modifier
        .fillMaxWidth()
        .heightIn(min = 48.dp)
        .alpha(if (enabled) 1f else 0.42f)
        .semantics { role = Role.Button }
        .clickable(enabled = enabled, onClick = onClick)
    Surface(
        modifier = actionModifier,
        shape = ConnectionControlShape,
        color = MaterialTheme.colorScheme.primary,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.Center,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (loading) {
                CircularProgressIndicator(
                    modifier = Modifier.size(17.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.onPrimary,
                )
                Spacer(Modifier.width(9.dp))
            }
            Text(
                text = label,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onPrimary,
            )
        }
    }
}

@Composable
private fun StatusHeading(
    text: String,
    loading: Boolean = false,
    isError: Boolean = false,
) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        if (loading) {
            CircularProgressIndicator(
                modifier = Modifier.size(17.dp),
                strokeWidth = 2.dp,
                color = MaterialTheme.colorScheme.primary,
            )
            Spacer(Modifier.width(9.dp))
        }
        Text(
            text = text,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold,
            color = if (isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
        )
    }
}

@Composable
private fun EndpointDetails(state: ConnectionUiState) {
    Spacer(Modifier.height(11.dp))
    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
    Spacer(Modifier.height(7.dp))
    DetailRow(
        label = stringResource(R.string.endpoint_label),
        value = state.canonicalEndpoint.orEmpty(),
    )
    DetailRow(
        label = stringResource(R.string.gateway_service_label),
        value = if (state.gatewayRunning) {
            stringResource(R.string.gateway_service_running)
        } else {
            stringResource(R.string.gateway_service_not_running)
        },
    )
}

@Composable
private fun DetailRow(label: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.width(18.dp))
        Text(
            text = value,
            modifier = Modifier.weight(1f),
            style = MaterialTheme.typography.bodySmall,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurface,
        )
    }
}

@Composable
private fun ConnectionEyebrow(
    text: String,
    isError: Boolean = false,
) {
    Text(
        text = text,
        fontFamily = FontFamily.Monospace,
        fontSize = 10.sp,
        fontWeight = FontWeight.Medium,
        letterSpacing = 0.75.sp,
        color = if (isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary,
    )
}
