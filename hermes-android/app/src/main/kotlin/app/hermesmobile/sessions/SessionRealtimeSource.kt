package app.hermesmobile.sessions

import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow

enum class RealtimeConnectionStatus {
    IDLE,
    CONNECTING,
    LIVE,
    RECONNECTING,
    DISCONNECTED,
    UNSUPPORTED,
    ERROR,
}

sealed interface SessionRealtimeUpdate {
    data class Connection(
        val status: RealtimeConnectionStatus,
        val controlStatus: RealtimeControlStatus,
        val message: String? = null,
    ) : SessionRealtimeUpdate

    data class Projection(
        val projection: RealtimeSessionProjection,
    ) : SessionRealtimeUpdate
}

fun interface SessionRealtimeSource {
    fun observe(
        session: SessionProjection,
        baseline: SessionTranscript,
    ): Flow<SessionRealtimeUpdate>

    companion object {
        val None = SessionRealtimeSource { _, _ -> emptyFlow() }
    }
}
