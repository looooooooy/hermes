package app.hermesmobile.sessions

sealed interface RealtimeResyncResult {
    data class Ready(
        val projection: RealtimeSessionProjection,
    ) : RealtimeResyncResult

    data object AuthenticationRequired : RealtimeResyncResult

    data class Unavailable(
        val summary: String,
    ) : RealtimeResyncResult
}

/** Enforces REST-first recovery before a new WebSocket event epoch is accepted. */
class RealtimeResyncCoordinator(
    private val transcriptSource: SessionTranscriptSource,
    private val reducer: RealtimeSessionReducer,
) {
    suspend fun resync(
        current: RealtimeSessionProjection,
        connectionEpoch: Long,
        profile: String? = null,
    ): RealtimeResyncResult {
        require(connectionEpoch > current.connectionEpoch) {
            "Reconnect epoch must advance monotonically."
        }
        return when (
            val result = transcriptSource.loadMessages(
                sessionKey = current.sessionKey,
                profile = profile,
            )
        ) {
            is SessionRepositoryResult.Data -> RealtimeResyncResult.Ready(
                reducer.resync(current, result.value, connectionEpoch),
            )
            SessionRepositoryResult.AuthenticationRequired ->
                RealtimeResyncResult.AuthenticationRequired
            is SessionRepositoryResult.Unavailable ->
                RealtimeResyncResult.Unavailable(result.summary)
        }
    }
}
