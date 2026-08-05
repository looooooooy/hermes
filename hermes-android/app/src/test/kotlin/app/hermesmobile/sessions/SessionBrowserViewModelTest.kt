package app.hermesmobile.sessions

import app.hermesmobile.connection.MainDispatcherRule
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionPage
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.onCompletion
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Rule
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

@OptIn(ExperimentalCoroutinesApi::class)
class SessionBrowserViewModelTest {
    @get:Rule
    val mainDispatcherRule = MainDispatcherRule()

    @Test
    fun `preload surfaces authoritative session list`() = runTest {
        val source = FakeSource(
            sessionResult = SessionRepositoryResult.Data(
                SessionPage(listOf(session("stored-1", "First session")), 1, 20, 0),
            ),
        )
        val viewModel = SessionBrowserViewModel(source)

        viewModel.start()
        advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals(SessionBrowserPhase.LIST, state.phase)
        assertEquals("First session", state.sessions.single().title)
        assertFalse(state.isRefreshing)
        assertEquals(RealtimeControlStatus.SERVER_UPGRADE_REQUIRED, state.controlStatus)
    }

    @Test
    fun `load more appends the next authoritative session page`() = runTest {
        val requestedOffsets = mutableListOf<Int>()
        val source = object : SessionBrowserSource {
            override suspend fun loadSessions(
                limit: Int,
                offset: Int,
                profile: String?,
            ): SessionRepositoryResult<SessionPage> {
                requestedOffsets += offset
                return SessionRepositoryResult.Data(
                    when (offset) {
                        0 -> SessionPage(
                            sessions = listOf(session("stored-1", "First session")),
                            total = 2,
                            limit = limit,
                            offset = offset,
                        )
                        1 -> SessionPage(
                            sessions = listOf(session("stored-2", "Second session")),
                            total = 2,
                            limit = limit,
                            offset = offset,
                        )
                        else -> error("Unexpected session page offset: $offset")
                    },
                )
            }

            override suspend fun loadMessages(
                sessionKey: SessionKey,
                limit: Int,
                offset: Int,
                profile: String?,
            ): SessionRepositoryResult<SessionTranscript> = error("Not used")
        }
        val viewModel = SessionBrowserViewModel(source)
        viewModel.start()
        advanceUntilIdle()

        assertTrue(viewModel.state.value.hasMoreSessions)

        viewModel.loadMoreSessions()
        advanceUntilIdle()

        assertEquals(listOf(0, 1), requestedOffsets)
        assertEquals(
            listOf("First session", "Second session"),
            viewModel.state.value.sessions.map(SessionProjection::title),
        )
        assertFalse(viewModel.state.value.hasMoreSessions)
    }

    @Test
    fun `opening session loads REST transcript and keeps stable session key`() = runTest {
        val projection = session("stored-1", "First session")
        val transcript = transcript("stored-1", "hello")
        val source = FakeSource(
            sessionResult = SessionRepositoryResult.Data(SessionPage(listOf(projection), 1, 20, 0)),
            transcriptResults = mutableMapOf(
                SessionKey("stored-1") to SessionRepositoryResult.Data(transcript),
            ),
        )
        val viewModel = SessionBrowserViewModel(source)
        viewModel.start()
        advanceUntilIdle()

        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals(SessionBrowserPhase.TRANSCRIPT, state.phase)
        assertEquals(SessionKey("stored-1"), state.selectedSession?.sessionKey)
        assertEquals(JsonPrimitive("hello"), state.transcript?.messages?.single()?.content)
        assertNull(state.realtime)
    }

    @Test
    fun `opening session requests the recent transcript window for its profile`() = runTest {
        val projection = session(
            id = "stored-1",
            title = "First session",
            profile = "fox",
            messageCount = 120,
        )
        val source = FakeSource(
            sessionResult = SessionRepositoryResult.Data(
                SessionPage(listOf(projection), 1, 20, 0),
            ),
            transcriptResults = mutableMapOf(
                SessionKey("stored-1") to SessionRepositoryResult.Data(
                    transcript("stored-1", "recent"),
                ),
            ),
        )
        val viewModel = SessionBrowserViewModel(source)
        viewModel.start()
        advanceUntilIdle()

        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        assertEquals(
            listOf(MessageRequest(SessionKey("stored-1"), 20, 100, "fox")),
            source.messageRequests,
        )
    }

