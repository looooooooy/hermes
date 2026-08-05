package app.hermesmobile.sessions

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.error
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.traversalIndex
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.hermesmobile.R
import app.hermesmobile.protocol.gateway.MobileControlMethods
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.CommandPhase
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

@Composable
fun SessionBrowserScreen(
    state: SessionBrowserUiState,
    onOpenSession: (SessionKey) -> Unit,
    onBack: () -> Unit,
    onRefresh: () -> Unit,
    onLoadMore: () -> Unit,
    onLoadOlder: () -> Unit = {},
    onReconnect: () -> Unit,
    onDraftChanged: (String) -> Unit = {},
    onSend: () -> Unit,
    onStop: () -> Unit = {},
    onGuidanceDraftChanged: (String) -> Unit = {},
    onSubmitGuidance: () -> Unit = {},
    onPendingChoice: (String) -> Unit = {},
    onPendingOtherChanged: (String) -> Unit = {},
    onPendingSubmit: () -> Unit = {},
    onPendingConfirm: () -> Unit = {},
    onPendingCancelConfirmation: () -> Unit = {},
    onRetryControl: () -> Unit = {},
    onOpenPairing: () -> Unit = {},
) {
    val showingList = state.isSessionListVisible
    var disclosureRegistry by remember { mutableStateOf(ConversationDisclosureRegistry()) }
    val disclosurePolicy = remember { ConversationDisclosurePolicy() }
    val currentDisclosureSessionKey by rememberUpdatedState(
        state.selectedSession?.sessionKey?.value.orEmpty(),
    )
    val onDisclosureToggle = remember(disclosurePolicy) {
        { key: ConversationDisclosureStateKey ->
            disclosureRegistry = disclosureRegistry.toggled(
                sessionKey = currentDisclosureSessionKey,
                key = key,
                policy = disclosurePolicy,
            )
        }
    }
    val onDisclosureFocus = remember {
        { key: ConversationDisclosureStateKey ->
            disclosureRegistry = disclosureRegistry.focused(
                sessionKey = currentDisclosureSessionKey,
                key = key,
            )
        }
    }
    Scaffold(
        topBar = {
            HermesSessionTopBar(
                state = state,
                onBack = onBack,
                actionLabel = stringResource(R.string.refresh).takeIf { showingList },
                actionEnabled = !state.isRefreshing,
                onAction = onRefresh.takeIf { showingList },
                secondaryActionLabel = stringResource(R.string.pair_device)
                    .takeIf { showingList },
                onSecondaryAction = onOpenPairing.takeIf { showingList },
            )
        },
    ) { contentPadding ->
        when (state.phase) {
            SessionBrowserPhase.IDLE,
            SessionBrowserPhase.LOADING_SESSIONS,
            -> LoadingState(
                label = stringResource(R.string.loading_sessions),
                modifier = Modifier.padding(contentPadding),
            )

            SessionBrowserPhase.LIST -> SessionList(
                sessions = state.sessions,
                hasMoreSessions = state.hasMoreSessions,
                isLoadingMoreSessions = state.isLoadingMoreSessions,
                onOpenSession = onOpenSession,
                onLoadMore = onLoadMore,
                modifier = Modifier.padding(contentPadding),
            )

            SessionBrowserPhase.LOADING_TRANSCRIPT -> LoadingState(
                label = stringResource(R.string.loading_transcript),
                modifier = Modifier.padding(contentPadding),
            )

            SessionBrowserPhase.TRANSCRIPT -> Transcript(
                state = state,
                onLoadOlder = onLoadOlder,
                onDraftChanged = onDraftChanged,
                onSend = onSend,
                onStop = onStop,
                onGuidanceDraftChanged = onGuidanceDraftChanged,
                onSubmitGuidance = onSubmitGuidance,
                onPendingChoice = onPendingChoice,
                onPendingOtherChanged = onPendingOtherChanged,
                onPendingSubmit = onPendingSubmit,
                onPendingConfirm = onPendingConfirm,
                onPendingCancelConfirmation = onPendingCancelConfirmation,
                onRetryControl = onRetryControl,
                disclosureRegistry = disclosureRegistry,
                onDisclosureToggle = onDisclosureToggle,
                onDisclosureFocus = onDisclosureFocus,
                modifier = Modifier.padding(contentPadding),
            )

            SessionBrowserPhase.AUTHENTICATION_REQUIRED -> RecoveryState(
                message = state.message.orEmpty(),
                primaryLabel = stringResource(R.string.back_to_connection),
                onPrimary = onReconnect,
                modifier = Modifier.padding(contentPadding),
            )

            SessionBrowserPhase.ERROR -> RecoveryState(
                message = state.message.orEmpty(),
                primaryLabel = stringResource(R.string.try_again),
                onPrimary = onRefresh,
                secondaryLabel = stringResource(R.string.back_to_connection),
                onSecondary = onReconnect,
                modifier = Modifier.padding(contentPadding),
            )
        }
    }
}

