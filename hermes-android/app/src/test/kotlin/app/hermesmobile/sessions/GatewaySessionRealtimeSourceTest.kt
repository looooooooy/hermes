package app.hermesmobile.sessions

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.WebSocketTicket
import app.hermesmobile.protocol.gateway.GatewayWebSocketClient
import app.hermesmobile.protocol.gateway.SessionObserverSnapshotMessage
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.filterIsInstance
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class GatewaySessionRealtimeSourceTest {
    private val clientInstanceId =
        ClientInstanceId("11111111-1111-4111-8111-111111111111")
    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `active session subscribes by stable key and profile then streams sequenced delta`() = runBlocking {
        val ticketClientInstanceIds = mutableListOf<ClientInstanceId>()
        val subscribeRequest = CompletableDeferred<JsonObject>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> {
                                subscribeRequest.complete(request)
                                webSocket.send(
                                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                                )
                                webSocket.send(
                                    """{"jsonrpc":"2.0","method":"event","params":{"type":"message.start","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{}}}""",
                                )
                                webSocket.send(
                                    """{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"runtime-1","session_key":"stored-1","event_sequence":2,"payload":{"text":"hello"}}}""",
                                )
                            }
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val httpClient = OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()
        val endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow()
        val source = GatewaySessionRealtimeSource(
            endpoint = endpoint,
            clientInstanceId = clientInstanceId,
            ticketProvider = WebSocketTicketSource { requestedClientInstanceId ->
                ticketClientInstanceIds += requestedClientInstanceId
                WebSocketTicketResult.Ready(WebSocketTicket("one-time-ticket", 30))
            },
            socketClient = GatewayWebSocketClient(httpClient),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.streamingAssistantText == "hello" }
                .projection
        }

        val request = withTimeout(3_000) { subscribeRequest.await() }
        assertEquals("session.observe.subscribe", request.getValue("method").jsonPrimitive.content)
        assertEquals("stored-1", request.getValue("params").jsonObject.getValue("session_key").jsonPrimitive.content)
        assertEquals("work", request.getValue("params").jsonObject.getValue("profile").jsonPrimitive.content)
        assertEquals(listOf(clientInstanceId), ticketClientInstanceIds)
        assertEquals("hello", projection.streamingAssistantText)
        assertEquals(2L, projection.lastEventOrdinal)
        assertEquals("hello", projection.timeline.filterIsInstance<SessionTimelineItem.AssistantTurn>().single().text)
    }

    @Test
    fun `v2 snapshot replay and live lifecycle updates install one local projection`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v2")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":2,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> {
                                assertEquals(2, request.getValue("params").jsonObject.getValue("observer_contract").jsonPrimitive.content.toInt())
                                webSocket.send(
                                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"sub-v2","profile":"work","runtime_generation":"generation-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":5,"snapshot_event_sequence":4,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"in_progress","items":[{"id":"item-1","label":"Run tests","status":"in_progress"}]}],"subagents":[{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":2,"parent_subagent_id":null,"name":"Runner","goal":"Run tests","summary":null,"status":"running"}],"tools":[{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":3,"status":"running","name":"Tests"}],"terminals":[{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":4,"status":"running"}],"replay_events":[{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":5,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Run tests","status":"completed"}]}}]}}""",
                                )
                                webSocket.send(
                                    """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"subagent.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":6,"payload":{"turn_id":"turn-1","subagent_id":"agent-1","revision":2,"first_event_sequence":2,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"Run tests","summary":"Passed","status":"completed"}}}""",
                                )
                            }
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val source = GatewaySessionRealtimeSource(
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            clientInstanceId = clientInstanceId,
            ticketProvider = WebSocketTicketSource {
                WebSocketTicketResult.Ready(WebSocketTicket("v2-ticket", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
        )

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.lastEventOrdinal == 6L }
                .projection
        }

        assertEquals(2, projection.observerContractVersion)
        assertEquals("work", projection.observerProfile)
        assertEquals("generation-1", projection.runtimeGeneration)
        assertEquals(2L, projection.todoSections.single().revision)
        assertEquals(HermesConversationTodoStatus.COMPLETED, projection.todoSections.single().items.single().status)
        assertEquals(2L, projection.subagents.single().revision)
        assertEquals(LiveSubagentStatus.COMPLETE, projection.subagents.single().status)
        assertEquals(1, projection.tools.size)
        assertEquals(1, projection.terminals.size)
    }

    @Test
    fun `v2 runtime generation rollover discards projection and resubscribes a fresh snapshot`() = runBlocking {
        fun socket(generation: String, emitRollover: Boolean): MockResponse =
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v2")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":2,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> {
                                webSocket.send(
                                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"sub-$generation","profile":"work","runtime_generation":"$generation","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":0,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}}""",
                                )
                                if (emitRollover) {
                                    webSocket.send(
                                        """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"work","runtime_generation":"generation-2","type":"message.start","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{}}}""",
                                    )
                                }
                            }
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            )
        server.enqueue(socket("generation-1", emitRollover = true))
        server.enqueue(socket("generation-2", emitRollover = false))
        var tickets = 0
        val transcriptSource = FakeTranscriptSource(baseline())
        val source = GatewaySessionRealtimeSource(
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            clientInstanceId = clientInstanceId,
            ticketProvider = WebSocketTicketSource {
                tickets += 1
                WebSocketTicketResult.Ready(WebSocketTicket("v2-ticket-$tickets", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = transcriptSource,
            reconnectDelayMillis = 0,
        )

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.runtimeGeneration == "generation-2" }
                .projection
        }

        assertEquals("generation-2", projection.runtimeGeneration)
        assertEquals(2L, projection.connectionEpoch)
        assertEquals(0L, projection.lastEventOrdinal)
        assertEquals(2, tickets)
        assertTrue(transcriptSource.loadCount >= 1)
    }

    @Test
    fun `v2 duplicate transport identity with a different digest forces authoritative resnapshot`() = runBlocking {
        val emptySnapshot = """{"observer_contract":2,"subscription_id":"sub-1","profile":"work","runtime_generation":"generation-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":0,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}"""
        val first = """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"First","status":"in_progress"}]}}}"""
        val conflicting = first.replace("\"First\"", "\"Conflicting\"")
        val authoritative = """{"observer_contract":2,"subscription_id":"sub-2","profile":"work","runtime_generation":"generation-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":1,"snapshot_event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"completed","items":[{"id":"item-1","label":"Authoritative","status":"completed"}]}],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}"""
        server.enqueue(v2ObserverSocket(emptySnapshot, listOf(first, conflicting)))
        server.enqueue(v2ObserverSocket(authoritative))
        var tickets = 0
        val source = v2Source { tickets += 1 }

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.todoSections.singleOrNull()?.items?.singleOrNull()?.label == "Authoritative" }
                .projection
        }

        assertEquals(2, tickets)
        assertEquals("Authoritative", projection.todoSections.single().items.single().label)
    }

    @Test
    fun `v2 stale transport identity absent from bounded digest history forces resnapshot`() = runBlocking {
        val snapshot = """{"observer_contract":2,"subscription_id":"sub-1","profile":"work","runtime_generation":"generation-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":1,"snapshot_event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"in_progress","items":[{"id":"item-1","label":"Snapshot","status":"in_progress"}]}],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}"""
        val unknownStale = """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"Snapshot","status":"in_progress"}]}}}"""
        val authoritative = snapshot
            .replace("sub-1", "sub-2")
            .replace("\"Snapshot\"", "\"Authoritative\"")
        server.enqueue(v2ObserverSocket(snapshot, listOf(unknownStale)))
        server.enqueue(v2ObserverSocket(authoritative))
        var tickets = 0
        val source = v2Source { tickets += 1 }

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.todoSections.singleOrNull()?.items?.singleOrNull()?.label == "Authoritative" }
                .projection
        }

        assertEquals(2, tickets)
        assertEquals("Authoritative", projection.todoSections.single().items.single().label)
    }

    @Test
    fun `v2 exact duplicate in bounded digest history is idempotent and live sequence continues`() = runBlocking {
        val snapshot = """{"observer_contract":2,"subscription_id":"sub-1","profile":"work","runtime_generation":"generation-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":0,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}"""
        val first = """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"First","status":"in_progress"}]}}}"""
        val second = """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":2,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"First","status":"completed"}]}}}"""
        server.enqueue(v2ObserverSocket(snapshot, listOf(first, first, second)))
        var tickets = 0
        val source = v2Source { tickets += 1 }

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.lastEventOrdinal == 2L }
                .projection
        }

        assertEquals(1, tickets)
        assertEquals(2L, projection.todoSections.single().revision)
    }

    @Test
    fun `v2 snapshot projects delimiter-colliding lifecycle identities without key collisions`() = runBlocking {
        val snapshot = """{"observer_contract":2,"subscription_id":"sub-collision","profile":"work","runtime_generation":"generation-1","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":10,"snapshot_event_sequence":10,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"a","section_id":"b:todo:c","revision":1,"first_event_sequence":1,"status":"completed","items":[{"id":"one","label":"First","status":"completed"}]},{"turn_id":"a:todo:b","section_id":"c","revision":1,"first_event_sequence":2,"status":"completed","items":[{"id":"two","label":"Second","status":"completed"}]}],"subagents":[{"turn_id":"a","subagent_id":"b:subagent:c","revision":1,"first_event_sequence":3,"parent_subagent_id":null,"name":"Parent A","goal":"","summary":null,"status":"completed"},{"turn_id":"a:subagent:b","subagent_id":"c","revision":1,"first_event_sequence":4,"parent_subagent_id":null,"name":"Parent B","goal":"","summary":null,"status":"completed"},{"turn_id":"a","subagent_id":"child","revision":1,"first_event_sequence":5,"parent_subagent_id":"b:subagent:c","name":"Child A","goal":"","summary":null,"status":"completed"},{"turn_id":"a:subagent:b","subagent_id":"child","revision":1,"first_event_sequence":6,"parent_subagent_id":"c","name":"Child B","goal":"","summary":null,"status":"completed"}],"tools":[{"turn_id":"a","tool_call_id":"b:tool:c","revision":1,"first_event_sequence":7,"status":"completed","name":"Tool A"},{"turn_id":"a:tool:b","tool_call_id":"c","revision":1,"first_event_sequence":8,"status":"completed","name":"Tool B"}],"terminals":[{"turn_id":"a","process_id":"b:terminal:c","revision":1,"first_event_sequence":9,"status":"completed","exit_code":0},{"turn_id":"a:terminal:b","process_id":"c","revision":1,"first_event_sequence":10,"status":"completed","exit_code":0}],"replay_events":[]}"""
        server.enqueue(v2ObserverSocket(snapshot))
        val source = v2Source { }

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.lastEventOrdinal == 10L }
                .projection
        }

        assertEquals(2, projection.todoSections.size)
        assertEquals(2, projection.todoSections.map { it.key }.toSet().size)
        assertEquals(4, projection.subagents.size)
        assertEquals(4, projection.subagents.map { it.key }.toSet().size)
        assertEquals(2, projection.subagents.mapNotNull { it.parentKey }.toSet().size)
        assertEquals(2, projection.tools.map { it.key }.toSet().size)
        assertEquals(2, projection.terminals.map { it.key }.toSet().size)
    }

    @Test
    fun `five hundred message snapshot merges with authoritative REST tail by sequence`() {
        val transcript = mergeObserverSnapshotMessages(
            baseline = longRestTail(),
            snapshot = (0 until 500).map { index ->
                SessionObserverSnapshotMessage(
                    role = if (index % 2 == 0) "user" else "assistant",
                    content = "message-$index",
                )
            },
        )

        assertEquals(510, transcript.messages.size)
        assertEquals(
            (0 until 510).map { "message-$it" },
            transcript.messages.map { (it.content as JsonPrimitive).content },
        )
        assertEquals(null, transcript.messages[489].messageId)
        assertEquals(490L, transcript.messages[490].messageId)
        assertEquals("reasoning-490", transcript.messages[490].reasoning)
        assertEquals("tool-490", transcript.messages[490].toolName)
        assertEquals("kind-490", transcript.messages[490].displayKind)
        assertEquals(
            JsonPrimitive("metadata-490"),
            transcript.messages[490].displayMetadata,
        )
        assertEquals(509L, transcript.messages.last().messageId)
        assertEquals(0, transcript.pagination.offset)
        assertEquals(510, transcript.pagination.returned)
    }

    @Test
    fun `snapshot separated from REST tail preserves the authoritative pagination gap`() {
        val baseline = longRestTail(start = 980, endExclusive = 1_000)

        val transcript = mergeObserverSnapshotMessages(
            baseline = baseline,
            snapshot = (0 until 500).map { index ->
                SessionObserverSnapshotMessage(
                    role = if (index % 2 == 0) "user" else "assistant",
                    content = "message-$index",
                )
            },
        )

        assertEquals(
            (980 until 1_000).map { "message-$it" },
            transcript.messages.map { (it.content as JsonPrimitive).content },
        )
        assertEquals(980, transcript.pagination.offset)
        assertEquals(20, transcript.pagination.returned)
    }

    @Test
    fun `subscription replay is reduced after its snapshot watermark before live events`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-replay","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":3,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[{"type":"message.start","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{}},{"type":"message.delta","session_id":"runtime-1","session_key":"stored-1","event_sequence":2,"payload":{"text":"partial"}},{"type":"message.complete","session_id":"runtime-1","session_key":"stored-1","event_sequence":3,"payload":{"status":"complete"}}]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                WebSocketTicketResult.Ready(WebSocketTicket("one-time-ticket", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.lastEventOrdinal == 3L }
                .projection
        }

        assertEquals("", projection.streamingAssistantText)
        assertEquals("partial", projection.timeline.filterIsInstance<SessionTimelineItem.AssistantTurn>().single().text)
        assertEquals(
            AssistantTurnStatus.COMPLETE,
            projection.timeline.filterIsInstance<SessionTimelineItem.AssistantTurn>().single().status,
        )
    }

    @Test
    fun `failed inflight snapshot preserves partial assistant text and error`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-failed","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"error","snapshot_event_sequence":5,"event_sequence":5,"messages":[],"inflight":{"user":null,"assistant":"partial answer","streaming":false,"error":"model failed"},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                WebSocketTicketResult.Ready(WebSocketTicket("one-time-ticket", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        val projection = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Projection>()
                .first { it.projection.lastError == "model failed" }
                .projection
        }

        val failed = projection.timeline.filterIsInstance<SessionTimelineItem.AssistantTurn>().single()
        assertEquals("partial answer", failed.text)
        assertEquals(AssistantTurnStatus.ERROR, failed.status)
        assertEquals("model failed", failed.error)
    }

    @Test
    fun `socket closed before ready reports reconnecting then resubscribes`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.close(1011, "not ready")
                    }
                },
            ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-2","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        var ticketCount = 0
        val statuses = mutableListOf<RealtimeConnectionStatus>()
        val endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow()
        val source = GatewaySessionRealtimeSource(
            endpoint = endpoint,
            clientInstanceId = clientInstanceId,
            ticketProvider = WebSocketTicketSource { _ ->
                ticketCount += 1
                WebSocketTicketResult.Ready(WebSocketTicket("ticket-$ticketCount", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 1,
            observerContractVersion = 1,
        )

        withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .onEach { statuses += it.status }
                .first { it.status == RealtimeConnectionStatus.LIVE }
        }

        assertEquals(2, ticketCount)
        assertEquals(
            listOf(
                RealtimeConnectionStatus.CONNECTING,
                RealtimeConnectionStatus.DISCONNECTED,
                RealtimeConnectionStatus.RECONNECTING,
                RealtimeConnectionStatus.LIVE,
            ),
            statuses,
        )
    }

    @Test
    fun `missing gateway ready times out then reconnects`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-after-ready-timeout","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        var ticketCount = 0
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                ticketCount += 1
                WebSocketTicketResult.Ready(WebSocketTicket("ticket-$ticketCount", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            readyTimeoutMillis = 50,
            observerContractVersion = 1,
        )

        withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .first { it.status == RealtimeConnectionStatus.LIVE }
        }

        assertEquals(2, ticketCount)
    }

    @Test
    fun `reconnect before first snapshot refreshes rest baseline`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.close(1011, "not ready")
                    }
                },
            ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-after-initial-failure","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val transcriptSource = FakeTranscriptSource(baseline())
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                WebSocketTicketResult.Ready(WebSocketTicket("ticket", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = transcriptSource,
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .first { it.status == RealtimeConnectionStatus.LIVE }
        }

        assertEquals(1, transcriptSource.loadCount)
    }

    @Test
    fun `replay overflow rejection reconnects and retries observer subscription`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val id = request.getValue("id").jsonPrimitive.content
                        webSocket.send(
                            """{"jsonrpc":"2.0","id":$id,"error":{"code":4091,"message":"live observer replay unavailable until the next turn"}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-after-overflow","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        var ticketCount = 0
        val statuses = mutableListOf<RealtimeConnectionStatus>()
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                ticketCount += 1
                WebSocketTicketResult.Ready(WebSocketTicket("ticket-$ticketCount", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .onEach { statuses += it.status }
                .first { it.status == RealtimeConnectionStatus.LIVE }
        }

        assertEquals(2, ticketCount)
        assertEquals(
            listOf(
                RealtimeConnectionStatus.CONNECTING,
                RealtimeConnectionStatus.DISCONNECTED,
                RealtimeConnectionStatus.RECONNECTING,
                RealtimeConnectionStatus.LIVE,
            ),
            statuses,
        )
    }

    @Test
    fun `live sequence gap reconnects before applying the later event`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> {
                                webSocket.send(
                                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-before-gap","session_key":"stored-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":true,"error":null},"replay_events":[]}}""",
                                )
                                webSocket.send(
                                    """{"jsonrpc":"2.0","method":"event","params":{"type":"message.start","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{}}}""",
                                )
                                webSocket.send(
                                    """{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"runtime-1","session_key":"stored-1","event_sequence":3,"payload":{"text":"must not be applied"}}}""",
                                )
                            }
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-after-gap","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        var ticketCount = 0
        val statuses = mutableListOf<RealtimeConnectionStatus>()
        val assistantTexts = mutableListOf<String>()
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                ticketCount += 1
                WebSocketTicketResult.Ready(WebSocketTicket("ticket-$ticketCount", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .onEach { update ->
                    when (update) {
                        is SessionRealtimeUpdate.Connection -> statuses += update.status
                        is SessionRealtimeUpdate.Projection -> {
                            assistantTexts += update.projection.streamingAssistantText
                        }
                    }
                }
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .first { statuses.count { it == RealtimeConnectionStatus.LIVE } == 2 }
        }

        assertEquals(2, ticketCount)
        assertEquals(
            listOf(
                RealtimeConnectionStatus.CONNECTING,
                RealtimeConnectionStatus.LIVE,
                RealtimeConnectionStatus.DISCONNECTED,
                RealtimeConnectionStatus.RECONNECTING,
                RealtimeConnectionStatus.LIVE,
            ),
            statuses,
        )
        assertFalse("must not be applied" in assistantTexts)
    }

    @Test
    fun `live observer event without sequence reconnects instead of silently dropping it`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> {
                                webSocket.send(
                                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-before-missing-sequence","session_key":"stored-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":true,"error":null},"replay_events":[]}}""",
                                )
                                webSocket.send(
                                    """{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"runtime-1","session_key":"stored-1","payload":{"text":"must not be silently dropped"}}}""",
                                )
                            }
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        when (method) {
                            "session.observe.subscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"sub-after-missing-sequence","session_key":"stored-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                            )
                            "session.observe.unsubscribe" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        var ticketCount = 0
        val statuses = mutableListOf<RealtimeConnectionStatus>()
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                ticketCount += 1
                WebSocketTicketResult.Ready(WebSocketTicket("ticket-$ticketCount", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .onEach { statuses += it.status }
                .first { statuses.count { it == RealtimeConnectionStatus.LIVE } == 2 }
        }

        assertEquals(2, ticketCount)
        assertEquals(
            listOf(
                RealtimeConnectionStatus.CONNECTING,
                RealtimeConnectionStatus.LIVE,
                RealtimeConnectionStatus.DISCONNECTED,
                RealtimeConnectionStatus.RECONNECTING,
                RealtimeConnectionStatus.LIVE,
            ),
            statuses,
        )
    }

    @Test
    fun `stale inactive REST projection still asks authoritative observer whether session is live`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val method = request.getValue("method").jsonPrimitive.content
                        val id = request.getValue("id").jsonPrimitive.content
                        if (method == "session.observe.subscribe") {
                            webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"error":{"code":4001,"message":"live session not found"}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        var minted = false
        val endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow()
        val source = GatewaySessionRealtimeSource(
            endpoint = endpoint,
            clientInstanceId = clientInstanceId,
            ticketProvider = WebSocketTicketSource { _ ->
                minted = true
                WebSocketTicketResult.Ready(WebSocketTicket("unused", 30))
            },
            socketClient = GatewayWebSocketClient(OkHttpClient()),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        val update = withTimeout(5_000) {
            source.observe(activeSession().copy(isActive = false), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .first { it.status == RealtimeConnectionStatus.DISCONNECTED }
        }

        assertEquals(RealtimeConnectionStatus.DISCONNECTED, update.status)
        assertEquals(RealtimeControlStatus.OBSERVER, update.controlStatus)
        assertEquals("This session is not currently running.", update.message)
        assertTrue(minted)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `observer RPC code outside frozen Cloud catalog becomes protocol error`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                        )
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val request = Json.parseToJsonElement(text).jsonObject
                        val id = request.getValue("id")
                        webSocket.send(
                            buildJsonObject {
                                put("jsonrpc", "2.0")
                                put("id", id)
                                put(
                                    "error",
                                    buildJsonObject {
                                        put("code", 4500)
                                        put(
                                            "message",
                                            "ws_ticket=rpc-error-secret " + "x".repeat(20_000),
                                        )
                                    },
                                )
                            }.toString(),
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val source = GatewaySessionRealtimeSource(
            clientInstanceId = clientInstanceId,
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            ticketProvider = WebSocketTicketSource { _ ->
                WebSocketTicketResult.Ready(WebSocketTicket("one-time-ticket", 30))
            },
            socketClient = GatewayWebSocketClient(OkHttpClient()),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
            observerContractVersion = 1,
        )

        val update = withTimeout(5_000) {
            source.observe(activeSession(), baseline())
                .filterIsInstance<SessionRealtimeUpdate.Connection>()
                .first { it.status == RealtimeConnectionStatus.ERROR }
        }
        assertEquals(
            "Hermes returned an invalid realtime observer response.",
            update.message,
        )
    }

    private fun v2Source(onMint: () -> Unit): GatewaySessionRealtimeSource =
        GatewaySessionRealtimeSource(
            endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow(),
            clientInstanceId = clientInstanceId,
            ticketProvider = WebSocketTicketSource {
                onMint()
                WebSocketTicketResult.Ready(WebSocketTicket("v2-ticket", 30))
            },
            socketClient = GatewayWebSocketClient(
                OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build(),
            ),
            transcriptSource = FakeTranscriptSource(baseline()),
            reconnectDelayMillis = 0,
        )

    private fun v2ObserverSocket(
        result: String,
        liveFrames: List<String> = emptyList(),
    ): MockResponse = MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v2")
                .withWebSocketUpgrade(
        object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(
                    """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":2,"connection_role":"observer"}}}""",
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val request = Json.parseToJsonElement(text).jsonObject
                val method = request.getValue("method").jsonPrimitive.content
                val id = request.getValue("id").jsonPrimitive.content
                when (method) {
                    "session.observe.subscribe" -> {
                        webSocket.send("""{"jsonrpc":"2.0","id":$id,"result":$result}""")
                        liveFrames.forEach(webSocket::send)
                    }
                    "session.observe.unsubscribe" -> webSocket.send(
                        """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2}}""",
                    )
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        },
    )

    private fun activeSession() = SessionProjection(
        sessionKey = SessionKey("stored-1"),
        lineageRoot = SessionKey("stored-1"),
        lineageTip = SessionKey("stored-1"),
        parentSessionKey = null,
        title = "Live",
        preview = null,
        source = "desktop",
        model = "test",
        profile = "work",
        cwd = null,
        gitBranch = null,
        startedAtEpochSeconds = 1.0,
        endedAtEpochSeconds = null,
        lastActiveEpochSeconds = 2.0,
        messageCount = 0,
        toolCallCount = 0,
        inputTokens = 0,
        outputTokens = 0,
        isActive = true,
        archived = false,
    )

    private fun baseline() = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = SessionKey("stored-1"),
        messages = emptyList(),
        pagination = TranscriptPagination(limit = 200, offset = 0, returned = 0),
    )

    private fun longRestTail(
        start: Int = 490,
        endExclusive: Int = 510,
    ) = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = SessionKey("stored-1"),
        messages = (start until endExclusive).map { index ->
            SessionMessageProjection(
                messageId = index.toLong(),
                role = if (index % 2 == 0) "user" else "assistant",
                content = JsonPrimitive("message-$index"),
                timestampEpochSeconds = index.toDouble(),
                reasoning = "reasoning-$index",
                reasoningContent = "reasoning-content-$index",
                reasoningDetails = JsonPrimitive("reasoning-details-$index"),
                toolCallId = "call-$index",
                toolCalls = JsonPrimitive("calls-$index"),
                toolName = "tool-$index",
                displayKind = "kind-$index",
                displayMetadata = JsonPrimitive("metadata-$index"),
            )
        },
        pagination = TranscriptPagination(
            limit = endExclusive - start,
            offset = start,
            returned = endExclusive - start,
        ),
    )

    private class FakeTranscriptSource(
        private val transcript: SessionTranscript,
    ) : SessionTranscriptSource {
        var loadCount: Int = 0
            private set

        override suspend fun loadMessages(
            sessionKey: SessionKey,
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionRepositoryResult<SessionTranscript> {
            loadCount += 1
            return SessionRepositoryResult.Data(transcript)
        }
    }
}
