package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.WebSocketTicket
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
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
import kotlin.test.assertIs
import kotlin.test.assertNull

class SessionObserverClientTest {
    private lateinit var server: MockWebServer
    private lateinit var socketClient: GatewayWebSocketClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        socketClient = GatewayWebSocketClient(
            httpClient = OkHttpClient.Builder()
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .build(),
        )
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `subscribe sends observer RPC and parses a typed live snapshot`() = runBlocking {
        val requestFrame = CompletableDeferred<JsonObject>()
        server.enqueue(
            observerSocket { webSocket, request ->
                requestFrame.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":0,"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"running tests"}],"inflight":{"user":"continue","assistant":"partial","streaming":true,"error":null},"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(
            SessionKey("durable-1"),
            profile = "work",
            observerContractVersion = 1,
        )

        val request = withTimeout(3_000) { requestFrame.await() }
        assertEquals("session.observe.subscribe", request.getValue("method").jsonPrimitive.content)
        assertEquals(
            setOf("session_key", "profile"),
            request.getValue("params").jsonObject.keys,
        )
        assertEquals("durable-1", request.getValue("params").jsonObject.getValue("session_key").jsonPrimitive.content)
        assertEquals("work", request.getValue("params").jsonObject.getValue("profile").jsonPrimitive.content)

