package app.hermesmobile

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import app.hermesmobile.auth.BrowserLauncher
import app.hermesmobile.auth.EncryptedTokenVault
import app.hermesmobile.auth.GatewayPasswordSignIn
import app.hermesmobile.auth.NativeOAuthCoordinator
import app.hermesmobile.auth.NativeSignIn
import app.hermesmobile.auth.PasswordSignIn
import app.hermesmobile.auth.TokenVault
import app.hermesmobile.connection.ConnectionPhase
import app.hermesmobile.connection.ConnectionScreen
import app.hermesmobile.connection.ConnectionViewModel
import app.hermesmobile.connection.EndpointStore
import app.hermesmobile.connection.SharedPreferencesEndpointStore
import app.hermesmobile.pairing.AuthenticatedPairingOwnerActions
import app.hermesmobile.pairing.DevicePairingScreen
import app.hermesmobile.pairing.DevicePairingViewModel
import app.hermesmobile.protocol.GatewayDiscovery
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.HermesStatusClient
import app.hermesmobile.protocol.auth.NativeAuthClient
import app.hermesmobile.protocol.auth.HermesSessionCookieJar
import app.hermesmobile.protocol.gateway.GatewayWebSocketClient
import app.hermesmobile.protocol.gateway.SessionControllerClient
import app.hermesmobile.protocol.pairing.DevicePairingClient
import app.hermesmobile.protocol.sessions.SessionsRestClient
import app.hermesmobile.sessions.AuthenticatedWebSocketTicketProvider
import app.hermesmobile.sessions.AuthenticatedSessionsRepository
import app.hermesmobile.sessions.GatewaySessionRealtimeSource
import app.hermesmobile.sessions.GatewaySessionControlSource
import app.hermesmobile.sessions.ScopedWebSocketTicketMint
import app.hermesmobile.sessions.ScopedCookieWebSocketTicketMint
import app.hermesmobile.sessions.SessionBrowserScreen
import app.hermesmobile.sessions.SessionBrowserSource
import app.hermesmobile.sessions.SessionBrowserViewModel
import app.hermesmobile.sessions.SessionControlSource
import app.hermesmobile.sessions.SessionRealtimeSource
import app.hermesmobile.sessions.SharedPreferencesClientInstanceIdStore
import app.hermesmobile.sessions.TokenRefresh
import app.hermesmobile.sessions.WebSocketTicketMint
import app.hermesmobile.sessions.CookieWebSocketTicketMint
import app.hermesmobile.ui.theme.HermesMobileTheme
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val sessionCookieJar = HermesSessionCookieJar()
        val httpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)
            .callTimeout(15, TimeUnit.SECONDS)
            .cookieJar(sessionCookieJar)
            .build()
        val discovery = HermesStatusClient(httpClient)
        val mainHandler = Handler(Looper.getMainLooper())
        val appContext = applicationContext
        val authClient = NativeAuthClient(httpClient)
        val nativeSignIn = NativeOAuthCoordinator(
            authClient = authClient,
            browserLauncher = BrowserLauncher { url ->
                mainHandler.post {
                    appContext.startActivity(
                        Intent(Intent.ACTION_VIEW, Uri.parse(url.toString())).apply {
                            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        },
                    )
                }
            },
        )
        val passwordSignIn = GatewayPasswordSignIn(authClient)
        val tokenVault = EncryptedTokenVault(appContext)
        val endpointStore = SharedPreferencesEndpointStore(appContext)
        val clientInstanceIdStore = SharedPreferencesClientInstanceIdStore(appContext)
        val viewModel = ViewModelProvider(
            this,
            ConnectionViewModelFactory(
                discovery,
                nativeSignIn,
                passwordSignIn,
                tokenVault,
                endpointStore,
            ),
        )[ConnectionViewModel::class.java]

        setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            var showSessions by rememberSaveable { mutableStateOf(false) }
            var showPairing by rememberSaveable { mutableStateOf(false) }
            LaunchedEffect(state.phase, state.canonicalEndpoint) {
                if (state.phase == ConnectionPhase.READY && state.canonicalEndpoint != null) {
                    showSessions = true
                }
            }
            HermesMobileTheme {
                val endpoint = state.canonicalEndpoint
                    ?.let(GatewayEndpoint::parse)
                    ?.getOrNull()
                if (showSessions && endpoint != null) {
                    if (showPairing) {
                        val pairingViewModel = remember(endpoint.baseUrl.toString()) {
                            ViewModelProvider(
                                this@MainActivity,
                                DevicePairingViewModelFactory(
                                    AuthenticatedPairingOwnerActions(
                                        endpoint = endpoint,
                                        tokenVault = tokenVault,
                                        pairingApi = DevicePairingClient(httpClient),
                                        tokenRefresh = TokenRefresh(authClient::refresh),
                                    ),
                                ),
                            )[
                                "device-pairing:${endpoint.baseUrl}",
                                DevicePairingViewModel::class.java,
                            ]
                        }
                        val pairingState by pairingViewModel.state.collectAsStateWithLifecycle()
                        DisposableEffect(pairingViewModel) {
                            pairingViewModel.onVisible()
                            onDispose(pairingViewModel::onHidden)
                        }
                        DevicePairingScreen(
                            state = pairingState,
                            onBack = { showPairing = false },
                            onPairingCodeChanged = pairingViewModel::onPairingCodeChanged,
                            onWorkspaceIdChanged = pairingViewModel::onWorkspaceIdChanged,
                            onAgentIdChanged = pairingViewModel::onAgentIdChanged,
                            onDeviceDisplayNameChanged =
                                pairingViewModel::onDeviceDisplayNameChanged,
                            onRequestControlScopeChanged =
                                pairingViewModel::onRequestControlScopeChanged,
                            onFingerprintVerificationChanged =
                                pairingViewModel::onFingerprintVerificationChanged,
                            onClaim = pairingViewModel::claim,
                            onConfirm = pairingViewModel::confirm,
                            onRejectFingerprint = pairingViewModel::rejectFingerprint,
                            onCancel = pairingViewModel::cancel,
                            onRevoke = pairingViewModel::revoke,
                            onReset = pairingViewModel::reset,
                            onRetryPending = pairingViewModel::retryPending,
                        )
                    } else {
                    val sessionViewModel = remember(
                        endpoint.baseUrl.toString(),
                        state.requiresAuthentication,
                    ) {
                        val source = AuthenticatedSessionsRepository(
                            endpoint = endpoint,
                            tokenVault = tokenVault,
                            sessionsApi = SessionsRestClient(httpClient),
                            tokenRefresh = TokenRefresh(authClient::refresh),
                            authenticationRequired = state.requiresAuthentication,
                        )
                        val socketClient = GatewayWebSocketClient(httpClient)
                        val clientInstanceId = clientInstanceIdStore.loadOrCreate()
                        val ticketProvider = AuthenticatedWebSocketTicketProvider(
                            endpoint = endpoint,
                            tokenVault = tokenVault,
                            tokenRefresh = TokenRefresh(authClient::refresh),
                            ticketMint = WebSocketTicketMint { ticketEndpoint, accessToken ->
                                authClient.mintWebSocketTicket(ticketEndpoint, accessToken)
                            },
                            scopedTicketMint = ScopedWebSocketTicketMint {
                                    ticketEndpoint,
                                    accessToken,
                                    request,
                                ->
                                authClient.mintWebSocketTicket(
                                    ticketEndpoint,
                                    accessToken,
                                    request,
                                )
                            },
                            cookieTicketMint = CookieWebSocketTicketMint { ticketEndpoint ->
                                authClient.mintCookieWebSocketTicket(ticketEndpoint)
                            },
                            scopedCookieTicketMint = ScopedCookieWebSocketTicketMint {
                                    ticketEndpoint,
                                    request,
                                ->
                                authClient.mintCookieWebSocketTicket(ticketEndpoint, request)
                            },
                        )
                        val realtimeSource = GatewaySessionRealtimeSource(
                            endpoint = endpoint,
                            ticketProvider = ticketProvider,
                            clientInstanceId = clientInstanceId,
                            socketClient = socketClient,
                            transcriptSource = source,
                        )
                        val controlSource = GatewaySessionControlSource(
                            endpoint = endpoint,
                            ticketSource = ticketProvider,
                            controllerClient = SessionControllerClient(socketClient),
                            clientInstanceId = clientInstanceId,
                        )
                        ViewModelProvider(
                            this@MainActivity,
                            SessionBrowserViewModelFactory(
                                source,
                                realtimeSource,
                                controlSource,
                            ),
                        )[
                            "sessions:${endpoint.baseUrl}",
                            SessionBrowserViewModel::class.java,
                        ]
                    }
                    val sessionState by sessionViewModel.state.collectAsStateWithLifecycle()
                    LaunchedEffect(sessionViewModel) {
                        sessionViewModel.start()
                    }
                    SessionBrowserScreen(
                        state = sessionState,
                        onOpenSession = sessionViewModel::openSession,
                        onBack = sessionViewModel::backToSessions,
                        onRefresh = sessionViewModel::refresh,
                        onLoadMore = sessionViewModel::loadMoreSessions,
                        onLoadOlder = sessionViewModel::loadOlderMessages,
                        onReconnect = {
                            sessionViewModel.disconnect()
                            showSessions = false
                            viewModel.returnToConnection()
                        },
                        onDraftChanged = sessionViewModel::onDraftChanged,
                        onSend = sessionViewModel::sendPrompt,
                        onStop = sessionViewModel::stopCurrentTurn,
                        onGuidanceDraftChanged = sessionViewModel::onGuidanceDraftChanged,
                        onSubmitGuidance = sessionViewModel::submitGuidance,
                        onPendingChoice = sessionViewModel::selectPendingChoice,
                        onPendingOtherChanged = sessionViewModel::updatePendingOtherDraft,
                        onPendingSubmit = sessionViewModel::submitPendingInput,
                        onPendingConfirm = sessionViewModel::confirmPendingChoice,
                        onPendingCancelConfirmation = sessionViewModel::cancelPendingConfirmation,
                        onRetryControl = sessionViewModel::retryControlConnection,
                        onOpenPairing = { showPairing = true },
                    )
                    }
                } else {
                    ConnectionScreen(
                        state = state,
                        onEndpointChanged = viewModel::onEndpointChanged,
                        onConnect = viewModel::connect,
                        onSignIn = viewModel::signIn,
                        onPasswordSignIn = viewModel::signInWithPassword,
                    )
                }
            }
        }
    }
}

