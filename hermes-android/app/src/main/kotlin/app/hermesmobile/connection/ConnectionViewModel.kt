package app.hermesmobile.connection

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import app.hermesmobile.auth.NativeSignIn
import app.hermesmobile.auth.NativeSignInResult
import app.hermesmobile.auth.PasswordSignIn
import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.DiscoveryResult
import app.hermesmobile.protocol.GatewayDiscovery
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeTokens
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow


enum class ConnectionPhase {
    IDLE,
    CHECKING,
    INVALID_ENDPOINT,
    AUTHENTICATION_REQUIRED,
    AUTHENTICATING,
    READY,
    UNAVAILABLE,
}

data class ConnectionUiState(
    val endpointInput: String = "",
    val phase: ConnectionPhase = ConnectionPhase.IDLE,
    val canonicalEndpoint: String? = null,
    val hermesVersion: String? = null,
    val gatewayRunning: Boolean = false,
    val supportsNativePkce: Boolean = false,
    val supportsPassword: Boolean = false,
    val requiresAuthentication: Boolean = false,
    val message: String? = null,
) {
    val isChecking: Boolean
        get() = phase == ConnectionPhase.CHECKING

    val isAuthenticating: Boolean
        get() = phase == ConnectionPhase.AUTHENTICATING

    val canConnect: Boolean
        get() = endpointInput.isNotBlank() && !isChecking && !isAuthenticating

    val canSignIn: Boolean
        get() = phase == ConnectionPhase.AUTHENTICATION_REQUIRED && supportsNativePkce

    val canPasswordSignIn: Boolean
        get() = phase == ConnectionPhase.AUTHENTICATION_REQUIRED && supportsPassword
}