    @Test
    fun `loading older messages prepends the preceding transcript window`() = runTest {
        val projection = session(
            id = "stored-1",
            title = "First session",
            profile = "fox",
            messageCount = 45,
        )
        val requests = mutableListOf<MessageRequest>()
        val source = object : SessionBrowserSource {
            override suspend fun loadSessions(
                limit: Int,
                offset: Int,
                profile: String?,
            ): SessionRepositoryResult<SessionPage> = SessionRepositoryResult.Data(
                SessionPage(listOf(projection), 1, limit, offset),
            )

            override suspend fun loadMessages(
                sessionKey: SessionKey,
                limit: Int,
                offset: Int,
                profile: String?,
            ): SessionRepositoryResult<SessionTranscript> {
                requests += MessageRequest(sessionKey, limit, offset, profile)
                val text = when (offset) {
                    25 -> "recent"
                    5 -> "older"
                    else -> error("Unexpected transcript offset: $offset")
                }
                return SessionRepositoryResult.Data(
                    transcript("stored-1", text).copy(
                        pagination = TranscriptPagination(
                            limit = limit,
                            offset = offset,
                            returned = 1,
                        ),
                    ),
                )
            }
        }
        val viewModel = SessionBrowserViewModel(source)
        viewModel.start()
        advanceUntilIdle()
        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        assertTrue(viewModel.state.value.hasOlderMessages)
        viewModel.loadOlderMessages()
        advanceUntilIdle()

        assertEquals(
            listOf(
                MessageRequest(SessionKey("stored-1"), 20, 25, "fox"),
                MessageRequest(SessionKey("stored-1"), 20, 5, "fox"),
            ),
            requests,
        )
        assertEquals(
            listOf(JsonPrimitive("older"), JsonPrimitive("recent")),
            viewModel.state.value.transcript?.messages?.map { it.content },
        )
        assertEquals(5, viewModel.state.value.transcript?.pagination?.offset)
        assertTrue(viewModel.state.value.hasOlderMessages)
        assertFalse(viewModel.state.value.isLoadingOlderMessages)
    }

    @Test
    fun `sparse recent tail keeps prepending until the missing middle is filled`() = runTest {
        val projection = session(
            id = "stored-1",
            title = "Long session",
            messageCount = 1_000,
        )
        val requests = mutableListOf<MessageRequest>()
        val source = object : SessionBrowserSource {
            override suspend fun loadSessions(
                limit: Int,
                offset: Int,
                profile: String?,
            ): SessionRepositoryResult<SessionPage> = SessionRepositoryResult.Data(
                SessionPage(listOf(projection), 1, limit, offset),
            )

            override suspend fun loadMessages(
                sessionKey: SessionKey,
                limit: Int,
                offset: Int,
                profile: String?,
            ): SessionRepositoryResult<SessionTranscript> {
                requests += MessageRequest(sessionKey, limit, offset, profile)
                val messages = (offset until offset + limit).map { index ->
                    transcript("stored-1", "message-$index")
                        .messages
                        .single()
                        .copy(messageId = index.toLong())
                }
                return SessionRepositoryResult.Data(
                    SessionTranscript(
                        sessionKey = sessionKey,
                        lineageTip = sessionKey,
                        messages = messages,
                        pagination = TranscriptPagination(
                            limit = limit,
                            offset = offset,
                            returned = messages.size,
                        ),
                    ),
                )
            }
        }
        val viewModel = SessionBrowserViewModel(source)
        viewModel.start()
        advanceUntilIdle()
        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        repeat(24) {
            viewModel.loadOlderMessages()
            advanceUntilIdle()
        }

        assertEquals(
            listOf(980) + (960 downTo 500 step 20),
            requests.map(MessageRequest::offset),
        )
        assertEquals(500, viewModel.state.value.transcript?.pagination?.offset)
        assertEquals(
            (500L until 1_000L).toList(),
            viewModel.state.value.transcript?.messages?.map { it.messageId },
        )
        assertTrue(viewModel.state.value.hasOlderMessages)

        viewModel.loadOlderMessages()
        advanceUntilIdle()

        assertEquals(480, viewModel.state.value.transcript?.pagination?.offset)
        assertEquals(
            (480L until 1_000L).toList(),
            viewModel.state.value.transcript?.messages?.map { it.messageId },
        )
    }