        val subscription = assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result).value
        assertEquals(SessionObserverSubscriptionId("subscription-1"), subscription.subscriptionId)
        assertEquals(SessionKey("durable-1"), subscription.sessionKey)
        assertEquals(RuntimeSessionId("runtime-1"), subscription.runtimeSessionId)
        assertEquals(true, subscription.running)
        assertEquals(SessionObserverStatus("working"), subscription.status)
        assertEquals(0L, subscription.eventSequence)
        assertEquals(
            SessionObserverSnapshotMessage(role = "user", content = "hello"),
            subscription.messages.first(),
        )
        assertEquals("running tests", subscription.messages.last().content)
        assertEquals("continue", subscription.inflight?.user)
        assertEquals("partial", subscription.inflight?.assistant)
        assertEquals(true, subscription.inflight?.streaming)
        assertNull(subscription.inflight?.error)
        connection.close()
    }

    @Test
    fun `v2 subscribe sends exact selection and accepts an authoritative snapshot baseline`() = runBlocking {
        val requestFrame = CompletableDeferred<JsonObject>()
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                requestFrame.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"runtime-20260801-01","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":4,"snapshot_event_sequence":4,"messages":[{"role":"assistant","content":"Display-safe snapshot"}],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"in_progress","items":[{"id":"todo-item-1","label":"Run focused tests","status":"in_progress"}]}],"subagents":[{"turn_id":"turn-1","subagent_id":"subagent-1","revision":1,"first_event_sequence":2,"parent_subagent_id":null,"name":"Test runner","goal":"Run checks","summary":null,"status":"running"}],"tools":[{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":3,"status":"running","name":"Contract tests"}],"terminals":[{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":4,"status":"running"}],"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            profile = "default",
        )

        val request = withTimeout(3_000) { requestFrame.await() }
        assertEquals(
            setOf("observer_contract", "session_key", "profile"),
            request.getValue("params").jsonObject.keys,
        )
        assertEquals(2, request.getValue("params").jsonObject.getValue("observer_contract").jsonPrimitive.content.toInt())
        assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result)
        connection.close()
    }

    @Test
    fun `v2 subscribe rejects credentials across every snapshot projection surface`() = runBlocking {
        val unsafeSnapshots = listOf(
            "messages.content" to
                """{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":0,"snapshot_event_sequence":0,"messages":[{"role":"assistant","content":"Authorization: Basic dXNlcjpwYXNz"}],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}""",
            "inflight.assistant" to
                """{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":0,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":"password = hunter2","streaming":true,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}""",
            "lifecycle.summary" to
                """{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":1,"snapshot_event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"parent_subagent_id":null,"name":"Runner","goal":"Run checks","summary":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln","status":"running"}],"tools":[],"terminals":[],"replay_events":[]}""",
            "replay.payload.text" to
                """{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":1,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":true,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"text":"AKIAIOSFODNN7EXAMPLE"}}]}""",
        )

        unsafeSnapshots.forEach { (surface, snapshot) ->
            server.enqueue(
                observerSocket(observerContract = 2) { webSocket, request ->
                    val id = request.getValue("id").jsonPrimitive.content
                    webSocket.send("""{"jsonrpc":"2.0","id":$id,"result":$snapshot}""")
                },
            )
            val connection = connectReady(observerContract = 2)

            val result = SessionObserverClient(connection).subscribe(
                SessionKey("durable-1"),
                "default",
            )

            assertIs<SessionObserverResult.InvalidResponse>(result, surface)
            connection.close()
        }
    }

    @Test
    fun `v2 subscribe retains ordinary vocabulary and nonnegative aggregate token counts`() = runBlocking {
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":2,"snapshot_event_sequence":1,"messages":[{"role":"assistant","content":"Basic authentication is disabled. Basic YWJjZA== is not a user-password credential."}],"inflight":{"user":"Discuss tokenizer behavior","assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"parent_subagent_id":null,"name":"Runner","goal":"Review pathology on docs.example.com","summary":"Token counts are aggregate only for release 1.2.3","status":"running","token_counts":{"input":0,"output":12,"reasoning":3}}],"tools":[],"terminals":[],"replay_events":[{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":2,"payload":{"text":"a.b.c remains ordinary display text"}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            "default",
        )

        assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result)
        connection.close()
    }

    @Test
    fun `v2 snapshot text limits count Unicode code points consistently`() = runBlocking {
        val displaySafeError = "😀".repeat(3_000)
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":0,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":"$displaySafeError"},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            "default",
        )

        assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result)
        connection.close()
    }

    @Test
    fun `v2 snapshot rejects duplicate entity identities atomically`() = runBlocking {
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                val todo = """{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"completed","items":[{"id":"item-1","label":"Done","status":"completed"}]}"""
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":1,"snapshot_event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[$todo,$todo],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `v2 snapshot rejects orphan and cyclic subagent graphs atomically`() = runBlocking {
        val invalidGraphs = listOf(
            """[{"turn_id":"turn-1","subagent_id":"child","revision":1,"first_event_sequence":1,"parent_subagent_id":"missing","name":"Child","goal":"","summary":null,"status":"running"}]""",
            """[{"turn_id":"turn-1","subagent_id":"a","revision":1,"first_event_sequence":1,"parent_subagent_id":"b","name":"A","goal":"","summary":null,"status":"running"},{"turn_id":"turn-1","subagent_id":"b","revision":1,"first_event_sequence":1,"parent_subagent_id":"a","name":"B","goal":"","summary":null,"status":"running"}]""",
        )
        invalidGraphs.forEach { graph ->
            server.enqueue(
                observerSocket(observerContract = 2) { webSocket, request ->
                    val id = request.getValue("id").jsonPrimitive.content
                    webSocket.send(
                        """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":1,"snapshot_event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":$graph,"tools":[],"terminals":[],"replay_events":[]}}""",
                    )
                },
            )
            val connection = connectReady(observerContract = 2)

            val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

            assertIs<SessionObserverResult.InvalidResponse>(result)
            connection.close()
        }
    }

    @Test
    fun `v2 replay reuses the full event decoder and rejects unsafe tool fields`() = runBlocking {
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":1,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"tool.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"running","name":"Tests","raw_args":{"command":"must-not-cross"}}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `v2 replay accepts contiguous todo subagent tool and terminal updates`() = runBlocking {
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":4,"snapshot_event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"Run tests","status":"in_progress"}]}},{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"subagent.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":2,"payload":{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":2,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"Run tests","summary":null,"status":"running"}},{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"tool.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":3,"payload":{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":3,"operation":"upsert","status":"running","name":"Tests"}},{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"terminal.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":4,"payload":{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":4,"operation":"upsert","status":"running"}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

        val subscription = assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result).value
        assertEquals(
            listOf("todo.update", "subagent.update", "tool.update", "terminal.update"),
            subscription.replayEvents.map(GatewayEvent::type),
        )
        connection.close()
    }

    @Test
    fun `v2 replay rejects an entity revision conflict against the snapshot`() = runBlocking {
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"running","event_sequence":2,"snapshot_event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"in_progress","items":[{"id":"item-1","label":"Run tests","status":"in_progress"}]}],"subagents":[],"tools":[],"terminals":[],"replay_events":[{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":2,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":3,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Run tests","status":"completed"}]}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `default v2 subscribe does not downgrade on a v1 ready connection`() = runBlocking {
        var requestReceived = false
        server.enqueue(
            observerSocket(observerContract = 1) { _, _ -> requestReceived = true },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

        assertIs<SessionObserverResult.Unsupported>(result)
        assertEquals(false, requestReceived)
        connection.close()
    }

    @Test
    fun `v2 replay rejects todo rewrites and absorbing entity metadata mutation`() = runBlocking {
        val invalidReplayPayloads = listOf(
            "todo.update" to """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Renamed","status":"completed"}]}""",
            "subagent.update" to """{"turn_id":"turn-1","subagent_id":"agent-1","revision":2,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Renamed","goal":"Different","summary":"Changed","status":"completed","model":"model-b","duration_ms":20,"api_calls":2}""",
            "tool.update" to """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":2,"first_event_sequence":2,"operation":"upsert","status":"completed","name":"Renamed","summary":"Changed","duration_ms":20}""",
            "terminal.update" to """{"turn_id":"turn-1","process_id":"process-1","revision":2,"first_event_sequence":3,"operation":"upsert","status":"failed","exit_code":2,"summary":"Changed","duration_ms":20}""",
        )
        invalidReplayPayloads.forEach { (type, payload) ->
            server.enqueue(
                observerSocket(observerContract = 2) { webSocket, request ->
                    val id = request.getValue("id").jsonPrimitive.content
                    webSocket.send(
                        """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":4,"snapshot_event_sequence":3,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"status":"completed","items":[{"id":"item-1","label":"First","status":"completed"},{"id":"item-2","label":"Second","status":"cancelled"}]}],"subagents":[{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"parent_subagent_id":null,"name":"Runner","goal":"Run tests","summary":"Done","status":"completed","model":"model-a","duration_ms":10,"api_calls":1}],"tools":[{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":2,"status":"completed","name":"Tests","summary":"Passed","duration_ms":10}],"terminals":[{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":3,"status":"failed","exit_code":1,"summary":"Failed","duration_ms":10}],"replay_events":[{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"$type","session_id":"runtime-1","session_key":"durable-1","event_sequence":4,"payload":$payload}]}}""",
                    )
                },
            )
            val connection = connectReady(observerContract = 2)

            val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

            assertIs<SessionObserverResult.InvalidResponse>(result)
            connection.close()
        }
    }

    @Test
    fun `v2 replay keeps delimiter-colliding compound identities and parent references distinct`() = runBlocking {
        fun replayEvent(sequence: Int, type: String, payload: String): String =
            """{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"$type","session_id":"runtime-1","session_key":"durable-1","event_sequence":$sequence,"payload":$payload}"""

        val replay = listOf(
            replayEvent(11, "todo.update", """{"turn_id":"a","section_id":"b:todo:c","revision":2,"first_event_sequence":1,"operation":"delete"}"""),
            replayEvent(12, "todo.update", """{"turn_id":"a:todo:b","section_id":"c","revision":2,"first_event_sequence":2,"operation":"delete"}"""),
            replayEvent(13, "subagent.update", """{"turn_id":"a","subagent_id":"child","revision":2,"first_event_sequence":5,"operation":"delete"}"""),
            replayEvent(14, "subagent.update", """{"turn_id":"a:subagent:b","subagent_id":"child","revision":2,"first_event_sequence":6,"operation":"delete"}"""),
            replayEvent(15, "subagent.update", """{"turn_id":"a","subagent_id":"b:subagent:c","revision":2,"first_event_sequence":3,"operation":"delete"}"""),
            replayEvent(16, "subagent.update", """{"turn_id":"a:subagent:b","subagent_id":"c","revision":2,"first_event_sequence":4,"operation":"delete"}"""),
            replayEvent(17, "tool.update", """{"turn_id":"a","tool_call_id":"b:tool:c","revision":2,"first_event_sequence":7,"operation":"delete"}"""),
            replayEvent(18, "tool.update", """{"turn_id":"a:tool:b","tool_call_id":"c","revision":2,"first_event_sequence":8,"operation":"delete"}"""),
            replayEvent(19, "terminal.update", """{"turn_id":"a","process_id":"b:terminal:c","revision":2,"first_event_sequence":9,"operation":"delete"}"""),
            replayEvent(20, "terminal.update", """{"turn_id":"a:terminal:b","process_id":"c","revision":2,"first_event_sequence":10,"operation":"delete"}"""),
        ).joinToString(",")
        server.enqueue(
            observerSocket(observerContract = 2) { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":20,"snapshot_event_sequence":10,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[{"turn_id":"a","section_id":"b:todo:c","revision":1,"first_event_sequence":1,"status":"completed","items":[{"id":"one","label":"First","status":"completed"}]},{"turn_id":"a:todo:b","section_id":"c","revision":1,"first_event_sequence":2,"status":"completed","items":[{"id":"two","label":"Second","status":"completed"}]}],"subagents":[{"turn_id":"a","subagent_id":"b:subagent:c","revision":1,"first_event_sequence":3,"parent_subagent_id":null,"name":"Parent A","goal":"","summary":null,"status":"completed"},{"turn_id":"a:subagent:b","subagent_id":"c","revision":1,"first_event_sequence":4,"parent_subagent_id":null,"name":"Parent B","goal":"","summary":null,"status":"completed"},{"turn_id":"a","subagent_id":"child","revision":1,"first_event_sequence":5,"parent_subagent_id":"b:subagent:c","name":"Child A","goal":"","summary":null,"status":"completed"},{"turn_id":"a:subagent:b","subagent_id":"child","revision":1,"first_event_sequence":6,"parent_subagent_id":"c","name":"Child B","goal":"","summary":null,"status":"completed"}],"tools":[{"turn_id":"a","tool_call_id":"b:tool:c","revision":1,"first_event_sequence":7,"status":"completed","name":"Tool A"},{"turn_id":"a:tool:b","tool_call_id":"c","revision":1,"first_event_sequence":8,"status":"completed","name":"Tool B"}],"terminals":[{"turn_id":"a","process_id":"b:terminal:c","revision":1,"first_event_sequence":9,"status":"completed","exit_code":0},{"turn_id":"a:terminal:b","process_id":"c","revision":1,"first_event_sequence":10,"status":"completed","exit_code":0}],"replay_events":[$replay]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 2)

        val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

        assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result)
        connection.close()
    }

    @Test
    fun `v2 snapshot cursor accepts max safe integer and rejects max plus one`() = runBlocking {
        val maximum = 9_007_199_254_740_991L
        listOf(maximum, maximum + 1).forEach { cursor ->
            server.enqueue(
                observerSocket(observerContract = 2) { webSocket, request ->
                    val id = request.getValue("id").jsonPrimitive.content
                    webSocket.send(
                        """{"jsonrpc":"2.0","id":$id,"result":{"observer_contract":2,"subscription_id":"subscription-v2","profile":"default","runtime_generation":"generation-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":$cursor,"snapshot_event_sequence":$cursor,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"todo_sections":[],"subagents":[],"tools":[],"terminals":[],"replay_events":[]}}""",
                    )
                },
            )
            val connection = connectReady(observerContract = 2)

            val result = SessionObserverClient(connection).subscribe(SessionKey("durable-1"), "default")

            if (cursor == maximum) {
                assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result)
            } else {
                assertIs<SessionObserverResult.InvalidResponse>(result)
            }
            connection.close()
        }
    }

    @Test
    fun `subscribe rejects a snapshot for a different durable session key`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-other","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":0,"messages":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects an unexplained replay sequence gap`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":2,"messages":[],"replay_events":[{"type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":2,"payload":{"text":"second"}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe accepts a declared contiguous coalesced replay range`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":2,"messages":[],"inflight":{"user":null,"assistant":"firstsecond","streaming":true,"error":null},"replay_events":[{"type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence_start":1,"event_sequence":2,"payload":{"text":"firstsecond"}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        val subscription = assertIs<SessionObserverResult.Success<SessionObserverSubscription>>(result).value
        assertEquals("firstsecond", subscription.replayEvents.single().payload?.get("text")?.jsonPrimitive?.content)
        connection.close()
    }

    @Test
    fun `subscribe rejects a coalesced range on a lifecycle event`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":2,"messages":[],"replay_events":[{"type":"tool.start","session_id":"runtime-1","session_key":"durable-1","event_sequence_start":1,"event_sequence":2,"payload":{"tool_id":"tool-1"}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects a snapshot without required inflight state`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects running boolean that disagrees with status`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"working","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects replay event outside Cloud observer allowlist`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":true,"error":null},"replay_events":[{"type":"tool.start","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"tool_id":"tool-1"}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.subscribe(SessionKey("durable-1"), observerContractVersion = 1)

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe requires the snapshot watermark field`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects result fields outside the frozen Cloud contract`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[],"extra":true}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects snapshot message fields outside role and content`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":false,"status":"idle","snapshot_event_sequence":0,"event_sequence":0,"messages":[{"role":"assistant","text":"legacy"}],"inflight":{"user":null,"assistant":null,"streaming":false,"error":null},"replay_events":[]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe validates replay payload for its declared Cloud event type`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":null,"streaming":true,"error":null},"replay_events":[{"type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{}}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `subscribe rejects replay fields outside the frozen Cloud contract`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"subscription_id":"subscription-1","session_key":"durable-1","runtime_session_id":"runtime-1","running":true,"status":"working","snapshot_event_sequence":0,"event_sequence":1,"messages":[],"inflight":{"user":null,"assistant":"x","streaming":true,"error":null},"replay_events":[{"type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"text":"x"},"extra":true}]}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `unsubscribe rejects a nonempty result object`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"unsubscribed":true}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)
        val client = SessionObserverClient(connection)

        val result = client.unsubscribe(
            SessionObserverSubscriptionId("subscription-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `observer rejects RPC codes outside the frozen Cloud error catalog`() = runBlocking {
        server.enqueue(
            observerSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"error":{"code":4500,"message":"unexpected"}}""",
                )
            },
        )
        val connection = connectReady(observerContract = 1)

        val result = SessionObserverClient(connection).subscribe(
            SessionKey("durable-1"),
            observerContractVersion = 1,
        )

        assertIs<SessionObserverResult.InvalidResponse>(result)
        connection.close()
    }

    private suspend fun connectReady(observerContract: Int?): GatewayConnection {
        val ready = CompletableDeferred<Unit>()
        val connection = socketClient.connect(
            endpoint = GatewayEndpoint.parse(server.url("/base/").toString()).getOrThrow(),
            ticket = WebSocketTicket("ticket", 30),
            observerContractVersion = observerContract ?: 1,
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) = Unit
            },
        )
        withTimeout(3_000) { ready.await() }
        assertEquals(observerContract, connection.capabilities?.observerContractVersion)
        return connection
    }

    private fun observerSocket(
        observerContract: Int? = 1,
        onRequest: (WebSocket, JsonObject) -> Unit,
    ): MockResponse = MockResponse()
        .setHeader(
            "Sec-WebSocket-Protocol",
            if (observerContract == 2) "hermes.tui.v2" else "hermes.tui.v1",
        )
        .withWebSocketUpgrade(
        object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                val capability = listOfNotNull(
                    observerContract?.let { "\"observer_contract\":$it" },
                    "\"connection_role\":\"observer\"",
                ).joinToString(",")
                webSocket.send(
                    """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{$capability}}}""",
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                onRequest(webSocket, Json.parseToJsonElement(text).jsonObject)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        },
    )
}