@Composable
private fun LoadingState(label: String, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        CircularProgressIndicator(modifier = Modifier.size(28.dp), strokeWidth = 3.dp)
        Spacer(Modifier.height(14.dp))
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun SessionList(
    sessions: List<SessionProjection>,
    hasMoreSessions: Boolean,
    isLoadingMoreSessions: Boolean,
    onOpenSession: (SessionKey) -> Unit,
    onLoadMore: () -> Unit,
    modifier: Modifier = Modifier,
) {
    if (sessions.isEmpty()) {
        Column(
            modifier = modifier
                .fillMaxSize()
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                text = stringResource(R.string.no_sessions),
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        return
    }

    LazyColumn(modifier = modifier.fillMaxSize()) {
        items(sessions, key = { it.sessionKey.value }) { session ->
            SessionRow(session, onOpenSession)
            HorizontalDivider(modifier = Modifier.padding(horizontal = 20.dp))
        }
        if (hasMoreSessions || isLoadingMoreSessions) {
            item(key = "load-more-sessions") {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    OutlinedButton(
                        onClick = onLoadMore,
                        enabled = !isLoadingMoreSessions,
                    ) {
                        if (isLoadingMoreSessions) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(18.dp),
                                strokeWidth = 2.dp,
                            )
                            Spacer(Modifier.width(8.dp))
                        }
                        Text(stringResource(R.string.load_more))
                    }
                }
            }
        }
    }
}

