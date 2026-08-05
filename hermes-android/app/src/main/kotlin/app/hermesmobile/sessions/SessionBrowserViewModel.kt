package app.hermesmobile.sessions

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.MobileControlMethods
import app.hermesmobile.protocol.gateway.PendingInputRespondResponse
import app.hermesmobile.protocol.gateway.PromptSubmitResponse
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyAnswer
import app.hermesmobile.protocol.gateway.SessionCommandState
import app.hermesmobile.protocol.gateway.SessionCommandStatus
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionControllerResult
import app.hermesmobile.protocol.gateway.SessionInterruptResponse
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.protocol.gateway.SessionSteerResponse
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.sessions.control.CommandAction
import app.hermesmobile.sessions.control.CommandState
import app.hermesmobile.sessions.control.CommandStateReducer
import app.hermesmobile.sessions.control.ComposerAction
import app.hermesmobile.sessions.control.ComposerState
import app.hermesmobile.sessions.control.ComposerStateReducer
import app.hermesmobile.sessions.control.ComposerSubmission
import app.hermesmobile.sessions.control.ControlAction
import app.hermesmobile.sessions.control.ControlLossReason
import app.hermesmobile.sessions.control.ControlState
import app.hermesmobile.sessions.control.ControlStateReducer
import app.hermesmobile.sessions.control.PendingInputInteractionAction
import app.hermesmobile.sessions.control.PendingInputInteractionReducer
import app.hermesmobile.sessions.control.PendingInputInteractionState
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.launch

enum class SessionBrowserPhase {
    IDLE,
    LOADING_SESSIONS,
    LIST,
    LOADING_TRANSCRIPT,
    TRANSCRIPT,
    AUTHENTICATION_REQUIRED,
    ERROR,
}

enum class RealtimeControlStatus {
    SERVER_UPGRADE_REQUIRED,
    OBSERVER,
    CONTROLLER,
}

data class SessionBrowserUiState(
    val phase: SessionBrowserPhase = SessionBrowserPhase.IDLE,
    val sessions: List<SessionProjection> = emptyList(),
    val hasMoreSessions: Boolean = false,
    val isLoadingMoreSessions: Boolean = false,
    val hasOlderMessages: Boolean = false,
    val isLoadingOlderMessages: Boolean = false,
    val selectedSession: SessionProjection? = null,
    val transcript: SessionTranscript? = null,
    val realtime: RealtimeSessionProjection? = null,
    val control: ControlState = ControlState(),
    val commands: CommandState = CommandState(),
    val composer: ComposerState = ComposerState(),
    val guidance: SessionGuidanceState = SessionGuidanceState(),
    val pendingInteraction: PendingInputInteractionState = PendingInputInteractionState(),
    val controlAvailableMethods: Set<String> = emptySet(),
    val controlStatus: RealtimeControlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
    val realtimeConnectionStatus: RealtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
    val realtimeMessage: String? = null,
    val interruptRequestId: ClientRequestId? = null,
    val isPendingInputSnapshotRefreshing: Boolean = false,
    val isRefreshing: Boolean = false,
    val message: String? = null,
) {
    val isSessionListVisible: Boolean
        get() = phase in setOf(
            SessionBrowserPhase.LOADING_SESSIONS,
            SessionBrowserPhase.LIST,
            SessionBrowserPhase.AUTHENTICATION_REQUIRED,
            SessionBrowserPhase.ERROR,
        )

    val canEditComposer: Boolean
        get() = phase == SessionBrowserPhase.TRANSCRIPT &&
            control.canMutate &&
            realtime != null &&
            !isPendingInputSnapshotRefreshing &&
            pendingInteraction.requestId == null

    val canSend: Boolean
        get() = canEditComposer &&
            MobileControlMethods.PROMPT_SUBMIT in controlAvailableMethods &&
            composer.submitted == null

    val isInterrupting: Boolean
        get() = interruptRequestId != null

    val canStop: Boolean
        get() = phase == SessionBrowserPhase.TRANSCRIPT &&
            control.canMutate &&
            MobileControlMethods.SESSION_INTERRUPT in controlAvailableMethods &&
            realtime?.running == true &&
            !isInterrupting

    val canGuide: Boolean
        get() = phase == SessionBrowserPhase.TRANSCRIPT &&
            control.canMutate &&
            MobileControlMethods.SESSION_STEER in controlAvailableMethods &&
            realtime?.running == true &&
            !isPendingInputSnapshotRefreshing &&
            pendingInteraction.requestId == null &&
            !isInterrupting &&
            guidance.inFlightRequestId == null

    val canRespondToPendingInput: Boolean
        get() {
            if (!control.canMutate) return false
            val pending = (
                control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller
                )?.lease?.pendingInput ?: return false
            if (pending.requestId != pendingInteraction.requestId) return false
            val requiredMethod = when (pending) {
                is SessionPendingInput.Approval -> MobileControlMethods.APPROVAL_RESPOND
                is SessionPendingInput.Clarify -> MobileControlMethods.CLARIFY_RESPOND
            }
            return requiredMethod in controlAvailableMethods
        }
}