    @Test
    fun `authentication failure is recoverable and does not show empty list`() = runTest {
        val viewModel = SessionBrowserViewModel(
            FakeSource(sessionResult = SessionRepositoryResult.AuthenticationRequired),
        )

        viewModel.start()
        advanceUntilIdle()

        assertEquals(SessionBrowserPhase.AUTHENTICATION_REQUIRED, viewModel.state.value.phase)
        assertTrue(viewModel.state.value.sessions.isEmpty())
        assertTrue(viewModel.state.value.message.orEmpty().isNotBlank())
    }

    @Test
    fun `live projection is only accepted for selected stable session`() = runTest {
        val projection = session("stored-1", "First session")
        val transcript = transcript("stored-1", "hello")
        val source = FakeSource(
            sessionResult = SessionRepositoryResult.Data(SessionPage(listOf(projection), 1, 20, 0)),
            transcriptResults = mutableMapOf(
                SessionKey("stored-1") to SessionRepositoryResult.Data(transcript),
            ),
        )
        val reducer = RealtimeSessionReducer()
        val viewModel = SessionBrowserViewModel(source)
        viewModel.start()
        advanceUntilIdle()
        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        val foreign = reducer.seed(
            transcript("stored-2", "foreign"),
            RuntimeSessionId("runtime-2"),
            connectionEpoch = 1,
        )
        viewModel.onRealtimeProjection(foreign)
        assertNull(viewModel.state.value.realtime)

        val matching = reducer.seed(transcript, RuntimeSessionId("runtime-1"), connectionEpoch = 1)
        viewModel.onRealtimeProjection(matching)
        assertEquals(RuntimeSessionId("runtime-1"), viewModel.state.value.realtime?.runtimeSessionId)
    }

    @Test
    fun `opening REST transcript starts realtime observation and applies current session updates`() = runTest {
        val projection = session("stored-1", "First session")
        val transcript = transcript("stored-1", "hello")
        val realtimeSource = FakeRealtimeSource()
        val viewModel = SessionBrowserViewModel(
            source = FakeSource(
                sessionResult = SessionRepositoryResult.Data(
                    SessionPage(listOf(projection), 1, 20, 0),
                ),
                transcriptResults = mutableMapOf(
                    SessionKey("stored-1") to SessionRepositoryResult.Data(transcript),
                ),
            ),
            realtimeSource = realtimeSource,
        )
        viewModel.start()
        advanceUntilIdle()

        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        assertEquals(SessionKey("stored-1"), realtimeSource.requests.single().first.sessionKey)
        assertEquals(transcript, realtimeSource.requests.single().second)

        realtimeSource.updates.emit(
            SessionRealtimeUpdate.Connection(
                status = RealtimeConnectionStatus.LIVE,
                controlStatus = RealtimeControlStatus.OBSERVER,
            ),
        )
        realtimeSource.updates.emit(
            SessionRealtimeUpdate.Projection(
                RealtimeSessionReducer().seed(
                    transcript = transcript,
                    runtimeSessionId = RuntimeSessionId("runtime-1"),
                    connectionEpoch = 1,
                ),
            ),
        )
        advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals(RealtimeConnectionStatus.LIVE, state.realtimeConnectionStatus)
        assertEquals(RealtimeControlStatus.OBSERVER, state.controlStatus)
        assertEquals(RuntimeSessionId("runtime-1"), state.realtime?.runtimeSessionId)
    }

    @Test
    fun `realtime diagnostic is sanitized and bounded before entering browser state`() = runTest {
        val projection = session("stored-1", "First session")
        val transcript = transcript("stored-1", "hello")
        val realtimeSource = FakeRealtimeSource()
        val viewModel = SessionBrowserViewModel(
            source = FakeSource(
                sessionResult = SessionRepositoryResult.Data(
                    SessionPage(listOf(projection), 1, 20, 0),
                ),
                transcriptResults = mutableMapOf(
                    SessionKey("stored-1") to SessionRepositoryResult.Data(transcript),
                ),
            ),
            realtimeSource = realtimeSource,
        )
        viewModel.start()
        advanceUntilIdle()
        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        realtimeSource.updates.emit(
            SessionRealtimeUpdate.Connection(
                status = RealtimeConnectionStatus.ERROR,
                controlStatus = RealtimeControlStatus.OBSERVER,
                message = "control_lease_id=ui-state-secret " + "x".repeat(20_000),
            ),
        )
        advanceUntilIdle()

        val message = requireNotNull(viewModel.state.value.realtimeMessage)
        assertTrue(message.contains("[redacted]"))
        assertFalse(message.contains("ui-state-secret"))
        assertTrue(message.codePointCount(0, message.length) <= 512)
    }