class ConnectionViewModel(
    private val discovery: GatewayDiscovery,
    private val nativeSignIn: NativeSignIn = UnsupportedNativeSignIn,
    private val passwordSignIn: PasswordSignIn = UnsupportedPasswordSignIn,
    private val tokenVault: TokenVault = NoOpTokenVault,
    private val endpointStore: EndpointStore = NoOpEndpointStore,
) : ViewModel() {
    private val mutableState = MutableStateFlow(
        ConnectionUiState(endpointInput = DEFAULT_GATEWAY_ENDPOINT),
    )
    val state: StateFlow<ConnectionUiState> = mutableState.asStateFlow()

    private var discoveryJob: Job? = null
    private var authenticationJob: Job? = null
    private var requestGeneration = 0L

    init {
        restoreEndpoint()
    }

    fun onEndpointChanged(value: String) {
        discoveryJob?.cancel()
        authenticationJob?.cancel()
        requestGeneration += 1
        mutableState.value = ConnectionUiState(endpointInput = value)
    }

    fun connect() {
        val input = mutableState.value.endpointInput
        val endpointResult = GatewayEndpoint.parse(input)
        if (endpointResult.isFailure) {
            discoveryJob?.cancel()
            authenticationJob?.cancel()
            requestGeneration += 1
            mutableState.value = mutableState.value.copy(
                phase = ConnectionPhase.INVALID_ENDPOINT,
                canonicalEndpoint = null,
                hermesVersion = null,
                gatewayRunning = false,
                supportsNativePkce = false,
                supportsPassword = false,
                message = endpointResult.exceptionOrNull()?.message
                    ?: "Enter a valid Hermes server address.",
            )
            return
        }

        val endpoint = endpointResult.getOrThrow()
        discoveryJob?.cancel()
        authenticationJob?.cancel()
        val generation = ++requestGeneration
        mutableState.value = mutableState.value.copy(
            phase = ConnectionPhase.CHECKING,
            canonicalEndpoint = endpoint.baseUrl.toString(),
            hermesVersion = null,
            gatewayRunning = false,
            supportsNativePkce = false,
            supportsPassword = false,
            message = null,
        )

        discoveryJob = viewModelScope.launch {
            val result = discovery.discover(endpoint)
            if (generation != requestGeneration) return@launch
            val discovered = result.toUiState(
                endpointInput = input,
                canonicalEndpoint = endpoint.baseUrl.toString(),
            )
            if (result is DiscoveryResult.Reachable) {
                runCatching { endpointStore.save(endpoint.baseUrl.toString()) }
            }
            val hasStoredCredentials = discovered.requiresAuthentication &&
                runCatching { tokenVault.load(endpoint.baseUrl.toString()) != null }
                    .getOrDefault(false)
            mutableState.value = if (hasStoredCredentials) {
                discovered.copy(phase = ConnectionPhase.READY)
            } else {
                discovered
            }
        }
    }

    private fun restoreEndpoint() {
        val stored = runCatching { endpointStore.load() }.getOrNull()
            ?.takeIf(String::isNotBlank)
            ?: return
        val endpoint = GatewayEndpoint.parse(stored).getOrNull()
        if (endpoint == null) {
            runCatching(endpointStore::clear)
            return
        }
        mutableState.value = ConnectionUiState(
            endpointInput = endpoint.baseUrl.toString(),
        )
        connect()
    }

    fun returnToConnection() {
        discoveryJob?.cancel()
        authenticationJob?.cancel()
        requestGeneration += 1
        mutableState.value = ConnectionUiState(
            endpointInput = mutableState.value.endpointInput,
        )
    }

    fun signIn() {
        val current = mutableState.value
        if (!current.canSignIn) return
        authenticate(current) { endpoint -> nativeSignIn.signIn(endpoint) }
    }

    fun signInWithPassword(username: String, password: String) {
        val current = mutableState.value
        if (!current.canPasswordSignIn || username.isBlank() || password.isEmpty()) return
        authenticate(current) { endpoint ->
            passwordSignIn.signIn(endpoint, username, password)
        }
    }

    private fun authenticate(
        current: ConnectionUiState,
        signIn: suspend (GatewayEndpoint) -> NativeSignInResult,
    ) {
        val endpoint = current.canonicalEndpoint
            ?.let(GatewayEndpoint::parse)
            ?.getOrNull()
            ?: return

        authenticationJob?.cancel()
        val generation = ++requestGeneration
        mutableState.value = current.copy(
            phase = ConnectionPhase.AUTHENTICATING,
            message = null,
        )
        authenticationJob = viewModelScope.launch {
            when (val result = signIn(endpoint)) {
                is NativeSignInResult.Authenticated -> {
                    if (generation != requestGeneration) return@launch
                    val stored = runCatching {
                        tokenVault.save(endpoint.baseUrl.toString(), result.tokens)
                    }.isSuccess
                    mutableState.value = mutableState.value.copy(
                        phase = if (stored) {
                            ConnectionPhase.READY
                        } else {
                            ConnectionPhase.AUTHENTICATION_REQUIRED
                        },
                        message = if (stored) {
                            null
                        } else {
                            "Secure credentials could not be stored on this device."
                        },
                    )
                }

                is NativeSignInResult.Failed -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = ConnectionPhase.AUTHENTICATION_REQUIRED,
                        message = result.summary,
                    )
                }
            }
        }
    }

    private fun DiscoveryResult.toUiState(
        endpointInput: String,
        canonicalEndpoint: String,
    ): ConnectionUiState = when (this) {
        is DiscoveryResult.Reachable -> ConnectionUiState(
            endpointInput = endpointInput,
            phase = if (status.authRequired) {
                ConnectionPhase.AUTHENTICATION_REQUIRED
            } else {
                ConnectionPhase.READY
            },
            canonicalEndpoint = canonicalEndpoint,
            hermesVersion = status.version,
            gatewayRunning = status.gatewayRunning,
            supportsNativePkce = status.supportsNativePkce,
            supportsPassword = status.supportsPassword,
            requiresAuthentication = status.authRequired,
        )

        is DiscoveryResult.HttpFailure -> ConnectionUiState(
            endpointInput = endpointInput,
            phase = if (statusCode == 401 || statusCode == 403) {
                ConnectionPhase.AUTHENTICATION_REQUIRED
            } else {
                ConnectionPhase.UNAVAILABLE
            },
            canonicalEndpoint = canonicalEndpoint,
            requiresAuthentication = statusCode == 401 || statusCode == 403,
            message = summary,
        )

        is DiscoveryResult.InvalidResponse -> ConnectionUiState(
            endpointInput = endpointInput,
            phase = ConnectionPhase.UNAVAILABLE,
            canonicalEndpoint = canonicalEndpoint,
            message = summary,
        )

        is DiscoveryResult.NetworkFailure -> ConnectionUiState(
            endpointInput = endpointInput,
            phase = ConnectionPhase.UNAVAILABLE,
            canonicalEndpoint = canonicalEndpoint,
            message = summary,
        )
    }
}

private object UnsupportedNativeSignIn : NativeSignIn {
    override suspend fun signIn(endpoint: GatewayEndpoint): NativeSignInResult =
        NativeSignInResult.Failed("This Hermes server does not offer native secure sign-in.")
}

private object UnsupportedPasswordSignIn : PasswordSignIn {
    override suspend fun signIn(
        endpoint: GatewayEndpoint,
        username: String,
        password: String,
    ): NativeSignInResult =
        NativeSignInResult.Failed("This Hermes server does not offer password sign-in.")
}

private object NoOpTokenVault : TokenVault {
    override fun save(endpointId: String, tokens: NativeTokens) = Unit

    override fun load(endpointId: String): NativeTokens? = null

    override fun clear(endpointId: String) = Unit
}

private object NoOpEndpointStore : EndpointStore {
    override fun load(): String? = null

    override fun save(canonicalEndpoint: String) = Unit

    override fun clear() = Unit
}