@Composable
private fun SessionRow(
    session: SessionProjection,
    onOpenSession: (SessionKey) -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onOpenSession(session.sessionKey) }
            .padding(horizontal = 20.dp, vertical = 16.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = session.title?.ifBlank { session.sessionKey.value }
                    ?: session.sessionKey.value,
                modifier = Modifier
                    .weight(1f)
                    .clickable { onOpenSession(session.sessionKey) },
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (session.isActive) {
                Spacer(Modifier.width(12.dp))
                Text(
                    text = stringResource(R.string.active_session),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
        }
        val preview = session.preview.orEmpty()
        if (preview.isNotBlank()) {
            Spacer(Modifier.height(5.dp))
            Text(
                text = preview,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Text(
                text = stringResource(R.string.messages_count, session.messageCount),
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            session.model?.takeIf(String::isNotBlank)?.let { model ->
                Text(
                    text = model,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

@Composable
private fun Transcript(
    state: SessionBrowserUiState,
    onLoadOlder: () -> Unit,
    onDraftChanged: (String) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
    onGuidanceDraftChanged: (String) -> Unit,
    onSubmitGuidance: () -> Unit,
    onPendingChoice: (String) -> Unit,
    onPendingOtherChanged: (String) -> Unit,
    onPendingSubmit: () -> Unit,
    onPendingConfirm: () -> Unit,
    onPendingCancelConfirmation: () -> Unit,
    onRetryControl: () -> Unit,
    disclosureRegistry: ConversationDisclosureRegistry,
    onDisclosureToggle: (ConversationDisclosureStateKey) -> Unit,
    onDisclosureFocus: (ConversationDisclosureStateKey) -> Unit,
    modifier: Modifier = Modifier,
) {
    val sessionKey = state.selectedSession?.sessionKey
    var followState by remember(sessionKey) { mutableStateOf(TranscriptFollowState()) }
    var guidanceExpanded by remember(
        sessionKey,
        state.realtime?.running,
        state.pendingInteraction.requestId,
    ) { mutableStateOf(false) }
    var longRunningExpanded by remember(sessionKey) { mutableStateOf(true) }
    var selectedLongRunningKind by remember(sessionKey) {
        mutableStateOf<LongRunningWorkKind?>(null)
    }
    val disclosureSessionKey = sessionKey?.value.orEmpty()
    val disclosureState = disclosureRegistry.state(disclosureSessionKey)
    val baseline = state.realtime?.transcript ?: state.transcript
    val timeline = state.realtime?.timeline.orEmpty()
    val useStructuredTimeline = timeline.isNotEmpty()
    val turnProjectionCache = remember(sessionKey) {
        val projector = ConversationTurnProjector()
        ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = projector::project,
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
        )
    }
    val conversationTurns = remember(baseline, state.realtime, turnProjectionCache) {
        baseline?.let { transcript ->
            turnProjectionCache.project(transcript, state.realtime)
        }.orEmpty()
    }
    val liveMessages = state.realtime?.liveMessages.orEmpty()
        .takeUnless { useStructuredTimeline }
        .orEmpty()
    val streamingText = state.realtime?.streamingAssistantText.orEmpty()
        .takeUnless { useStructuredTimeline }
        .orEmpty()
    val streamingReasoningText = state.realtime?.streamingReasoningText.orEmpty()
        .takeUnless { useStructuredTimeline }
        .orEmpty()
    val tools = state.realtime?.tools.orEmpty()
        .takeUnless { useStructuredTimeline }
        .orEmpty()
    val showWorking = state.realtime?.running == true &&
        !useStructuredTimeline &&
        streamingText.isBlank() &&
        streamingReasoningText.isBlank() &&
        tools.isEmpty()
    val workingText = stringResource(R.string.working)
    val legacyTurns = remember(
        liveMessages,
        streamingText,
        streamingReasoningText,
        tools,
        showWorking,
        workingText,
        useStructuredTimeline,
    ) {
        if (useStructuredTimeline) {
            emptyList()
        } else {
            buildList {
                liveMessages.forEachIndexed { index, message ->
                    add(
                        ConversationTurnUiModel(
                            key = "turn:legacy:message:$index:${message.text.hashCode()}",
                            userPrompt = null,
                            thinking = message.reasoning,
                            statusText = "",
                            tools = emptyList(),
                            response = message.text,
                            status = when (message.status) {
                                LiveMessageStatus.COMPLETE -> ConversationTurnStatus.COMPLETE
                                LiveMessageStatus.ERROR -> ConversationTurnStatus.ERROR
                            },
                        ),
                    )
                }
                if (
                    streamingText.isNotBlank() ||
                    streamingReasoningText.isNotBlank() ||
                    tools.isNotEmpty() ||
                    showWorking
                ) {
                    add(
                        ConversationTurnUiModel(
                            key = "turn:legacy:active",
                            userPrompt = null,
                            thinking = streamingReasoningText,
                            statusText = if (showWorking) workingText else "",
                            tools = tools.map(LiveToolProjection::toConversationToolUiModel),
                            response = streamingText,
                            status = ConversationTurnStatus.STREAMING,
                        ),
                    )
                }
            }
        }
    }
    val historyHeaderItems = if (state.hasOlderMessages || state.isLoadingOlderMessages) 1 else 0
    val renderedItemKeysCache = remember(sessionKey) { TranscriptRenderedItemKeysCache() }
    val renderedItemKeys = renderedItemKeysCache.update(
        hasHistoryHeader = historyHeaderItems == 1,
        conversationTurns = conversationTurns,
        legacyTurns = legacyTurns,
    )
    val totalItems = renderedItemKeys.size
    val listState = remember(sessionKey) {
        LazyListState(firstVisibleItemIndex = (totalItems - 1).coerceAtLeast(0))
    }
    val scrollAnchorTracker = remember(sessionKey) { TranscriptScrollAnchorTracker() }
    var previousRenderedItemKeys by remember(sessionKey) {
        mutableStateOf<List<String>>(emptyList())
    }
    val anchorBeforeItemsChanged = if (
        !followState.isFollowingLatest &&
        previousRenderedItemKeys.isNotEmpty() &&
        previousRenderedItemKeys != renderedItemKeys
    ) {
        scrollAnchorTracker.anchorFor(renderedItemKeys)
    } else {
        null
    }
    val followRequestCoalescer = remember(sessionKey) { TranscriptFollowRequestCoalescer() }
    val coroutineScope = rememberCoroutineScope()
    val revisionTurns = if (legacyTurns.isNotEmpty()) legacyTurns else conversationTurns
    val currentExecution = remember(state.realtime?.running, revisionTurns) {
        currentExecutionPresentation(
            running = state.realtime?.running == true,
            turns = revisionTurns,
        )
    }
    val longRunningWork = remember(state.realtime?.running, revisionTurns) {
        longRunningWorkPresentation(
            running = state.realtime?.running == true,
            turns = revisionTurns,
        )
    }
    val contentRevision = revisionTurns.latestContentRevision(
        connectionEpoch = state.realtime?.connectionEpoch,
        eventOrdinal = state.realtime?.lastEventOrdinal,
    )
    val minimapMarkers = remember(revisionTurns) {
        buildTranscriptMinimapMarkers(revisionTurns)
    }

    SideEffect {
        if (previousRenderedItemKeys != renderedItemKeys) {
            previousRenderedItemKeys = renderedItemKeys
        }
    }
    LaunchedEffect(sessionKey, renderedItemKeys, anchorBeforeItemsChanged) {
        anchorBeforeItemsChanged?.let { anchor ->
            val index = renderedItemKeys.indexOf(anchor.key)
            if (index >= 0) {
                listState.scrollToItem(index, anchor.scrollOffset)
            }
        }
    }

    LaunchedEffect(sessionKey, contentRevision) {
        followState = followState.onContentChanged()
        if (followState.shouldScrollToLatest && totalItems > 0) {
            followRequestCoalescer.request()
            withFrameNanos { }
            val layoutInfo = listState.layoutInfo
            val lastVisibleItem = layoutInfo.visibleItemsInfo.lastOrNull()
            val viewportAtLatest = isTranscriptViewportAtLatest(
                totalItems = totalItems,
                lastVisibleIndex = lastVisibleItem?.index,
                lastVisibleEndOffset = lastVisibleItem?.let { it.offset + it.size },
                viewportEndOffset = layoutInfo.viewportEndOffset,
            )
            if (
                followRequestCoalescer.consumeFrame(
                    isViewportAtLatest = viewportAtLatest || !followState.shouldScrollToLatest,
                    isUserScrollingBackward =
                        listState.isScrollInProgress && listState.lastScrolledBackward,
                )
            ) {
                listState.scrollToItem(totalItems - 1, Int.MAX_VALUE)
            }
        }
    }
    LaunchedEffect(listState, totalItems, renderedItemKeys) {
        snapshotFlow {
            val layoutInfo = listState.layoutInfo
            val lastVisibleItem = layoutInfo.visibleItemsInfo.lastOrNull()
            TranscriptViewport(
                isScrollInProgress = listState.isScrollInProgress,
                lastScrolledBackward = listState.lastScrolledBackward,
                firstVisibleIndex = listState.firstVisibleItemIndex,
                firstVisibleScrollOffset = listState.firstVisibleItemScrollOffset,
                isAtLatest = isTranscriptViewportAtLatest(
                    totalItems = totalItems,
                    lastVisibleIndex = lastVisibleItem?.index,
                    lastVisibleEndOffset = lastVisibleItem?.let { it.offset + it.size },
                    viewportEndOffset = layoutInfo.viewportEndOffset,
                ),
            )
        }.collectLatest { viewport ->
            scrollAnchorTracker.update(
                renderedItemKeys = renderedItemKeys,
                firstVisibleIndex = viewport.firstVisibleIndex,
                firstVisibleScrollOffset = viewport.firstVisibleScrollOffset,
            )
            followState = followState.onViewportChanged(
                isScrollInProgress = viewport.isScrollInProgress,
                lastScrolledBackward = viewport.lastScrolledBackward,
                isAtLatest = viewport.isAtLatest,
            )
        }
    }
    ProvideHermesTranscriptDesignSystem {
        val colors = HermesTranscriptThemeTokens.colors
        val metrics = HermesTranscriptThemeTokens.metrics
        Column(
            modifier = modifier
                .fillMaxSize()
                .background(colors.background)
                .imePadding(),
        ) {
            HermesSessionStatusStrip(
                state = state,
                onRetryControl = onRetryControl,
            )
            HermesCurrentExecutionStrip(currentExecution)
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
            ) {
                val showMinimap = shouldShowTranscriptMinimap(
                    markerCount = minimapMarkers.size,
                    viewportScrollable = listState.canScrollBackward || listState.canScrollForward,
                )
                LazyColumn(
                    state = listState,
                    modifier = Modifier
                        .fillMaxSize()
                        .testTag("transcript-list"),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(
                        start = metrics.horizontalContentInset,
                        top = 12.dp,
                        end = if (showMinimap) {
                            metrics.minimumTouchTarget + 12.dp
                        } else {
                            metrics.horizontalContentInset
                        },
                        bottom = 12.dp,
                    ),
                    verticalArrangement = Arrangement.spacedBy(metrics.turnGap),
                ) {
                if (state.hasOlderMessages || state.isLoadingOlderMessages) {
                    item(
                        key = TRANSCRIPT_HISTORY_HEADER_KEY,
                        contentType = "transcript-control",
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.Center,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            OutlinedButton(
                                onClick = onLoadOlder,
                                enabled = !state.isLoadingOlderMessages,
                            ) {
                                if (state.isLoadingOlderMessages) {
                                    CircularProgressIndicator(
                                        modifier = Modifier.size(18.dp),
                                        strokeWidth = 2.dp,
                                    )
                                    Spacer(Modifier.width(8.dp))
                                }
                                Text(stringResource(R.string.load_earlier_messages))
                            }
                        }
                    }
                }
                items(
                    items = conversationTurns,
                    key = ConversationTurnUiModel::key,
                    contentType = { "canonical-turn" },
                ) { turn ->
                    ConversationTurnContent(
                        turn = turn,
                        disclosureState = disclosureState,
                        onDisclosureToggle = onDisclosureToggle,
                    )
                }
                items(
                    items = legacyTurns,
                    key = ConversationTurnUiModel::key,
                    contentType = { "legacy-turn" },
                ) { turn ->
                    ConversationTurnContent(
                        turn = turn,
                        disclosureState = disclosureState,
                        onDisclosureToggle = onDisclosureToggle,
                    )
                }
                }
                if (showMinimap) {
                    val activeMarkerIndex = activeTranscriptMinimapMarkerIndex(
                        markers = minimapMarkers,
                        renderedItemKeys = renderedItemKeys,
                        firstVisibleItemIndex = listState.firstVisibleItemIndex,
                    )
                    TranscriptMinimap(
                        markers = minimapMarkers,
                        activeMarkerIndex = activeMarkerIndex,
                        onMarkerSelected = { marker ->
                            marker.disclosureSection?.let { section ->
                                onDisclosureFocus(
                                    ConversationDisclosureStateKey(marker.turnKey, section),
                                )
                            }
                            val itemIndex = renderedItemKeys.indexOf(marker.turnKey)
                            if (itemIndex >= 0) {
                                followState = followState.copy(isFollowingLatest = false)
                                coroutineScope.launch {
                                    listState.animateScrollToItem(itemIndex)
                                }
                            }
                        },
                        modifier = Modifier
                            .align(Alignment.CenterEnd)
                            .padding(vertical = 16.dp, horizontal = 4.dp),
                    )
                }
                if (!followState.isFollowingLatest) {
                    OutlinedButton(
                        onClick = {
                            followState = followState.onJumpToLatest()
                            if (totalItems > 0) {
                                coroutineScope.launch {
                                    listState.animateScrollToItem(totalItems - 1, Int.MAX_VALUE)
                                }
                            }
                        },
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .padding(bottom = 12.dp)
                            .testTag("jump-to-latest"),
                    ) {
                        Text(
                            if (followState.unseenUpdates > 0) {
                                stringResource(
                                    R.string.back_to_latest_updates,
                                    followState.unseenUpdates,
                                )
                            } else {
                                stringResource(R.string.back_to_latest)
                            },
                        )
                    }
                }
            }
            HorizontalDivider()
            val pendingInput = authoritativePendingInput(state.control)
            val showsPendingFeedback = when (state.pendingInteraction.outcome) {
                is PendingInputInteractionOutcome.Failed,
                PendingInputInteractionOutcome.DeliveryUnknown,
                PendingInputInteractionOutcome.ResolvedElsewhere,
                -> true

                null,
                PendingInputInteractionOutcome.Accepted,
                PendingInputInteractionOutcome.RetryAvailable,
                -> false
            }
            if (pendingInput == null) {
                PendingInputFeedback(state.pendingInteraction.outcome)
            }
            if (showsPendingFeedback) {
                HorizontalDivider()
            }
            CommandFeedback(state)
            QueuedPromptPanel(
                window = queuedPromptWindow(state.commands),
                visible = state.realtime?.running == true,
            )
            HermesLongRunningWorkDock(
                presentation = longRunningWork,
                expanded = longRunningExpanded,
                selectedKind = selectedLongRunningKind,
                onExpandedChange = { longRunningExpanded = it },
                onItemClick = { item ->
                    selectedLongRunningKind = item.kind
                    onDisclosureFocus(item.sectionKey)
                    val itemIndex = renderedItemKeys.indexOf(item.turnKey)
                    if (itemIndex >= 0) {
                        coroutineScope.launch {
                            listState.animateScrollToItem(itemIndex)
                        }
                    }
                },
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
            )
            when (sessionBottomInputSurface(pendingInput)) {
                SessionBottomInputSurface.Decision -> HermesPendingInputDock(
                    pendingInput = checkNotNull(pendingInput),
                    interaction = state.pendingInteraction,
                    mutationEnabled = state.canRespondToPendingInput,
                    onChoice = onPendingChoice,
                    onOtherChanged = onPendingOtherChanged,
                    onSubmit = onPendingSubmit,
                    onConfirm = onPendingConfirm,
                    onCancelConfirmation = onPendingCancelConfirmation,
                )

                SessionBottomInputSurface.Composer -> {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .semantics { isTraversalGroup = true }
                            .testTag("composer-input-group"),
                    ) {
                        val guidanceActionVisible = sessionComposerGuidanceActionVisible(
                            running = state.realtime?.running == true,
                            canMutate = state.control.canMutate &&
                                MobileControlMethods.SESSION_STEER in state.controlAvailableMethods,
                            isInterrupting = state.isInterrupting,
                        )
                        val guidanceMode = guidanceActionVisible && guidanceExpanded
                        val activeDraft = if (guidanceMode) {
                            state.guidance.draft
                        } else {
                            state.composer.draft
                        }
                        val onActiveDraftChanged = if (guidanceMode) {
                            onGuidanceDraftChanged
                        } else {
                            onDraftChanged
                        }
                        val onActiveSubmit = if (guidanceMode) {
                            onSubmitGuidance
                        } else {
                            onSend
                        }
                        val guidanceAvailable = state.canGuide &&
                            state.guidance.inFlightRequestId == null
                        val composerPresentation = transcriptComposerPresentation(
                            running = state.realtime?.running == true,
                            isInterrupting = state.isInterrupting,
                            guidanceMode = guidanceMode,
                            canEdit = if (guidanceMode) guidanceAvailable else state.canEditComposer,
                            canSend = if (guidanceMode) guidanceAvailable else state.canSend,
                            canStop = state.canStop,
                            hasDraft = activeDraft.isNotBlank(),
                        )
                        val voiceInput = rememberSessionVoiceInput(
                            sessionKey = state.selectedSession?.sessionKey?.value.orEmpty(),
                            draft = activeDraft,
                            enabled = composerPresentation.inputEnabled,
                            onDraftChanged = onActiveDraftChanged,
                        )
                        HermesAgentComposer(
                            draft = activeDraft,
                            presentation = composerPresentation,
                            isInterrupting = state.isInterrupting,
                            guidanceMode = guidanceMode,
                            guidanceActionVisible = guidanceActionVisible,
                            onDraftChanged = onActiveDraftChanged,
                            onSubmit = onActiveSubmit,
                            onStop = onStop,
                            onGuideMode = { guidanceExpanded = true },
                            onQueueMode = { guidanceExpanded = false },
                            guidanceState = state.guidance,
                            voiceInputState = voiceInput.state,
                            onVoiceAction = voiceInput.onAction,
                            modifier = Modifier
                                .semantics { traversalIndex = 0f }
                                .testTag("composer-traversal-controls"),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun PendingInputFeedback(outcome: PendingInputInteractionOutcome?) {
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    val message = when (outcome) {
        is PendingInputInteractionOutcome.Failed -> outcome.summary
        PendingInputInteractionOutcome.DeliveryUnknown ->
            stringResource(R.string.pending_input_delivery_unknown)

        PendingInputInteractionOutcome.ResolvedElsewhere ->
            stringResource(R.string.pending_input_resolved_elsewhere)

        null,
        PendingInputInteractionOutcome.Accepted,
        PendingInputInteractionOutcome.RetryAvailable,
        -> null
    }
    val color = when (outcome) {
        is PendingInputInteractionOutcome.Failed -> colors.error
        PendingInputInteractionOutcome.DeliveryUnknown -> colors.warning
        else -> colors.muted
    }
    val feedbackSemantics = pendingInputFeedbackSemantics(outcome)
    message?.let {
        Text(
            text = it,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp)
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

@Composable
private fun QueuedPromptPanel(
    window: QueuedPromptWindow,
    visible: Boolean,
) {
    if (!visible || window.items.isEmpty()) return
    val colors = HermesTranscriptThemeTokens.colors
    val typography = HermesTranscriptThemeTokens.typography
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 6.dp)
            .semantics { liveRegion = LiveRegionMode.Polite }
            .testTag("queued-prompt-panel"),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        Text(
            text = stringResource(R.string.queued_prompts, window.totalCount),
            style = typography.meta,
            color = colors.muted,
        )
        if (window.hiddenBeforeCount > 0) {
            Text(
                text = stringResource(R.string.queued_prompts_earlier, window.hiddenBeforeCount),
                style = typography.meta,
                color = colors.muted,
            )
        }
        window.items.forEachIndexed { index, item ->
            Text(
                text = "${window.hiddenBeforeCount + index + 1}. ${item.preview}",
                modifier = Modifier.testTag("queued-prompt:${item.requestId.value}"),
                style = typography.process,
                color = colors.tool,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

@Composable
private fun CommandFeedback(state: SessionBrowserUiState) {
    val activeRequestId = state.composer.submitted?.requestId ?: state.interruptRequestId
    val command = activeRequestId
        ?.let(state.commands.commands::get)
        ?: state.commands.commands.values.lastOrNull()
        ?: return
    val text = when (command.phase) {
        CommandPhase.UNKNOWN -> stringResource(R.string.command_delivery_unknown)
        CommandPhase.FAILED -> command.failureSummary
            ?.takeIf(String::isNotBlank)
            ?: stringResource(R.string.command_failed)
        CommandPhase.REJECTED -> stringResource(R.string.command_rejected)
        CommandPhase.SENDING,
        CommandPhase.ACCEPTED,
        CommandPhase.QUEUED,
        CommandPhase.COMPLETE,
        -> return
    }
    Text(
        text = text,
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp)
            .testTag("command-feedback"),
        style = MaterialTheme.typography.labelMedium,
        color = if (command.phase == CommandPhase.UNKNOWN) {
            MaterialTheme.colorScheme.tertiary
        } else {
            MaterialTheme.colorScheme.error
        },
    )
}

internal fun historicalMessageKey(
    index: Int,
    message: SessionMessageProjection,
): String = "history-$index-${message.messageId ?: "missing"}"

private fun LiveToolProjection.toConversationToolUiModel(): ConversationToolUiModel {
    val arguments = payload.firstPayload("arguments", "args", "input", "parameters")
    val context = HermesMessagePresentation.payload(payload["context"])
        .visibleText()
        .takeIf(String::isNotBlank)
    val call = HermesMessagePresentation.toolCall(
        name = name,
        arguments = arguments,
        context = context,
    )
    val result = HermesMessagePresentation.toolResult(payload)
    val safeContext = HermesMessagePresentation.payloadText(context)
        .visibleText()
        .takeIf(String::isNotBlank)
    val safeArguments = HermesMessagePresentation.payload(arguments)
        .visibleText()
        .takeIf(String::isNotBlank)
    return ConversationToolUiModel(
        key = key,
        toolId = key,
        name = name,
        callLabel = call.label,
        context = safeContext,
        arguments = safeArguments,
        argumentDetails = call.details,
        output = result.text,
        resultDetails = result.details,
        status = when (status) {
            LiveToolStatus.RUNNING -> ConversationToolStatus.RUNNING
            LiveToolStatus.COMPLETE -> ConversationToolStatus.COMPLETE
            LiveToolStatus.ERROR -> ConversationToolStatus.ERROR
            LiveToolStatus.INTERRUPTED -> ConversationToolStatus.INTERRUPTED
            LiveToolStatus.UNKNOWN -> ConversationToolStatus.UNKNOWN
        },
    )
}

internal sealed interface TranscriptTimelineRevision {
    data class Realtime(
        val connectionEpoch: Long,
        val lastEventOrdinal: Long,
    ) : TranscriptTimelineRevision

    data class RestBaseline(
        val historyCount: Int,
        val lastHistoryId: Long?,
    ) : TranscriptTimelineRevision
}

internal fun transcriptTimelineRevision(
    connectionEpoch: Long?,
    lastEventOrdinal: Long?,
    historyCount: Int,
    lastHistoryId: Long?,
): TranscriptTimelineRevision = if (connectionEpoch != null && lastEventOrdinal != null) {
    TranscriptTimelineRevision.Realtime(connectionEpoch, lastEventOrdinal)
} else {
    TranscriptTimelineRevision.RestBaseline(historyCount, lastHistoryId)
}

private data class TranscriptViewport(
    val isScrollInProgress: Boolean,
    val lastScrolledBackward: Boolean,
    val firstVisibleIndex: Int,
    val firstVisibleScrollOffset: Int,
    val isAtLatest: Boolean,
)

internal enum class TranscriptComposerPrimaryAction {
    Send,
    Queue,
    Guide,
}

internal fun transcriptComposerPrimaryAction(
    running: Boolean,
    isInterrupting: Boolean,
    guidanceMode: Boolean = false,
): TranscriptComposerPrimaryAction = when {
    !running && !isInterrupting -> TranscriptComposerPrimaryAction.Send
    guidanceMode -> TranscriptComposerPrimaryAction.Guide
    else -> TranscriptComposerPrimaryAction.Queue
}

private fun JsonObject.firstPayload(vararg keys: String): JsonElement? =
    keys.firstNotNullOfOrNull { key ->
        get(key)?.takeIf { value ->
            HermesMessagePresentation.readableText(value).isNotBlank()
        }
    }

@Composable
private fun RecoveryState(
    message: String,
    primaryLabel: String,
    onPrimary: () -> Unit,
    modifier: Modifier = Modifier,
    secondaryLabel: String? = null,
    onSecondary: (() -> Unit)? = null,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(18.dp))
        Button(onClick = onPrimary) { Text(primaryLabel) }
        if (secondaryLabel != null && onSecondary != null) {
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = onSecondary) { Text(secondaryLabel) }
        }
    }
}

private fun JsonElement.displayText(): String = when (this) {
    JsonNull -> ""
    is JsonPrimitive -> content
    is JsonArray -> joinToString(separator = "\n") { it.displayText() }
        .trim()
    is JsonObject -> {
        val preferred = listOf("text", "content", "message", "rendered")
            .firstNotNullOfOrNull { key -> get(key)?.displayText()?.takeIf(String::isNotBlank) }
        preferred ?: values.joinToString(separator = "\n") { it.displayText() }.trim()
    }
}