class SessionBrowserViewModel(
    private val source: SessionBrowserSource,
    private val realtimeSource: SessionRealtimeSource = SessionRealtimeSource.None,
    private val controlSource: SessionControlSource? = null,
    private val requestIdFactory: () -> ClientRequestId = {
        ClientRequestId(java.util.UUID.randomUUID().toString())
    },
    private val turnIdFactory: () -> ClientTurnId = {
        ClientTurnId(java.util.UUID.randomUUID().toString())
    },
    private val clockEpochMs: () -> Long = { System.currentTimeMillis() },
    private val leaseRenewLeadMillis: Long = DEFAULT_LEASE_RENEW_LEAD_MILLIS,
) : ViewModel() {
    init {
        require(leaseRenewLeadMillis >= 0) { "Lease renew lead time must not be negative." }
    }

    private val mutableState = MutableStateFlow(SessionBrowserUiState())
    val state: StateFlow<SessionBrowserUiState> = mutableState.asStateFlow()

    private var requestJob: Job? = null
    private var realtimeJob: Job? = null
    private var controlOpenJob: Job? = null
    private var promptJob: Job? = null
    private var controlEventsJob: Job? = null
    private var leaseRenewJob: Job? = null
    private var interruptJob: Job? = null
    private var guidanceJob: Job? = null
    private var pendingInputJob: Job? = null
    private var controlChannel: SessionControlChannel? = null
    private var controlRuntimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId? = null
    private var controlOpeningRuntimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId? = null
    private var controlGeneration = 0L
    private var guidanceGeneration = 0L
    private var pendingInputGeneration = 0L
    private var pendingInputControlGeneration = 0L
    private var leaseRenewGeneration = 0L
    private val controlReducer = ControlStateReducer()
    private val commandReducer = CommandStateReducer()
    private val composerReducer = ComposerStateReducer()
    private val pendingInteractionReducer = PendingInputInteractionReducer()
    private var requestGeneration = 0L
    private var nextSessionOffset = 0
    private var started = false

    fun start() {
        if (started) return
        started = true
        loadSessions(showLoading = true)
    }

    fun retryControlConnection() {
        val current = mutableState.value
        val session = current.selectedSession ?: return
        val projection = current.realtime ?: return
        if (current.phase != SessionBrowserPhase.TRANSCRIPT) return
        if (current.control.mode !is app.hermesmobile.sessions.control.ControlMode.Lost) return
        mutableState.value = current.copy(
            control = controlReducer.reduce(
                current.control,
                ControlAction.ObserverConnected,
            ),
            controlStatus = RealtimeControlStatus.OBSERVER,
        )
        startControlSessionIfPossible(session, projection)
    }

    fun refresh() {
        if (mutableState.value.isSessionListVisible) {
            loadSessions(showLoading = mutableState.value.sessions.isEmpty())
        } else {
            mutableState.value.selectedSession?.sessionKey?.let(::openSession)
        }
    }

    fun updatePendingOtherDraft(text: String) {
        val current = mutableState.value
        if (
            !current.control.canMutate ||
            current.pendingInteraction.requestId == null ||
            current.pendingInteraction.inFlightClientRequestId != null
        ) {
            return
        }
        mutableState.value = current.copy(
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                PendingInputInteractionAction.OtherDraftChanged(text),
            ),
        )
    }

    fun selectPendingChoice(choiceId: String) {
        val current = mutableState.value
        if (current.pendingInteraction.inFlightClientRequestId != null) return
        val pending = current.pendingInputForMutation() ?: return
        val requiresConfirmation = when (pending) {
            is SessionPendingInput.Approval -> {
                val choice = SessionApprovalChoice.fromWireValue(choiceId)
                    ?.takeIf(pending.choices::contains)
                    ?: return
                choice == SessionApprovalChoice.ALLOW_ALWAYS
            }
            is SessionPendingInput.Clarify -> {
                if (pending.choices.none { it.id == choiceId }) return
                false
            }
        }
        mutableState.value = current.copy(
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                PendingInputInteractionAction.ChoiceSelected(
                    choiceId = choiceId,
                    requiresConfirmation = requiresConfirmation,
                ),
            ),
        )
    }

    fun cancelPendingConfirmation() {
        val current = mutableState.value
        if (current.pendingInputForMutation() == null) return
        mutableState.value = current.copy(
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                PendingInputInteractionAction.ConfirmationCancelled,
            ),
        )
    }

    fun confirmPendingChoice() {
        val current = mutableState.value
        val pending = current.pendingInputForMutation() as? SessionPendingInput.Approval ?: return
        if (
            current.pendingInteraction.selectedChoiceId != SessionApprovalChoice.ALLOW_ALWAYS.wireValue ||
            SessionApprovalChoice.ALLOW_ALWAYS !in pending.choices ||
            !current.pendingInteraction.requiresConfirmation
        ) {
            return
        }
        mutableState.value = current.copy(
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                PendingInputInteractionAction.ConfirmationGranted,
            ),
        )
        submitPendingInput()
    }

    fun submitPendingInput() {
        val current = mutableState.value
        val channel = controlChannel ?: return
        val runtimeSessionId = controlRuntimeSessionId ?: return
        val lease = (current.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller)
            ?.lease
            ?: return
        val pending = current.pendingInputForMutation() ?: return
        if (!current.pendingInteraction.canSubmit) return

        val approvalChoice: SessionApprovalChoice?
        val clarifyAnswer: SessionClarifyAnswer?
        when (pending) {
            is SessionPendingInput.Approval -> {
                approvalChoice = SessionApprovalChoice.fromWireValue(
                    current.pendingInteraction.selectedChoiceId,
                )?.takeIf(pending.choices::contains) ?: return
                clarifyAnswer = null
            }
            is SessionPendingInput.Clarify -> {
                approvalChoice = null
                clarifyAnswer = current.pendingInteraction.selectedChoiceId?.let { choiceId ->
                    pending.choices.firstOrNull { it.id == choiceId } ?: return
                    SessionClarifyAnswer.Choice(choiceId)
                } ?: current.pendingInteraction.otherDraft
                    .takeIf(String::isNotBlank)
                    ?.takeIf { pending.allowOther }
                    ?.let(SessionClarifyAnswer::Other)
                    ?: return
            }
        }

        val clientRequestId = current.pendingInteraction.inFlightClientRequestId
            ?: requestIdFactory()
        pendingInputControlGeneration = controlGeneration
        val generation = ++pendingInputGeneration
        mutableState.value = current.copy(
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                PendingInputInteractionAction.SubmissionStarted(clientRequestId),
            ),
        )
        pendingInputJob = viewModelScope.launch {
            val result = when (pending) {
                is SessionPendingInput.Approval -> channel.respondApproval(
                    leaseId = lease.leaseId,
                    clientRequestId = clientRequestId,
                    requestId = pending.requestId,
                    choice = requireNotNull(approvalChoice),
                )
                is SessionPendingInput.Clarify -> channel.respondClarify(
                    leaseId = lease.leaseId,
                    clientRequestId = clientRequestId,
                    requestId = pending.requestId,
                    answer = requireNotNull(clarifyAnswer),
                )
            }
            if (!isCurrentPendingInputControl(channel, runtimeSessionId, generation)) return@launch
            val currentLeaseId = (
                mutableState.value.control.mode as?
                    app.hermesmobile.sessions.control.ControlMode.Controller
                )?.lease?.leaseId ?: return@launch
            if (currentLeaseId != lease.leaseId) {
                applyPendingInteractionBeforeSnapshotRefresh(
                    PendingInputInteractionAction.DeliveryUnknown(clientRequestId),
                )
                refreshPendingInputSnapshot(
                    channel = channel,
                    runtimeSessionId = runtimeSessionId,
                    leaseId = currentLeaseId,
                    generation = generation,
                )
                return@launch
            }
            when (result) {
                is SessionControllerResult.Success -> {
                    val expectedKind = when (pending) {
                        is SessionPendingInput.Approval ->
                            app.hermesmobile.protocol.gateway.PendingInputKind.APPROVAL
                        is SessionPendingInput.Clarify ->
                            app.hermesmobile.protocol.gateway.PendingInputKind.CLARIFY
                    }
                    if (
                        result.value.clientRequestId == clientRequestId &&
                        result.value.requestId == pending.requestId &&
                        result.value.kind == expectedKind
                    ) {
                        applyAcceptedPendingResponse(result.value, clientRequestId)
                    } else {
                        applyPendingInteractionBeforeSnapshotRefresh(
                            PendingInputInteractionAction.DeliveryUnknown(clientRequestId),
                        )
                    }
                    refreshPendingInputSnapshot(
                        channel = channel,
                        runtimeSessionId = runtimeSessionId,
                        leaseId = lease.leaseId,
                        generation = generation,
                    )
                }
                SessionControllerResult.Timeout,
                SessionControllerResult.Disconnected,
                SessionControllerResult.NotReady,
                SessionControllerResult.InvalidResponse,
                -> {
                    applyPendingInteractionBeforeSnapshotRefresh(
                        PendingInputInteractionAction.DeliveryUnknown(clientRequestId),
                    )
                    refreshPendingInputSnapshot(
                        channel = channel,
                        runtimeSessionId = runtimeSessionId,
                        leaseId = lease.leaseId,
                        generation = generation,
                    )
                }
                is SessionControllerResult.RpcFailure -> when (result.error.code) {
                    EFFECT_UNKNOWN_ERROR -> {
                        applyPendingInteractionBeforeSnapshotRefresh(
                            PendingInputInteractionAction.DeliveryUnknown(clientRequestId),
                        )
                        channel.commandStatus(
                            method = when (pending) {
                                is SessionPendingInput.Approval ->
                                    MobileControlMethods.APPROVAL_RESPOND
                                is SessionPendingInput.Clarify ->
                                    MobileControlMethods.CLARIFY_RESPOND
                            },
                            requestId = clientRequestId,
                        )
                        if (!isCurrentPendingInputControl(channel, runtimeSessionId, generation)) {
                            return@launch
                        }
                        refreshPendingInputSnapshot(
                            channel = channel,
                            runtimeSessionId = runtimeSessionId,
                            leaseId = lease.leaseId,
                            generation = generation,
                        )
                    }
                    PENDING_REQUEST_STALE_ERROR -> {
                        applyPendingInteractionBeforeSnapshotRefresh(
                            PendingInputInteractionAction.ResolvedElsewhere(clientRequestId),
                        )
                        refreshPendingInputSnapshot(
                            channel = channel,
                            runtimeSessionId = runtimeSessionId,
                            leaseId = lease.leaseId,
                            generation = generation,
                        )
                    }
                    CLIENT_REQUEST_CONFLICT_ERROR -> {
                        applyPendingInteractionBeforeSnapshotRefresh(
                            PendingInputInteractionAction.ReconciliationRequired(
                                clientRequestId = clientRequestId,
                                summary = "This response could not be reconciled. Refresh the request before trying again.",
                                clearAnswer = false,
                            ),
                        )
                        refreshPendingInputSnapshot(
                            channel = channel,
                            runtimeSessionId = runtimeSessionId,
                            leaseId = lease.leaseId,
                            generation = generation,
                        )
                    }
                    INVALID_PENDING_RESPONSE_ERROR -> {
                        applyPendingInteractionBeforeSnapshotRefresh(
                            PendingInputInteractionAction.ReconciliationRequired(
                                clientRequestId = clientRequestId,
                                summary = "The selected response is no longer allowed.",
                                clearAnswer = true,
                            ),
                        )
                        refreshPendingInputSnapshot(
                            channel = channel,
                            runtimeSessionId = runtimeSessionId,
                            leaseId = lease.leaseId,
                            generation = generation,
                        )
                    }
                    else -> applyPendingFailure(
                        clientRequestId,
                        "Hermes could not accept this response.",
                    )
                }
                SessionControllerResult.Unsupported -> applyPendingFailure(
                    clientRequestId,
                    "Hermes server does not support this response.",
                )
            }
        }
    }

    fun loadMoreSessions() {
        val current = mutableState.value
        if (
            current.phase != SessionBrowserPhase.LIST ||
            !current.hasMoreSessions ||
            current.isRefreshing ||
            current.isLoadingMoreSessions
        ) {
            return
        }
        requestJob?.cancel()
        val generation = ++requestGeneration
        val offset = nextSessionOffset
        mutableState.value = current.copy(
            isRefreshing = true,
            isLoadingMoreSessions = true,
            message = null,
        )
        requestJob = viewModelScope.launch {
            when (val result = source.loadSessions(offset = offset)) {
                is SessionRepositoryResult.Data -> {
                    if (generation != requestGeneration) return@launch
                    val page = result.value
                    nextSessionOffset = page.offset + page.sessions.size
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.LIST,
                        sessions = (current.sessions + page.sessions).distinctBy { it.sessionKey },
                        hasMoreSessions = page.sessions.isNotEmpty() &&
                            nextSessionOffset < page.total,
                        isRefreshing = false,
                        isLoadingMoreSessions = false,
                        message = null,
                    )
                }
                SessionRepositoryResult.AuthenticationRequired -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.AUTHENTICATION_REQUIRED,
                        sessions = emptyList(),
                        hasMoreSessions = false,
                        isRefreshing = false,
                        isLoadingMoreSessions = false,
                        message = AUTHENTICATION_MESSAGE,
                    )
                }
                is SessionRepositoryResult.Unavailable -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.ERROR,
                        isRefreshing = false,
                        isLoadingMoreSessions = false,
                        message = result.summary,
                    )
                }
            }
        }
    }

    fun openSession(sessionKey: SessionKey) {
        val selected = mutableState.value.sessions.firstOrNull { it.sessionKey == sessionKey }
            ?: return
        requestJob?.cancel()
        stopRealtimeObservation()
        stopControlSession(releaseLease = true)
        val generation = ++requestGeneration
        mutableState.value = mutableState.value.copy(
            phase = SessionBrowserPhase.LOADING_TRANSCRIPT,
            selectedSession = selected,
            transcript = null,
            realtime = null,
            controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
            realtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
            realtimeMessage = null,
            isRefreshing = true,
            hasOlderMessages = false,
            isLoadingOlderMessages = false,
            message = null,
        )
        requestJob = viewModelScope.launch {
            when (
                val result = source.loadMessages(
                    sessionKey = sessionKey,
                    limit = INITIAL_TRANSCRIPT_WINDOW_SIZE,
                    offset = (selected.messageCount - INITIAL_TRANSCRIPT_WINDOW_SIZE).coerceAtLeast(0),
                    profile = selected.profile,
                )
            ) {
                is SessionRepositoryResult.Data -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.TRANSCRIPT,
                        transcript = result.value,
                        isRefreshing = false,
                        hasOlderMessages = result.value.pagination.offset > 0,
                        isLoadingOlderMessages = false,
                        message = null,
                    )
                    startRealtimeObservation(selected, result.value)
                }
                SessionRepositoryResult.AuthenticationRequired -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.AUTHENTICATION_REQUIRED,
                        selectedSession = null,
                        transcript = null,
                        realtime = null,
                        isRefreshing = false,
                        hasOlderMessages = false,
                        isLoadingOlderMessages = false,
                        message = AUTHENTICATION_MESSAGE,
                    )
                }
                is SessionRepositoryResult.Unavailable -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.ERROR,
                        isRefreshing = false,
                        hasOlderMessages = false,
                        isLoadingOlderMessages = false,
                        message = result.summary,
                    )
                }
            }
        }
    }

    fun loadOlderMessages() {
        val current = mutableState.value
        val selected = current.selectedSession ?: return
        val transcript = current.transcript ?: return
        val currentOffset = transcript.pagination.offset
        if (
            current.phase != SessionBrowserPhase.TRANSCRIPT ||
            currentOffset <= 0 ||
            current.isRefreshing ||
            current.isLoadingOlderMessages
        ) {
            return
        }
        requestJob?.cancel()
        val generation = ++requestGeneration
        val limit = minOf(INITIAL_TRANSCRIPT_WINDOW_SIZE, currentOffset)
        val offset = currentOffset - limit
        mutableState.value = current.copy(
            isLoadingOlderMessages = true,
            message = null,
        )
        requestJob = viewModelScope.launch {
            when (
                val result = source.loadMessages(
                    sessionKey = selected.sessionKey,
                    limit = limit,
                    offset = offset,
                    profile = selected.profile,
                )
            ) {
                is SessionRepositoryResult.Data -> {
                    if (generation != requestGeneration) return@launch
                    val latest = mutableState.value
                    val currentTranscript = latest.transcript ?: return@launch
                    val older = result.value
                    val mergedMessages = older.messages + currentTranscript.messages
                    val merged = currentTranscript.copy(
                        lineageTip = older.lineageTip,
                        messages = mergedMessages,
                        pagination = currentTranscript.pagination.copy(
                            limit = null,
                            offset = older.pagination.offset,
                            returned = mergedMessages.size,
                        ),
                    )
                    mutableState.value = latest.copy(
                        transcript = merged,
                        realtime = latest.realtime?.copy(transcript = merged),
                        hasOlderMessages = older.pagination.offset > 0,
                        isLoadingOlderMessages = false,
                        message = null,
                    )
                }
                SessionRepositoryResult.AuthenticationRequired -> {
                    if (generation != requestGeneration) return@launch
                    stopRealtimeObservation()
                    stopControlSession(releaseLease = true)
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.AUTHENTICATION_REQUIRED,
                        selectedSession = null,
                        transcript = null,
                        realtime = null,
                        hasOlderMessages = false,
                        isLoadingOlderMessages = false,
                        message = AUTHENTICATION_MESSAGE,
                    )
                }
                is SessionRepositoryResult.Unavailable -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        isLoadingOlderMessages = false,
                        message = result.summary,
                    )
                }
            }
        }
    }

    fun backToSessions() {
        requestJob?.cancel()
        stopRealtimeObservation()
        stopControlSession(releaseLease = true)
        requestGeneration += 1
        mutableState.value = mutableState.value.copy(
            phase = SessionBrowserPhase.LIST,
            selectedSession = null,
            transcript = null,
            realtime = null,
            controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
            realtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
            realtimeMessage = null,
            isRefreshing = false,
            hasOlderMessages = false,
            isLoadingOlderMessages = false,
            message = null,
        )
    }

    fun disconnect() {
        requestJob?.cancel()
        requestJob = null
        stopRealtimeObservation()
        stopControlSession(releaseLease = true)
        requestGeneration += 1
        started = false
        mutableState.value = mutableState.value.copy(
            phase = SessionBrowserPhase.AUTHENTICATION_REQUIRED,
            selectedSession = null,
            transcript = null,
            realtime = null,
            controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
            realtimeConnectionStatus = RealtimeConnectionStatus.IDLE,
            realtimeMessage = null,
            isRefreshing = false,
            hasOlderMessages = false,
            isLoadingOlderMessages = false,
            message = AUTHENTICATION_MESSAGE,
        )
    }

    fun onRealtimeProjection(projection: RealtimeSessionProjection) {
        val current = mutableState.value
        if (current.phase != SessionBrowserPhase.TRANSCRIPT) return
        if (current.selectedSession?.sessionKey != projection.sessionKey) return
        if (!projection.running) {
            guidanceGeneration += 1
            guidanceJob?.cancel()
            guidanceJob = null
        }
        mutableState.value = current.copy(
            realtime = projection,
            interruptRequestId = current.interruptRequestId.takeIf { projection.running },
            guidance = current.guidance.takeIf { projection.running } ?: SessionGuidanceState(),
        )
        val boundControlRuntime = controlRuntimeSessionId ?: controlOpeningRuntimeSessionId
        if (
            boundControlRuntime != null &&
            boundControlRuntime != projection.runtimeSessionId
        ) {
            revokeControlForRuntimeChange()
            return
        }
        startControlSessionIfPossible(current.selectedSession, projection)
    }

    fun onControlStatus(status: RealtimeControlStatus) {
        mutableState.value = mutableState.value.copy(controlStatus = status)
    }

    fun onDraftChanged(text: String) {
        mutableState.value = mutableState.value.copy(
            composer = composerReducer.reduce(
                mutableState.value.composer,
                ComposerAction.DraftChanged(text),
            ),
        )
    }

    fun onGuidanceDraftChanged(text: String) {
        val current = mutableState.value
        if (!current.canGuide) return
        mutableState.value = current.copy(guidance = current.guidance.withDraft(text))
    }

    fun submitGuidance() {
        val current = mutableState.value
        val channel = controlChannel ?: return
        val runtimeSessionId = controlRuntimeSessionId ?: return
        val lease = (current.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller)
            ?.lease
            ?: return
        if (!current.canGuide) return
        val text = current.guidance.draft.trim()
        if (text.isEmpty()) return
        val requestId = requestIdFactory()
        val leaseId = lease.leaseId
        val controlAttemptGeneration = controlGeneration
        val guidanceAttemptGeneration = ++guidanceGeneration
        mutableState.value = current.copy(
            guidance = current.guidance.started(requestId, text),
        )
        guidanceJob = viewModelScope.launch {
            val result = channel.steer(leaseId, requestId, text)
            if (!isCurrentGuidanceAttempt(
                    channel = channel,
                    runtimeSessionId = runtimeSessionId,
                    leaseId = leaseId,
                    controlAttemptGeneration = controlAttemptGeneration,
                    guidanceAttemptGeneration = guidanceAttemptGeneration,
                )
            ) {
                return@launch
            }
            when (result) {
                is SessionControllerResult.Success -> {
                    if (
                        result.value.clientRequestId != requestId ||
                        result.value.status == SessionCommandState.UNKNOWN
                    ) {
                        reconcileUnknownGuidance(
                            channel = channel,
                            runtimeSessionId = runtimeSessionId,
                            leaseId = leaseId,
                            requestId = requestId,
                            controlAttemptGeneration = controlAttemptGeneration,
                            guidanceAttemptGeneration = guidanceAttemptGeneration,
                        )
                    } else {
                        applyGuidanceResponse(requestId, result.value)
                    }
                }
                SessionControllerResult.Timeout,
                SessionControllerResult.NotReady,
                SessionControllerResult.InvalidResponse,
                -> reconcileUnknownGuidance(
                    channel = channel,
                    runtimeSessionId = runtimeSessionId,
                    leaseId = leaseId,
                    requestId = requestId,
                    controlAttemptGeneration = controlAttemptGeneration,
                    guidanceAttemptGeneration = guidanceAttemptGeneration,
                )
                SessionControllerResult.Disconnected -> revokeControlAfterGuidanceDisconnect(
                    channel = channel,
                    runtimeSessionId = runtimeSessionId,
                    requestId = requestId,
                )
                is SessionControllerResult.RpcFailure -> if (
                    result.error.code == EFFECT_UNKNOWN_ERROR
                ) {
                    reconcileUnknownGuidance(
                        channel = channel,
                        runtimeSessionId = runtimeSessionId,
                        leaseId = leaseId,
                        requestId = requestId,
                        controlAttemptGeneration = controlAttemptGeneration,
                        guidanceAttemptGeneration = guidanceAttemptGeneration,
                    )
                } else {
                    applyGuidanceFailure(requestId, result.error.message)
                }
                SessionControllerResult.Unsupported -> applyGuidanceFailure(
                    requestId,
                    "Hermes server does not support guidance for the current turn.",
                )
            }
        }
    }

    private fun applyGuidanceResponse(
        requestId: ClientRequestId,
        response: SessionSteerResponse,
    ) {
        if (response.clientRequestId != requestId) {
            applyGuidanceFailure(requestId, "Hermes returned a mismatched guidance response.")
            return
        }
        when (response.status) {
            SessionCommandState.ACCEPTED,
            SessionCommandState.QUEUED,
            -> {
                val current = mutableState.value
                mutableState.value = current.copy(
                    guidance = current.guidance.accepted(requestId),
                )
            }
            SessionCommandState.REJECTED -> applyGuidanceFailure(
                requestId,
                "Hermes rejected guidance for the current turn.",
            )
            SessionCommandState.UNKNOWN -> applyGuidanceFailure(
                requestId,
                "Hermes returned an unknown guidance status.",
            )
        }
    }

    private suspend fun reconcileUnknownGuidance(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
        controlAttemptGeneration: Long,
        guidanceAttemptGeneration: Long,
    ) {
        if (!isCurrentGuidanceAttempt(
                channel = channel,
                runtimeSessionId = runtimeSessionId,
                leaseId = leaseId,
                controlAttemptGeneration = controlAttemptGeneration,
                guidanceAttemptGeneration = guidanceAttemptGeneration,
            )
        ) {
            return
        }
        val current = mutableState.value
        mutableState.value = current.copy(
            guidance = current.guidance.deliveryUnknown(requestId),
        )
        val status = channel.commandStatus(MobileControlMethods.SESSION_STEER, requestId)
        if (!isCurrentGuidanceAttempt(
                channel = channel,
                runtimeSessionId = runtimeSessionId,
                leaseId = leaseId,
                controlAttemptGeneration = controlAttemptGeneration,
                guidanceAttemptGeneration = guidanceAttemptGeneration,
            )
        ) {
            return
        }
        when (status) {
            is SessionControllerResult.Success -> {
                if (
                    status.value.clientRequestId == requestId &&
                    status.value.status != SessionCommandState.UNKNOWN
                ) {
                    applyGuidanceResponse(
                        requestId,
                        SessionSteerResponse(
                            status = status.value.status,
                            clientRequestId = status.value.clientRequestId,
                        ),
                    )
                }
            }
            else -> Unit
        }
    }

    private fun isCurrentGuidanceAttempt(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        leaseId: SessionControlLeaseId,
        controlAttemptGeneration: Long,
        guidanceAttemptGeneration: Long,
    ): Boolean = controlAttemptGeneration == controlGeneration &&
        guidanceAttemptGeneration == guidanceGeneration &&
        controlChannel === channel &&
        controlRuntimeSessionId == runtimeSessionId &&
        (mutableState.value.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller)
            ?.lease
            ?.leaseId == leaseId

    private fun applyGuidanceFailure(requestId: ClientRequestId, summary: String) {
        val current = mutableState.value
        mutableState.value = current.copy(
            guidance = current.guidance.failed(
                requestId,
                HermesMessagePresentation.safeText(
                    summary,
                    maxCodePoints = MAX_COMMAND_FAILURE_CODE_POINTS,
                ).orEmpty(),
            ),
        )
    }

    private fun revokeControlAfterGuidanceDisconnect(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        requestId: ClientRequestId,
    ) {
        if (controlChannel !== channel || controlRuntimeSessionId != runtimeSessionId) return
        guidanceGeneration += 1
        controlEventsJob?.cancel()
        controlEventsJob = null
        leaseRenewJob?.cancel()
        leaseRenewJob = null
        interruptJob?.cancel()
        interruptJob = null
        pendingInputGeneration += 1
        pendingInputJob?.cancel()
        pendingInputJob = null
        guidanceJob = null
        controlChannel = null
        controlRuntimeSessionId = null
        controlOpeningRuntimeSessionId = null
        val current = mutableState.value
        mutableState.value = current.copy(
            control = controlReducer.reduce(
                current.control,
                ControlAction.LeaseLost(ControlLossReason.CONNECTION_LOST),
            ),
            controlStatus = RealtimeControlStatus.OBSERVER,
            controlAvailableMethods = emptySet(),
            interruptRequestId = null,
            guidance = current.guidance.deliveryUnknown(requestId),
            pendingInteraction = pendingInteractionAfterTransportLoss(current.pendingInteraction),
        )
        channel.close()
    }

    fun sendPrompt() {
        val current = mutableState.value
        val channel = controlChannel ?: return
        val lease = (current.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller)?.lease
            ?: return
        if (!current.canSend) return
        val text = current.composer.draft
        if (text.isBlank()) return
        val requestId = requestIdFactory()
        val clientTurnId = turnIdFactory()
        mutableState.value = current.copy(
            commands = commandReducer.reduce(
                current.commands,
                CommandAction.Started(
                    requestId = requestId,
                    clientTurnId = clientTurnId,
                    promptPreview = HermesMessagePresentation.safeText(
                        text,
                        maxCodePoints = MAX_QUEUED_PROMPT_PREVIEW_CODE_POINTS,
                    ),
                ),
            ),
            composer = composerReducer.reduce(
                current.composer,
                ComposerAction.SubmitStarted(
                    ComposerSubmission(
                        requestId = requestId,
                        clientTurnId = clientTurnId,
                        text = text,
                    ),
                ),
            ),
        )
        promptJob = viewModelScope.launch {
            val result = channel.submitPrompt(lease.leaseId, requestId, clientTurnId, text)
            when (result) {
                is SessionControllerResult.Success -> applyPromptAck(
                    result.value.toCommandStatus(),
                )
                SessionControllerResult.Timeout,
                SessionControllerResult.Disconnected,
                SessionControllerResult.NotReady,
                SessionControllerResult.InvalidResponse,
                -> {
                    mutableState.value = mutableState.value.copy(
                        commands = commandReducer.reduce(
                            mutableState.value.commands,
                            CommandAction.DeliveryUnknown(requestId),
                        ),
                    )
                    when (
                        val status = channel.commandStatus(
                            MobileControlMethods.PROMPT_SUBMIT,
                            requestId,
                        )
                    ) {
                        is SessionControllerResult.Success -> {
                            if (status.value.clientRequestId == requestId) {
                                applyPromptAck(status.value)
                            }
                        }
                        else -> Unit
                    }
                }
                is SessionControllerResult.RpcFailure -> if (
                    result.error.code == EFFECT_UNKNOWN_ERROR
                ) {
                    mutableState.value = mutableState.value.copy(
                        commands = commandReducer.reduce(
                            mutableState.value.commands,
                            CommandAction.DeliveryUnknown(requestId),
                        ),
                    )
                    when (
                        val status = channel.commandStatus(
                            MobileControlMethods.PROMPT_SUBMIT,
                            requestId,
                        )
                    ) {
                        is SessionControllerResult.Success -> {
                            if (status.value.clientRequestId == requestId) {
                                applyPromptAck(status.value)
                            }
                        }
                        else -> Unit
                    }
                } else {
                    applyPromptFailure(requestId, result.error.message)
                }
                SessionControllerResult.Unsupported -> applyPromptFailure(
                    requestId,
                    "Hermes server does not support prompt submission.",
                )
            }
        }
    }

    fun stopCurrentTurn() {
        val current = mutableState.value
        val channel = controlChannel ?: return
        val lease = (
            current.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller
            )?.lease ?: return
        if (!current.canStop) return
        val requestId = requestIdFactory()
        mutableState.value = current.copy(
            interruptRequestId = requestId,
            commands = commandReducer.reduce(
                current.commands,
                CommandAction.Started(requestId),
            ),
        )
        interruptJob = viewModelScope.launch {
            when (val result = channel.interrupt(lease.leaseId, requestId)) {
                is SessionControllerResult.Success -> applyInterruptAck(result.value)
                SessionControllerResult.Timeout,
                SessionControllerResult.Disconnected,
                SessionControllerResult.NotReady,
                SessionControllerResult.InvalidResponse,
                -> {
                    mutableState.value = mutableState.value.copy(
                        commands = commandReducer.reduce(
                            mutableState.value.commands,
                            CommandAction.DeliveryUnknown(requestId),
                        ),
                    )
                    when (
                        val status = channel.commandStatus(
                            MobileControlMethods.SESSION_INTERRUPT,
                            requestId,
                        )
                    ) {
                        is SessionControllerResult.Success -> {
                            if (status.value.clientRequestId == requestId) {
                                val reconciled = commandReducer.reduce(
                                    mutableState.value.commands,
                                    CommandAction.StatusReconciled(status.value),
                                )
                                val current = mutableState.value
                                mutableState.value = current.copy(
                                    commands = reconciled,
                                    interruptRequestId = current.interruptRequestId
                                        ?.takeIf { pendingId ->
                                            pendingId == requestId &&
                                                current.realtime?.running == true &&
                                                status.value.status != SessionCommandState.REJECTED
                                        },
                                )
                            }
                        }
                        else -> Unit
                    }
                }
                is SessionControllerResult.RpcFailure -> if (
                    result.error.code == EFFECT_UNKNOWN_ERROR
                ) {
                    mutableState.value = mutableState.value.copy(
                        commands = commandReducer.reduce(
                            mutableState.value.commands,
                            CommandAction.DeliveryUnknown(requestId),
                        ),
                    )
                    when (
                        val status = channel.commandStatus(
                            MobileControlMethods.SESSION_INTERRUPT,
                            requestId,
                        )
                    ) {
                        is SessionControllerResult.Success -> {
                            if (status.value.clientRequestId == requestId) {
                                val reconciled = commandReducer.reduce(
                                    mutableState.value.commands,
                                    CommandAction.StatusReconciled(status.value),
                                )
                                val currentState = mutableState.value
                                mutableState.value = currentState.copy(
                                    commands = reconciled,
                                    interruptRequestId = currentState.interruptRequestId
                                        ?.takeIf { pendingId ->
                                            pendingId == requestId &&
                                                currentState.realtime?.running == true &&
                                                status.value.status != SessionCommandState.REJECTED
                                        },
                                )
                            }
                        }
                        else -> Unit
                    }
                } else {
                    applyInterruptFailure(requestId, result.error.message)
                }
                SessionControllerResult.Unsupported -> {
                    applyInterruptFailure(
                        requestId,
                        "Hermes server does not support stopping this turn.",
                    )
                }
            }
        }
    }

    private fun applyPendingInteraction(action: PendingInputInteractionAction) {
        val current = mutableState.value
        mutableState.value = current.copy(
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                action,
            ),
        )
    }

    private fun applyPendingInteractionBeforeSnapshotRefresh(action: PendingInputInteractionAction) {
        val current = mutableState.value
        mutableState.value = current.copy(
            isPendingInputSnapshotRefreshing = true,
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                action,
            ),
        )
    }

    private fun pendingInteractionAfterTransportLoss(
        current: PendingInputInteractionState,
    ): PendingInputInteractionState {
        val clientRequestId = current.inFlightClientRequestId ?: return current
        pendingInputGeneration += 1
        pendingInputJob?.cancel()
        pendingInputJob = null
        return pendingInteractionReducer.reduce(
            current,
            PendingInputInteractionAction.DeliveryUnknown(clientRequestId),
        )
    }

    private fun applyAcceptedPendingResponse(
        response: PendingInputRespondResponse,
        clientRequestId: ClientRequestId,
    ) {
        val current = mutableState.value
        mutableState.value = current.copy(
            control = current.control.copy(
                controlRevision = maxOf(
                    current.control.controlRevision ?: 0,
                    response.controlRevision,
                ),
            ),
            isPendingInputSnapshotRefreshing = true,
            pendingInteraction = pendingInteractionReducer.reduce(
                current.pendingInteraction,
                PendingInputInteractionAction.Accepted(clientRequestId),
            ),
        )
    }

    private fun applyPendingFailure(clientRequestId: ClientRequestId, summary: String) {
        applyPendingInteraction(
            PendingInputInteractionAction.Failed(
                clientRequestId = clientRequestId,
                summary = HermesMessagePresentation.safeText(
                    summary,
                    maxCodePoints = MAX_COMMAND_FAILURE_CODE_POINTS,
                ).orEmpty(),
            ),
        )
    }

    private suspend fun refreshPendingInputSnapshot(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        leaseId: SessionControlLeaseId,
        generation: Long,
    ) {
        val renewed = channel.renew(leaseId)
        if (!isCurrentPendingInputControl(channel, runtimeSessionId, generation)) {
            return
        }
        val currentLeaseId = (
            mutableState.value.control.mode as?
                app.hermesmobile.sessions.control.ControlMode.Controller
            )?.lease?.leaseId
        if (currentLeaseId != leaseId) {
            revokeControlAfterPendingSnapshotFailure(channel, runtimeSessionId)
            return
        }
        when (renewed) {
            is SessionControllerResult.Success -> {
                val currentRevision = mutableState.value.control.controlRevision ?: 0
                if (renewed.value.controlRevision >= currentRevision) {
                    applyControlLeaseSnapshot(
                        lease = renewed.value,
                        completesPendingInputSnapshotRefresh = true,
                    )
                    startLeaseRenewal(channel, runtimeSessionId, renewed.value)
                } else {
                    revokeControlAfterPendingSnapshotFailure(channel, runtimeSessionId)
                }
            }
            else -> revokeControlAfterPendingSnapshotFailure(channel, runtimeSessionId)
        }
    }

    private fun isCurrentPendingInputControl(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        generation: Long,
    ): Boolean = generation == pendingInputGeneration &&
        pendingInputControlGeneration == controlGeneration &&
        controlChannel === channel &&
        controlRuntimeSessionId == runtimeSessionId

    private fun revokeControlAfterPendingSnapshotFailure(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
    ) {
        if (controlChannel !== channel || controlRuntimeSessionId != runtimeSessionId) return
        controlGeneration += 1
        guidanceGeneration += 1
        controlEventsJob?.cancel()
        controlEventsJob = null
        leaseRenewJob?.cancel()
        leaseRenewJob = null
        interruptJob?.cancel()
        interruptJob = null
        guidanceJob?.cancel()
        guidanceJob = null
        pendingInputGeneration += 1
        pendingInputJob = null
        controlChannel = null
        controlRuntimeSessionId = null
        controlOpeningRuntimeSessionId = null
        val current = mutableState.value
        mutableState.value = current.copy(
            control = controlReducer.reduce(
                current.control,
                ControlAction.LeaseLost(ControlLossReason.CONNECTION_LOST),
            ),
            controlStatus = RealtimeControlStatus.OBSERVER,
            controlAvailableMethods = emptySet(),
            interruptRequestId = null,
            guidance = guidanceAfterTransportLoss(current.guidance),
        )
        channel.close()
    }

    private fun applyControlLeaseSnapshot(
        lease: SessionControlLease,
        completesPendingInputSnapshotRefresh: Boolean,
    ) {
        val current = mutableState.value
        if (lease.controlRevision < (current.control.controlRevision ?: 0)) return
        val preservesPendingInputSnapshotRefresh =
            current.isPendingInputSnapshotRefreshing && !completesPendingInputSnapshotRefresh
        val previousLeaseId = (
            current.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller
            )?.lease?.leaseId
        val invalidatesGuidanceAttempt = previousLeaseId != null &&
            previousLeaseId != lease.leaseId &&
            current.guidance.inFlightRequestId != null
        if (invalidatesGuidanceAttempt) {
            guidanceGeneration += 1
            guidanceJob?.cancel()
            guidanceJob = null
        }
        mutableState.value = current.copy(
            control = controlReducer.reduce(
                current.control,
                ControlAction.LeaseGranted(lease),
            ),
            controlStatus = RealtimeControlStatus.CONTROLLER,
            guidance = if (invalidatesGuidanceAttempt) {
                guidanceAfterTransportLoss(current.guidance)
            } else {
                current.guidance
            },
            isPendingInputSnapshotRefreshing = if (completesPendingInputSnapshotRefresh) {
                false
            } else {
                current.isPendingInputSnapshotRefreshing
            },
            pendingInteraction = if (preservesPendingInputSnapshotRefresh) {
                current.pendingInteraction
            } else {
                reconcilePendingInteraction(
                    current.pendingInteraction,
                    lease.pendingInput?.requestId,
                )
            },
        )
    }

    private fun reconcilePendingInteraction(
        current: PendingInputInteractionState,
        authoritativeRequestId: String?,
    ): PendingInputInteractionState = when {
        authoritativeRequestId == current.requestId -> pendingInteractionReducer.reduce(
            current,
            PendingInputInteractionAction.Observed(authoritativeRequestId),
        )
        authoritativeRequestId == null && current.requestId != null -> PendingInputInteractionState(
            outcome = if (current.outcome == PendingInputInteractionOutcome.Accepted) {
                PendingInputInteractionOutcome.Accepted
            } else {
                PendingInputInteractionOutcome.ResolvedElsewhere
            },
        )
        else -> pendingInteractionReducer.reduce(
            current,
            PendingInputInteractionAction.Observed(authoritativeRequestId),
        )
    }

    private fun applyPromptFailure(requestId: ClientRequestId, summary: String) {
        mutableState.value = mutableState.value.copy(
            commands = commandReducer.reduce(
                mutableState.value.commands,
                CommandAction.Failed(
                    requestId,
                    HermesMessagePresentation.safeText(
                        summary,
                        maxCodePoints = MAX_COMMAND_FAILURE_CODE_POINTS,
                    ).orEmpty(),
                ),
            ),
            composer = composerReducer.reduce(
                mutableState.value.composer,
                ComposerAction.SubmissionRejected(requestId),
            ),
        )
    }

    private fun applyInterruptFailure(requestId: ClientRequestId, summary: String) {
        mutableState.value = mutableState.value.copy(
            commands = commandReducer.reduce(
                mutableState.value.commands,
                CommandAction.Failed(
                    requestId,
                    HermesMessagePresentation.safeText(
                        summary,
                        maxCodePoints = MAX_COMMAND_FAILURE_CODE_POINTS,
                    ).orEmpty(),
                ),
            ),
            interruptRequestId = null,
        )
    }

    private fun applyInterruptAck(response: SessionInterruptResponse) {
        mutableState.value = mutableState.value.copy(
            commands = commandReducer.reduce(
                mutableState.value.commands,
                CommandAction.Acknowledged(response.toCommandStatus()),
            ),
            interruptRequestId = mutableState.value.interruptRequestId.takeUnless {
                response.status == SessionCommandState.REJECTED
            },
        )
    }

    private fun applyPromptAck(status: SessionCommandStatus) {
        val composer = when (status.status) {
            SessionCommandState.ACCEPTED,
            SessionCommandState.QUEUED,
            -> composerReducer.reduce(
                mutableState.value.composer,
                ComposerAction.SubmissionAcknowledged(status.clientRequestId),
            )
            SessionCommandState.REJECTED -> composerReducer.reduce(
                mutableState.value.composer,
                ComposerAction.SubmissionRejected(status.clientRequestId),
            )
            SessionCommandState.UNKNOWN -> mutableState.value.composer
        }
        mutableState.value = mutableState.value.copy(
            commands = commandReducer.reduce(
                mutableState.value.commands,
                CommandAction.Acknowledged(status),
            ),
            composer = composer,
        )
    }

    private fun startControlSessionIfPossible(
        session: SessionProjection?,
        projection: RealtimeSessionProjection,
    ) {
        val source = controlSource ?: return
        val runtime = projection.runtimeSessionId
        val mode = mutableState.value.control.mode
        val mayAcquire = mode is app.hermesmobile.sessions.control.ControlMode.Disconnected ||
            mode is app.hermesmobile.sessions.control.ControlMode.Observer
        if (session == null || controlChannel != null || !mayAcquire) return
        val generation = ++controlGeneration
        controlOpeningRuntimeSessionId = runtime
        mutableState.value = mutableState.value.copy(
            control = controlReducer.reduce(
                mutableState.value.control,
                ControlAction.AcquireStarted,
            ),
        )
        controlOpenJob = viewModelScope.launch {
            when (val opened = source.open(session, runtime)) {
                is SessionControlOpenResult.Ready -> {
                    if (!isCurrentControlOpen(generation, session, runtime)) {
                        opened.channel.close()
                        return@launch
                    }
                    controlOpeningRuntimeSessionId = null
                    controlChannel = opened.channel
                    controlRuntimeSessionId = runtime
                    mutableState.value = mutableState.value.copy(
                        controlAvailableMethods = opened.channel.availableMethods,
                    )
                    observeControlEvents(opened.channel, runtime)
                    val status = opened.channel.status()
                    if (
                        generation != controlGeneration ||
                        controlChannel !== opened.channel ||
                        controlRuntimeSessionId != runtime
                    ) {
                        opened.channel.close()
                        return@launch
                    }
                    if (status is SessionControllerResult.Success) {
                        mutableState.value = mutableState.value.copy(
                            control = controlReducer.reduce(
                                mutableState.value.control,
                                ControlAction.StatusReconciled(status.value),
                            ),
                            controlStatus = RealtimeControlStatus.OBSERVER,
                        )
                        if (status.value.controllerKind == SessionControllerKind.DESKTOP) {
                            return@launch
                        }
                        mutableState.value = mutableState.value.copy(
                            control = controlReducer.reduce(
                                mutableState.value.control,
                                ControlAction.AcquireStarted,
                            ),
                        )
                    } else {
                        failControlAcquisition(
                            generation = generation,
                            session = session,
                            runtime = runtime,
                            channel = opened.channel,
                            reason = ControlLossReason.CONNECTION_LOST,
                        )
                        return@launch
                    }
                    when (val lease = opened.channel.acquire()) {
                        is SessionControllerResult.Success -> {
                            if (
                                generation != controlGeneration ||
                                controlChannel !== opened.channel ||
                                controlRuntimeSessionId != runtime
                            ) {
                                opened.channel.release(lease.value.leaseId)
                                opened.channel.close()
                                return@launch
                            }
                            applyControlLeaseSnapshot(
                                lease = lease.value,
                                completesPendingInputSnapshotRefresh = true,
                            )
                            startLeaseRenewal(opened.channel, runtime, lease.value)
                            resumeGuidanceReconciliationIfNeeded(opened.channel, runtime)
                        }
                        SessionControllerResult.Unsupported,
                        is SessionControllerResult.RpcFailure,
                        SessionControllerResult.InvalidResponse,
                        -> failControlAcquisition(
                            generation = generation,
                            session = session,
                            runtime = runtime,
                            channel = opened.channel,
                            reason = ControlLossReason.REJECTED,
                        )
                        SessionControllerResult.NotReady,
                        SessionControllerResult.Disconnected,
                        SessionControllerResult.Timeout,
                        -> failControlAcquisition(
                            generation = generation,
                            session = session,
                            runtime = runtime,
                            channel = opened.channel,
                            reason = ControlLossReason.CONNECTION_LOST,
                        )
                    }
                }
                SessionControlOpenResult.AuthenticationRequired -> failControlAcquisition(
                    generation = generation,
                    session = session,
                    runtime = runtime,
                    channel = null,
                    reason = ControlLossReason.REJECTED,
                )
                is SessionControlOpenResult.Unavailable -> failControlAcquisition(
                    generation = generation,
                    session = session,
                    runtime = runtime,
                    channel = null,
                    reason = ControlLossReason.CONNECTION_LOST,
                )
            }
        }
    }

    private fun failControlAcquisition(
        generation: Long,
        session: SessionProjection,
        runtime: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        channel: SessionControlChannel?,
        reason: ControlLossReason,
    ) {
        val current = mutableState.value
        val currentAttempt = generation == controlGeneration &&
            current.phase == SessionBrowserPhase.TRANSCRIPT &&
            current.selectedSession?.sessionKey == session.sessionKey &&
            current.realtime?.runtimeSessionId == runtime &&
            current.control.mode is app.hermesmobile.sessions.control.ControlMode.Acquiring &&
            if (channel == null) {
                controlOpeningRuntimeSessionId == runtime && controlChannel == null
            } else {
                controlChannel === channel && controlRuntimeSessionId == runtime
            }
        if (!currentAttempt) {
            channel?.close()
            return
        }

        controlOpenJob = null
        controlEventsJob?.cancel()
        controlEventsJob = null
        controlOpeningRuntimeSessionId = null
        controlChannel = null
        controlRuntimeSessionId = null
        mutableState.value = current.copy(
            control = controlReducer.reduce(
                current.control,
                ControlAction.LeaseLost(reason),
            ),
            controlStatus = RealtimeControlStatus.OBSERVER,
            controlAvailableMethods = emptySet(),
            interruptRequestId = null,
            pendingInteraction = pendingInteractionAfterTransportLoss(
                current.pendingInteraction,
            ),
        )
        channel?.close()
    }

    private fun isCurrentControlOpen(
        generation: Long,
        session: SessionProjection,
        runtime: app.hermesmobile.protocol.sessions.RuntimeSessionId,
    ): Boolean {
        val current = mutableState.value
        return generation == controlGeneration &&
            controlOpeningRuntimeSessionId == runtime &&
            current.phase == SessionBrowserPhase.TRANSCRIPT &&
            current.selectedSession?.sessionKey == session.sessionKey &&
            current.realtime?.runtimeSessionId == runtime &&
            current.control.mode is app.hermesmobile.sessions.control.ControlMode.Acquiring
    }

    private fun resumeGuidanceReconciliationIfNeeded(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
    ) {
        val current = mutableState.value
        val currentGuidance = current.guidance
        val requestId = currentGuidance.inFlightRequestId
            ?.takeIf { currentGuidance.phase == SessionGuidancePhase.DELIVERY_UNKNOWN }
            ?: return
        val leaseId = (
            current.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller
            )?.lease?.leaseId ?: return
        val controlAttemptGeneration = controlGeneration
        val guidanceAttemptGeneration = ++guidanceGeneration
        guidanceJob?.cancel()
        guidanceJob = viewModelScope.launch {
            reconcileUnknownGuidance(
                channel = channel,
                runtimeSessionId = runtimeSessionId,
                leaseId = leaseId,
                requestId = requestId,
                controlAttemptGeneration = controlAttemptGeneration,
                guidanceAttemptGeneration = guidanceAttemptGeneration,
            )
        }
    }

    private fun loadSessions(showLoading: Boolean) {
        requestJob?.cancel()
        stopRealtimeObservation()
        stopControlSession(releaseLease = true)
        val generation = ++requestGeneration
        mutableState.value = mutableState.value.copy(
            phase = if (showLoading) {
                SessionBrowserPhase.LOADING_SESSIONS
            } else {
                mutableState.value.phase
            },
            isRefreshing = true,
            isLoadingMoreSessions = false,
            message = null,
        )
        requestJob = viewModelScope.launch {
            when (val result = source.loadSessions()) {
                is SessionRepositoryResult.Data -> {
                    if (generation != requestGeneration) return@launch
                    val page = result.value
                    nextSessionOffset = page.offset + page.sessions.size
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.LIST,
                        sessions = page.sessions,
                        hasMoreSessions = page.sessions.isNotEmpty() &&
                            nextSessionOffset < page.total,
                        selectedSession = null,
                        transcript = null,
                        realtime = null,
                        isRefreshing = false,
                        isLoadingMoreSessions = false,
                        hasOlderMessages = false,
                        isLoadingOlderMessages = false,
                        message = null,
                    )
                }
                SessionRepositoryResult.AuthenticationRequired -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.AUTHENTICATION_REQUIRED,
                        sessions = emptyList(),
                        hasMoreSessions = false,
                        selectedSession = null,
                        transcript = null,
                        realtime = null,
                        isRefreshing = false,
                        isLoadingMoreSessions = false,
                        hasOlderMessages = false,
                        isLoadingOlderMessages = false,
                        message = AUTHENTICATION_MESSAGE,
                    )
                }
                is SessionRepositoryResult.Unavailable -> {
                    if (generation != requestGeneration) return@launch
                    mutableState.value = mutableState.value.copy(
                        phase = SessionBrowserPhase.ERROR,
                        isRefreshing = false,
                        isLoadingMoreSessions = false,
                        message = result.summary,
                    )
                }
            }
        }
    }

    private fun startRealtimeObservation(
        session: SessionProjection,
        baseline: SessionTranscript,
    ) {
        realtimeJob?.cancel()
        realtimeJob = viewModelScope.launch {
            realtimeSource.observe(session, baseline)
                .catch { error ->
                    applyRealtimeConnection(
                        sessionKey = session.sessionKey,
                        update = SessionRealtimeUpdate.Connection(
                            status = RealtimeConnectionStatus.ERROR,
                            controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
                            message = error.message?.takeIf(String::isNotBlank)
                                ?: "Realtime session observation failed.",
                        ),
                    )
                }
                .collect { update ->
                    when (update) {
                        is SessionRealtimeUpdate.Connection ->
                            applyRealtimeConnection(session.sessionKey, update)
                        is SessionRealtimeUpdate.Projection ->
                            onRealtimeProjection(update.projection)
                    }
                }
        }
    }

    private fun applyRealtimeConnection(
        sessionKey: SessionKey,
        update: SessionRealtimeUpdate.Connection,
    ) {
        val current = mutableState.value
        if (current.phase != SessionBrowserPhase.TRANSCRIPT) return
        if (current.selectedSession?.sessionKey != sessionKey) return
        mutableState.value = current.copy(
            controlStatus = update.controlStatus,
            realtimeConnectionStatus = update.status,
            realtimeMessage = HermesMessagePresentation.safeText(
                update.message,
                maxCodePoints = MAX_REALTIME_DIAGNOSTIC_CODE_POINTS,
            ),
        )
    }

    private fun stopRealtimeObservation() {
        realtimeJob?.cancel()
        realtimeJob = null
    }

    private fun stopControlSession(releaseLease: Boolean) {
        controlGeneration += 1
        guidanceGeneration += 1
        controlOpenJob?.cancel()
        controlOpenJob = null
        promptJob?.cancel()
        promptJob = null
        controlEventsJob?.cancel()
        controlEventsJob = null
        leaseRenewJob?.cancel()
        leaseRenewJob = null
        interruptJob?.cancel()
        interruptJob = null
        guidanceJob?.cancel()
        guidanceJob = null
        pendingInputGeneration += 1
        pendingInputJob?.cancel()
        pendingInputJob = null
        val channel = controlChannel
        val lease = (
            mutableState.value.control.mode as?
                app.hermesmobile.sessions.control.ControlMode.Controller
            )?.lease
        controlChannel = null
        controlRuntimeSessionId = null
        controlOpeningRuntimeSessionId = null
        mutableState.value = mutableState.value.copy(
            control = ControlState(),
            controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
            controlAvailableMethods = emptySet(),
            interruptRequestId = null,
            guidance = SessionGuidanceState(),
            pendingInteraction = PendingInputInteractionState(),
        )
        if (channel != null) {
            viewModelScope.launch {
                if (releaseLease && lease != null) {
                    channel.release(lease.leaseId)
                }
                channel.close()
            }
        }
    }

    private fun revokeControlForRuntimeChange() {
        controlGeneration += 1
        guidanceGeneration += 1
        controlOpenJob?.cancel()
        controlOpenJob = null
        promptJob?.cancel()
        promptJob = null
        controlEventsJob?.cancel()
        controlEventsJob = null
        leaseRenewJob?.cancel()
        leaseRenewJob = null
        interruptJob?.cancel()
        interruptJob = null
        guidanceJob?.cancel()
        guidanceJob = null
        pendingInputGeneration += 1
        pendingInputJob?.cancel()
        pendingInputJob = null
        val channel = controlChannel
        val lease = (
            mutableState.value.control.mode as?
                app.hermesmobile.sessions.control.ControlMode.Controller
            )?.lease
        controlChannel = null
        controlRuntimeSessionId = null
        controlOpeningRuntimeSessionId = null
        mutableState.value = mutableState.value.copy(
            control = controlReducer.reduce(
                mutableState.value.control,
                ControlAction.LeaseLost(ControlLossReason.CONNECTION_LOST),
            ),
            controlStatus = RealtimeControlStatus.OBSERVER,
            controlAvailableMethods = emptySet(),
            interruptRequestId = null,
            guidance = SessionGuidanceState(),
            pendingInteraction = PendingInputInteractionState(),
        )
        if (channel != null) {
            viewModelScope.launch {
                if (lease != null) {
                    channel.release(lease.leaseId)
                }
                channel.close()
            }
        }
    }

    override fun onCleared() {
        controlGeneration += 1
        guidanceGeneration += 1
        controlOpenJob?.cancel()
        promptJob?.cancel()
        controlEventsJob?.cancel()
        leaseRenewJob?.cancel()
        interruptJob?.cancel()
        guidanceJob?.cancel()
        pendingInputGeneration += 1
        pendingInputJob?.cancel()
        controlChannel?.close()
        controlChannel = null
        controlOpeningRuntimeSessionId = null
        super.onCleared()
    }

    private fun observeControlEvents(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
    ) {
        controlEventsJob?.cancel()
        controlEventsJob = viewModelScope.launch {
            channel.events.collect { event ->
                if (controlChannel !== channel || controlRuntimeSessionId != runtimeSessionId) {
                    return@collect
                }
                when (event) {
                    SessionControlTransportEvent.Ready -> Unit
                    is SessionControlTransportEvent.Closed -> {
                        guidanceGeneration += 1
                        leaseRenewJob?.cancel()
                        leaseRenewJob = null
                        interruptJob?.cancel()
                        interruptJob = null
                        guidanceJob?.cancel()
                        guidanceJob = null
                        controlChannel = null
                        controlRuntimeSessionId = null
                        val current = mutableState.value
                        mutableState.value = current.copy(
                            control = controlReducer.reduce(
                                current.control,
                                ControlAction.LeaseLost(ControlLossReason.CONNECTION_LOST),
                            ),
                            controlStatus = RealtimeControlStatus.OBSERVER,
                            controlAvailableMethods = emptySet(),
                            interruptRequestId = null,
                            guidance = guidanceAfterTransportLoss(current.guidance),
                            pendingInteraction = pendingInteractionAfterTransportLoss(
                                current.pendingInteraction,
                            ),
                        )
                        channel.close()
                    }
                }
            }
        }
    }

    private fun startLeaseRenewal(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        initialLease: SessionControlLease,
    ) {
        val renewalGeneration = ++leaseRenewGeneration
        val controlAttemptGeneration = controlGeneration
        leaseRenewJob?.cancel()
        leaseRenewJob = viewModelScope.launch {
            var lease = initialLease
            while (
                isCurrentLeaseRenewal(
                    channel = channel,
                    runtimeSessionId = runtimeSessionId,
                    leaseId = lease.leaseId,
                    controlAttemptGeneration = controlAttemptGeneration,
                    renewalGeneration = renewalGeneration,
                )
            ) {
                val waitMillis = (
                    lease.expiresAtEpochMs - leaseRenewLeadMillis - clockEpochMs()
                    ).coerceAtLeast(0L)
                delay(waitMillis)
                if (!isCurrentLeaseRenewal(
                        channel = channel,
                        runtimeSessionId = runtimeSessionId,
                        leaseId = lease.leaseId,
                        controlAttemptGeneration = controlAttemptGeneration,
                        renewalGeneration = renewalGeneration,
                    )
                ) {
                    return@launch
                }
                val renewed = channel.renew(lease.leaseId)
                if (!isCurrentLeaseRenewal(
                        channel = channel,
                        runtimeSessionId = runtimeSessionId,
                        leaseId = lease.leaseId,
                        controlAttemptGeneration = controlAttemptGeneration,
                        renewalGeneration = renewalGeneration,
                    )
                ) {
                    return@launch
                }
                when (renewed) {
                    is SessionControllerResult.Success -> {
                        lease = renewed.value
                        applyControlLeaseSnapshot(
                            lease = lease,
                            completesPendingInputSnapshotRefresh = false,
                        )
                    }
                    else -> {
                        guidanceGeneration += 1
                        guidanceJob?.cancel()
                        guidanceJob = null
                        controlChannel = null
                        controlRuntimeSessionId = null
                        val current = mutableState.value
                        mutableState.value = current.copy(
                            control = controlReducer.reduce(
                                current.control,
                                ControlAction.LeaseLost(ControlLossReason.LEASE_EXPIRED),
                            ),
                            controlStatus = RealtimeControlStatus.OBSERVER,
                            controlAvailableMethods = emptySet(),
                            guidance = guidanceAfterTransportLoss(current.guidance),
                            pendingInteraction = pendingInteractionAfterTransportLoss(
                                current.pendingInteraction,
                            ),
                        )
                        channel.close()
                        return@launch
                    }
                }
            }
        }
    }

    private fun isCurrentLeaseRenewal(
        channel: SessionControlChannel,
        runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId,
        leaseId: SessionControlLeaseId,
        controlAttemptGeneration: Long,
        renewalGeneration: Long,
    ): Boolean = controlAttemptGeneration == controlGeneration &&
        renewalGeneration == leaseRenewGeneration &&
        controlChannel === channel &&
        controlRuntimeSessionId == runtimeSessionId &&
        (mutableState.value.control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller)
            ?.lease
            ?.leaseId == leaseId

    private companion object {
        const val INITIAL_TRANSCRIPT_WINDOW_SIZE = 20
        const val DEFAULT_LEASE_RENEW_LEAD_MILLIS = 5_000L
        const val MAX_COMMAND_FAILURE_CODE_POINTS = 512
        const val MAX_QUEUED_PROMPT_PREVIEW_CODE_POINTS = 240
        const val MAX_REALTIME_DIAGNOSTIC_CODE_POINTS = 512
        const val CLIENT_REQUEST_CONFLICT_ERROR = 4207
        const val PENDING_REQUEST_STALE_ERROR = 4208
        const val INVALID_PENDING_RESPONSE_ERROR = 4213
        const val EFFECT_UNKNOWN_ERROR = 4307
        const val AUTHENTICATION_MESSAGE = "Sign in again to load Hermes sessions."
    }
}

private fun guidanceAfterTransportLoss(current: SessionGuidanceState): SessionGuidanceState {
    val requestId = current.inFlightRequestId ?: return current
    return current.deliveryUnknown(requestId)
}

private fun SessionBrowserUiState.pendingInputForMutation(): SessionPendingInput? {
    if (!canRespondToPendingInput) return null
    val pending = (control.mode as? app.hermesmobile.sessions.control.ControlMode.Controller)
        ?.lease
        ?.pendingInput
        ?: return null
    return pending.takeIf { it.requestId == pendingInteraction.requestId }
}

private fun PromptSubmitResponse.toCommandStatus(): SessionCommandStatus = SessionCommandStatus(
    status = status,
    clientRequestId = clientRequestId,
    clientTurnId = clientTurnId,
    serverTurnId = serverTurnId,
)

private fun SessionInterruptResponse.toCommandStatus(): SessionCommandStatus = SessionCommandStatus(
    status = status,
    clientRequestId = clientRequestId,
)