private class DevicePairingViewModelFactory(
    private val actions: app.hermesmobile.pairing.PairingOwnerActions,
) : ViewModelProvider.Factory {
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        require(modelClass.isAssignableFrom(DevicePairingViewModel::class.java)) {
            "Unsupported ViewModel: ${modelClass.name}"
        }
        @Suppress("UNCHECKED_CAST")
        return DevicePairingViewModel(actions) as T
    }
}

private class SessionBrowserViewModelFactory(
    private val source: SessionBrowserSource,
    private val realtimeSource: SessionRealtimeSource,
    private val controlSource: SessionControlSource,
) : ViewModelProvider.Factory {
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        require(modelClass.isAssignableFrom(SessionBrowserViewModel::class.java)) {
            "Unsupported ViewModel: ${modelClass.name}"
        }
        @Suppress("UNCHECKED_CAST")
        return SessionBrowserViewModel(source, realtimeSource, controlSource) as T
    }
}

private class ConnectionViewModelFactory(
    private val discovery: GatewayDiscovery,
    private val nativeSignIn: NativeSignIn,
    private val passwordSignIn: PasswordSignIn,
    private val tokenVault: TokenVault,
    private val endpointStore: EndpointStore,
) : ViewModelProvider.Factory {
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        require(modelClass.isAssignableFrom(ConnectionViewModel::class.java)) {
            "Unsupported ViewModel: ${modelClass.name}"
        }
        @Suppress("UNCHECKED_CAST")
        return ConnectionViewModel(
            discovery = discovery,
            nativeSignIn = nativeSignIn,
            passwordSignIn = passwordSignIn,
            tokenVault = tokenVault,
            endpointStore = endpointStore,
        ) as T
    }
}
