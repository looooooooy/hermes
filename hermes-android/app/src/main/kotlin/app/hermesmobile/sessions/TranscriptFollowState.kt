package app.hermesmobile.sessions

internal const val MAX_TRANSCRIPT_UNSEEN_UPDATES = 999
internal const val TRANSCRIPT_HISTORY_HEADER_KEY = "load-older-messages"

data class TranscriptFollowState(
    val isFollowingLatest: Boolean = true,
    val unseenUpdates: Int = 0,
) {
    val shouldScrollToLatest: Boolean
        get() = isFollowingLatest

    fun onContentChanged(updateCount: Int = 1): TranscriptFollowState {
        if (updateCount <= 0 || isFollowingLatest) return this
        val nextCount = (unseenUpdates.toLong() + updateCount)
            .coerceAtMost(MAX_TRANSCRIPT_UNSEEN_UPDATES.toLong())
            .toInt()
        return copy(unseenUpdates = nextCount)
    }

    fun onViewportChanged(
        isScrollInProgress: Boolean,
        lastScrolledBackward: Boolean,
        isAtLatest: Boolean,
    ): TranscriptFollowState = when {
        isScrollInProgress && lastScrolledBackward -> copy(isFollowingLatest = false)
        !isScrollInProgress && isAtLatest -> copy(
            isFollowingLatest = true,
            unseenUpdates = 0,
        )
        else -> this
    }

    fun onJumpToLatest(): TranscriptFollowState = copy(
        isFollowingLatest = true,
        unseenUpdates = 0,
    )

    fun onSessionChanged(): TranscriptFollowState = TranscriptFollowState()
}

internal class TranscriptFollowRequestCoalescer {
    private var pending: Boolean = false

    fun request() {
        pending = true
    }

    fun consumeFrame(
        isViewportAtLatest: Boolean,
        isUserScrollingBackward: Boolean = false,
    ): Boolean {
        val shouldFollow = pending && !isViewportAtLatest && !isUserScrollingBackward
        pending = false
        return shouldFollow
    }
}

internal class TranscriptRenderedItemKeysCache {
    private var renderedItemKeys: List<String> = emptyList()

    fun update(
        hasHistoryHeader: Boolean,
        conversationTurns: List<ConversationTurnUiModel>,
        legacyTurns: List<ConversationTurnUiModel>,
    ): List<String> {
        val expectedSize = (if (hasHistoryHeader) 1 else 0) +
            conversationTurns.size +
            legacyTurns.size
        if (
            renderedItemKeys.size == expectedSize &&
            renderedItemKeys.matchesStructure(hasHistoryHeader, conversationTurns, legacyTurns)
        ) {
            return renderedItemKeys
        }
        return buildList(expectedSize) {
            if (hasHistoryHeader) add(TRANSCRIPT_HISTORY_HEADER_KEY)
            conversationTurns.forEach { add(it.key) }
            legacyTurns.forEach { add(it.key) }
        }.also { renderedItemKeys = it }
    }

    private fun List<String>.matchesStructure(
        hasHistoryHeader: Boolean,
        conversationTurns: List<ConversationTurnUiModel>,
        legacyTurns: List<ConversationTurnUiModel>,
    ): Boolean {
        var index = 0
        if (hasHistoryHeader && get(index++) != TRANSCRIPT_HISTORY_HEADER_KEY) return false
        conversationTurns.forEach { turn ->
            if (get(index++) != turn.key) return false
        }
        legacyTurns.forEach { turn ->
            if (get(index++) != turn.key) return false
        }
        return true
    }
}

internal data class TranscriptScrollAnchor(
    val key: String,
    val scrollOffset: Int,
)

internal class TranscriptScrollAnchorTracker {
    private var renderedItemKeys: List<String> = emptyList()
    private var firstVisibleIndex: Int = 0
    private var firstVisibleScrollOffset: Int = 0

    fun update(
        renderedItemKeys: List<String>,
        firstVisibleIndex: Int,
        firstVisibleScrollOffset: Int,
    ) {
        this.renderedItemKeys = renderedItemKeys
        this.firstVisibleIndex = firstVisibleIndex
        this.firstVisibleScrollOffset = firstVisibleScrollOffset
    }

    fun anchorFor(nextRenderedItemKeys: List<String>): TranscriptScrollAnchor? {
        val firstVisibleKey = renderedItemKeys.getOrNull(firstVisibleIndex)
        val survivingKey = renderedItemKeys
            .drop(firstVisibleIndex)
            .firstOrNull(nextRenderedItemKeys::contains)
            ?: return null
        return TranscriptScrollAnchor(
            key = survivingKey,
            scrollOffset = if (survivingKey == firstVisibleKey) firstVisibleScrollOffset else 0,
        )
    }
}

internal data class TranscriptLatestContentRevision(
    val stableKey: String,
    val connectionEpoch: Long?,
    val eventOrdinal: Long?,
    val contentHash: Int,
)

internal fun List<ConversationTurnUiModel>.latestContentRevision(
    connectionEpoch: Long? = null,
    eventOrdinal: Long? = null,
): TranscriptLatestContentRevision? =
    lastOrNull()?.let { latest ->
        val realtimeCursorActive = connectionEpoch != null && eventOrdinal != null && eventOrdinal > 0
        TranscriptLatestContentRevision(
            stableKey = latest.key,
            connectionEpoch = connectionEpoch?.takeIf { realtimeCursorActive },
            eventOrdinal = eventOrdinal?.takeIf { realtimeCursorActive },
            contentHash = if (realtimeCursorActive) 0 else latest.hashCode(),
        )
    }

internal fun isTranscriptViewportAtLatest(
    totalItems: Int,
    lastVisibleIndex: Int?,
    lastVisibleEndOffset: Int?,
    viewportEndOffset: Int,
): Boolean {
    if (totalItems <= 0) return true
    if (lastVisibleIndex != totalItems - 1) return false
    return lastVisibleEndOffset != null && lastVisibleEndOffset <= viewportEndOffset
}