    @Test
    fun `returning to sessions cancels realtime observation and ignores later updates`() = runTest {
        val projection = session("stored-1", "First session")
        val transcript = transcript("stored-1", "hello")
        val realtimeSource = FakeRealtimeSource()
        val viewModel = SessionBrowserViewModel(
            source = FakeSource(
                sessionResult = SessionRepositoryResult.Data(
                    SessionPage(listOf(projection), 1, 20, 0),
                ),
                transcriptResults = mutableMapOf(
                    SessionKey("stored-1") to SessionRepositoryResult.Data(transcript),
                ),
            ),
            realtimeSource = realtimeSource,
        )
        viewModel.start()
        advanceUntilIdle()
        viewModel.openSession(SessionKey("stored-1"))
        advanceUntilIdle()

        viewModel.backToSessions()
        advanceUntilIdle()

        assertEquals(1, realtimeSource.cancellations)
        assertEquals(RealtimeConnectionStatus.IDLE, viewModel.state.value.realtimeConnectionStatus)
        assertNull(viewModel.state.value.realtime)

        realtimeSource.updates.emit(
            SessionRealtimeUpdate.Projection(
                RealtimeSessionReducer().seed(
                    transcript = transcript,
                    runtimeSessionId = RuntimeSessionId("late-runtime"),
                    connectionEpoch = 1,
                ),
            ),
        )
        advanceUntilIdle()
        assertNull(viewModel.state.value.realtime)
    }

    private fun session(
        id: String,
        title: String,
        profile: String? = null,
        messageCount: Int = 1,
    ) = SessionProjection(
        sessionKey = SessionKey(id),
        lineageRoot = SessionKey(id),
        lineageTip = SessionKey(id),
        parentSessionKey = null,
        title = title,
        preview = "Preview",
        source = "desktop",
        model = "test-model",
        profile = profile,
        cwd = null,
        gitBranch = null,
        startedAtEpochSeconds = 100.0,
        endedAtEpochSeconds = null,
        lastActiveEpochSeconds = 120.0,
        messageCount = messageCount,
        toolCallCount = 0,
        inputTokens = 0,
        outputTokens = 0,
        isActive = true,
        archived = false,
    )

    private fun transcript(id: String, text: String) = SessionTranscript(
        sessionKey = SessionKey(id),
        lineageTip = SessionKey(id),
        messages = listOf(
            SessionMessageProjection(
                messageId = 1,
                role = "user",
                content = JsonPrimitive(text),
                timestampEpochSeconds = 100.0,
                reasoning = null,
                reasoningContent = null,
                reasoningDetails = null,
                toolCallId = null,
                toolCalls = null,
                toolName = null,
                displayKind = null,
                displayMetadata = null,
            ),
        ),
        pagination = TranscriptPagination(limit = 200, offset = 0, returned = 1),
    )

    private class FakeSource(
        private val sessionResult: SessionRepositoryResult<SessionPage>,
        private val transcriptResults: MutableMap<SessionKey, SessionRepositoryResult<SessionTranscript>> = mutableMapOf(),
    ) : SessionBrowserSource {
        val messageRequests = mutableListOf<MessageRequest>()

        override suspend fun loadSessions(
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionRepositoryResult<SessionPage> = sessionResult

        override suspend fun loadMessages(
            sessionKey: SessionKey,
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionRepositoryResult<SessionTranscript> {
            messageRequests += MessageRequest(sessionKey, limit, offset, profile)
            return transcriptResults.getValue(sessionKey)
        }
    }

    private data class MessageRequest(
        val sessionKey: SessionKey,
        val limit: Int,
        val offset: Int,
        val profile: String?,
    )

    private class FakeRealtimeSource : SessionRealtimeSource {
        val requests = mutableListOf<Pair<SessionProjection, SessionTranscript>>()
        val updates = MutableSharedFlow<SessionRealtimeUpdate>(extraBufferCapacity = 8)
        var cancellations = 0

        override fun observe(
            session: SessionProjection,
            baseline: SessionTranscript,
        ): Flow<SessionRealtimeUpdate> {
            requests += session to baseline
            return updates.onCompletion { cancellations += 1 }
        }
    }
}
