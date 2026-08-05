package app.hermesmobile.sessions

import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class RealtimeResyncCoordinatorTest {
    @Test
    fun `reconnect installs REST baseline before accepting the new event epoch`() = runTest {
        val reducer = RealtimeSessionReducer()
        val oldTranscript = transcript(SessionKey("tip-1"))
        val newTranscript = transcript(SessionKey("tip-2"))
        val current = reducer.seed(oldTranscript, RuntimeSessionId("runtime-1"), connectionEpoch = 1)
        val source = FakeSource(SessionRepositoryResult.Data(newTranscript))
        val coordinator = RealtimeResyncCoordinator(source, reducer)

        val result = coordinator.resync(current, connectionEpoch = 2)

        val ready = assertIs<RealtimeResyncResult.Ready>(result)
        assertEquals(2, ready.projection.connectionEpoch)
        assertEquals(SessionKey("tip-2"), ready.projection.lineageTip)
        assertEquals(listOf(SessionKey("stored-1")), source.requests)
    }

    @Test
    fun `authentication failure does not advance connection epoch`() = runTest {
        val reducer = RealtimeSessionReducer()
        val current = reducer.seed(
            transcript(SessionKey("tip-1")),
            RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        )
        val coordinator = RealtimeResyncCoordinator(
            FakeSource(SessionRepositoryResult.AuthenticationRequired),
            reducer,
        )

        val result = coordinator.resync(current, connectionEpoch = 2)

        assertIs<RealtimeResyncResult.AuthenticationRequired>(result)
        assertEquals(1, current.connectionEpoch)
    }

    private fun transcript(tip: SessionKey) = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = tip,
        messages = emptyList(),
        pagination = TranscriptPagination(limit = 200, offset = 0, returned = 0),
    )

    private class FakeSource(
        private val result: SessionRepositoryResult<SessionTranscript>,
    ) : SessionTranscriptSource {
        val requests = mutableListOf<SessionKey>()

        override suspend fun loadMessages(
            sessionKey: SessionKey,
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionRepositoryResult<SessionTranscript> {
            requests += sessionKey
            return result
        }
    }
}
