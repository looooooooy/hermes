package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.WebSocketTicket
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import kotlinx.serialization.json.buildJsonObject
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.ByteString.Companion.encodeUtf8
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

class GatewayWebSocketClientTest {
    private lateinit var server: MockWebServer
    private lateinit var client: GatewayWebSocketClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        client = GatewayWebSocketClient(
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
    fun `connect uses one-time ticket and Hermes subprotocol then waits for gateway ready`() = runBlocking {
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

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val ready = CompletableDeferred<Unit>()
        val events = mutableListOf<GatewayEvent>()
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = WebSocketTicket("one time ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) {
                    events += event
                }
            },
        )

        withTimeout(3_000) { ready.await() }

        val request = server.takeRequest()
        assertEquals("/base/api/ws?ticket=one%20time%20ticket", request.path)
        assertEquals("hermes.tui.v1", request.getHeader("Sec-WebSocket-Protocol"))
        assertFalse(request.headers.names().contains("Authorization"))
        assertEquals("gateway.ready", events.single().type)
        assertEquals(GatewaySocketState.Ready, connection.state)
        assertEquals(
            GatewayCapabilities(
                observerContractVersion = 1,
                connectionRole = GatewayConnectionRole.OBSERVER,
            ),
            connection.capabilities,
        )
        connection.close()
    }

    @Test
    fun `observer connection accepts an exact v2 gateway ready advertisement`() = runBlocking {
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

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val ready = CompletableDeferred<Unit>()
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = WebSocketTicket("v2-ticket", 30),
            observerContractVersion = 2,
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) = Unit
            },
        )

        withTimeout(3_000) { ready.await() }

        assertEquals("hermes.tui.v2", server.takeRequest().getHeader("Sec-WebSocket-Protocol"))
        assertEquals(2, connection.capabilities?.observerContractVersion)
        assertTrue(connection.capabilities?.supportsSessionObserver(2) == true)
        assertFalse(connection.capabilities?.supportsSessionObserver(1) == true)
        connection.close()
    }

    @Test
    fun `sensitive v2 event fails before observer callback can echo it`() = runBlocking {
        val ready = CompletableDeferred<Unit>()
        val protocolError = CompletableDeferred<Unit>()
        val events = mutableListOf<GatewayEvent>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v2")
                .withWebSocketUpgrade(
                    object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: Response) {
                            webSocket.send(
                                """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":2,"connection_role":"observer"}}}""",
                            )
                            webSocket.send(
                                """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"text":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"}}}""",
                            )
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, reason)
                        }
                    },
                ),
        )
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = WebSocketTicket("v2-ticket", 30),
            observerContractVersion = 2,
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) {
                    events += event
                }

                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        withTimeout(3_000) { ready.await() }
        delay(250)
        assertEquals(listOf("gateway.ready"), events.map(GatewayEvent::type))
        withTimeout(3_000) { protocolError.await() }
        connection.close()
    }

    @Test
    fun `display-safe Basic prose reaches observer callback without a protocol error`() = runBlocking {
        val ready = CompletableDeferred<Unit>()
        val delivered = CompletableDeferred<GatewayEvent>()
        val protocolErrors = mutableListOf<Unit>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v2")
                .withWebSocketUpgrade(
                    object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: Response) {
                            webSocket.send(
                                """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":2,"connection_role":"observer"}}}""",
                            )
                            webSocket.send(
                                """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"message.delta","session_id":"runtime-1","session_key":"durable-1","event_sequence":1,"payload":{"text":"Basic authentication is disabled. Basic YWJjZA== is not a user-password credential."}}}""",
                            )
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, reason)
                        }
                    },
                ),
        )
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = WebSocketTicket("v2-ticket", 30),
            observerContractVersion = 2,
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) {
                    if (event.type == "message.delta") delivered.complete(event)
                }

                override fun onProtocolError() {
                    protocolErrors += Unit
                }
            },
        )

        withTimeout(3_000) { ready.await() }
        assertEquals("message.delta", withTimeout(3_000) { delivered.await() }.type)
        assertTrue(protocolErrors.isEmpty())
        connection.close()
    }

    @Test
    fun `v2 selected websocket subprotocol mismatch fails before gateway ready`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val protocolError = CompletableDeferred<Unit>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                    object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: Response) {
                            webSocket.send(
                                """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":2,"connection_role":"observer"}}}""",
                            )
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            closeCode.complete(code)
                            webSocket.close(code, reason)
                        }
                    },
                ),
        )
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = WebSocketTicket("v2-ticket", 30),
            observerContractVersion = 2,
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit
                override fun onEvent(event: GatewayEvent) = Unit
                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        assertEquals("hermes.tui.v2", server.takeRequest().getHeader("Sec-WebSocket-Protocol"))
        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        withTimeout(3_000) { protocolError.await() }
        assertFalse(connection.state is GatewaySocketState.Ready)
        connection.close()
    }

    @Test
    fun `v2 gateway ready contract mismatch fails closed`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v2")
                .withWebSocketUpgrade(
                    object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: Response) {
                            webSocket.send(
                                """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}""",
                            )
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            closeCode.complete(code)
                            webSocket.close(code, reason)
                        }
                    },
                ),
        )
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = WebSocketTicket("v2-ticket", 30),
            observerContractVersion = 2,
            observer = NOOP_OBSERVER,
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        assertFalse(connection.state is GatewaySocketState.Ready)
        connection.close()
    }

    @Test
    fun `correlates RPC results and preserves server RPC errors`() = runBlocking {
        val json = Json
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
                        val request = json.parseToJsonElement(text) as JsonObject
                        val id = request.getValue("id").jsonPrimitive.long
                        when (request.getValue("method").jsonPrimitive.content) {
                            "session.info" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"result":{"model":"test-model"}}""",
                            )
                            "prompt.submit" -> webSocket.send(
                                """{"jsonrpc":"2.0","id":$id,"error":{"code":4009,"message":"session busy"}}""",
                            )
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val ready = CompletableDeferred<Unit>()
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) = Unit
            },
        )
        withTimeout(3_000) { ready.await() }

        val info = connection.call("session.info", buildJsonObject { put("session_id", "runtime-1") })
        val busy = connection.call("prompt.submit", buildJsonObject { put("text", "hello") })

        val success = assertIs<GatewayCallResult.Success>(info)
        assertEquals("test-model", (success.value as JsonObject).getValue("model").jsonPrimitive.content)
        val failure = assertIs<GatewayCallResult.RpcFailure>(busy)
        assertEquals(4009, failure.error.code)
        assertEquals("session busy", failure.error.message)
        connection.close()
    }

    @Test
    fun `call does not send before gateway ready`() = runBlocking {
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
        val connection = client.connect(endpoint(), WebSocketTicket("ticket", 30), NOOP_OBSERVER)

        val result = connection.call("session.info")

        assertIs<GatewayCallResult.NotReady>(result)
        connection.close()
    }

    @Test
    fun `cancelled rpc call is removed from pending requests`() = runBlocking {
        val requestSeen = CompletableDeferred<Unit>()
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
                        requestSeen.complete(Unit)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val ready = CompletableDeferred<Unit>()
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) = Unit
            },
        )
        withTimeout(3_000) { ready.await() }
        val call = launch { connection.call("session.info") }
        withTimeout(3_000) { requestSeen.await() }

        call.cancelAndJoin()

        assertEquals("GatewayConnection(state=Ready, pendingCalls=0)", connection.toString())
        connection.close()
    }

    @Test
    fun `oversized aggregate text frame reports protocol error and closes with message too big`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val protocolError = CompletableDeferred<Unit>()
        val document =
            """{"jsonrpc":"2.0","method":"notice","params":{"padding":"${"x".repeat(JsonRpcCodec.MAX_FRAME_CHARS / 2)}"}}"""
        assertTrue(document.length <= JsonRpcCodec.MAX_FRAME_CHARS)
        val oversizedFrame = "$document\n$document"
        assertTrue(oversizedFrame.length > JsonRpcCodec.MAX_FRAME_CHARS)
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(oversizedFrame)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit
                override fun onEvent(event: GatewayEvent) = Unit
                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        assertEquals(1009, withTimeout(3_000) { closeCode.await() })
        withTimeout(3_000) { protocolError.await() }
        connection.close()
    }

    @Test
    fun `one websocket text frame must contain exactly one JSON document`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val protocolError = CompletableDeferred<Unit>()
        val readyFrame =
            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer"}}}"""
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send("$readyFrame\n$readyFrame")
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit
                override fun onEvent(event: GatewayEvent) = Unit
                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        withTimeout(3_000) { protocolError.await() }
        connection.close()
    }

    @Test
    fun `text frame limit is measured in UTF-8 bytes`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val protocolError = CompletableDeferred<Unit>()
        val oversizedUtf8Frame =
            """{"jsonrpc":"2.0","method":"notice","params":{"padding":"${"中".repeat(90_000)}"}}"""
        assertTrue(oversizedUtf8Frame.length < 256 * 1024)
        assertTrue(oversizedUtf8Frame.encodeToByteArray().size > 256 * 1024)
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(oversizedUtf8Frame)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit
                override fun onEvent(event: GatewayEvent) = Unit
                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        assertEquals(1009, withTimeout(3_000) { closeCode.await() })
        withTimeout(3_000) { protocolError.await() }
        connection.close()
    }

    @Test
    fun `binary websocket frame closes with protocol error`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val protocolError = CompletableDeferred<Unit>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send("""{"jsonrpc":"2.0"}""".encodeUtf8())
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit
                override fun onEvent(event: GatewayEvent) = Unit
                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        withTimeout(3_000) { protocolError.await() }
        connection.close()
    }

    @Test
    fun `first server frame must be gateway ready`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val protocolError = CompletableDeferred<Unit>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"notice","params":{"state":"warming"}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit
                override fun onEvent(event: GatewayEvent) = Unit
                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        withTimeout(3_000) { protocolError.await() }
        connection.close()
    }

    @Test
    fun `Cloud observer ready payload rejects undeclared fields`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"connection_role":"observer","skin":"extra"}}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = NOOP_OBSERVER,
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        connection.close()
    }

    @Test
    fun `Cloud observer ready payload requires the exact observer schema`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1}}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = NOOP_OBSERVER,
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        assertFalse(connection.state is GatewaySocketState.Ready)
        connection.close()
    }

    @Test
    fun `Cloud control ready payload rejects omitted capability declarations`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"control_contract":1,"connection_role":"control"}}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = NOOP_OBSERVER,
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        assertFalse(connection.state is GatewaySocketState.Ready)
        connection.close()
    }

    @Test
    fun `Cloud control ready payload rejects undeclared fields`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val payloadWithExtra = CONTROL_READY_PAYLOAD.dropLast(1) + ""","skin":"extra"}"""
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":$payloadWithExtra}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = NOOP_OBSERVER,
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        assertFalse(connection.state is GatewaySocketState.Ready)
        connection.close()
    }

    @Test
    fun `Cloud runtime method order is accepted when the exact set is advertised`() = runBlocking {
        val ready = CompletableDeferred<Unit>()
        val runtimeOrderedPayload = CONTROL_READY_PAYLOAD.replace(
            "[\"approval.respond\",\"clarify.respond\",\"prompt.submit\",\"session.command.status\",\"session.control.acquire\",\"session.control.release\",\"session.control.renew\",\"session.control.status\",\"session.interrupt\",\"session.steer\"]",
            "[\"session.control.acquire\",\"session.control.renew\",\"session.control.release\",\"session.control.status\",\"session.command.status\",\"prompt.submit\",\"session.interrupt\",\"session.steer\",\"approval.respond\",\"clarify.respond\"]",
        )
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":$runtimeOrderedPayload}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) = Unit
            },
        )

        withTimeout(3_000) { ready.await() }
        assertEquals(MobileControlMethods.IMPLEMENTED, connection.capabilities?.controlAvailableMethods)
        connection.close()
    }

    @Test
    fun `Cloud control ready requires the exact authoritative method and error catalogs`() {
        val requiredErrors = linkedMapOf(
            "control_role_required" to 4200,
            "control_contract_unsupported" to 4201,
            "live_runtime_unavailable" to 4202,
            "controller_conflict" to 4203,
            "lease_required" to 4204,
            "lease_expired" to 4205,
            "lease_mismatch" to 4206,
            "request_id_payload_conflict" to 4207,
            "pending_request_conflict" to 4208,
            "method_not_allowed" to 4209,
            "command_unknown" to 4210,
            "revision_conflict" to 4211,
            "session_binding_mismatch" to 4212,
            "invalid_pending_response" to 4213,
            "owner_adapter_unavailable" to 4214,
            "relay_overloaded" to 4215,
            "deadline_exceeded_before_effect" to 4306,
            "effect_unknown" to 4307,
        )
        val exact = GatewayCapabilities(
            observerContractVersion = 1,
            controlContractVersion = 1,
            connectionRole = GatewayConnectionRole.CONTROL,
            controlAvailableMethods = MobileControlMethods.IMPLEMENTED,
            controlErrorCodes = requiredErrors,
        )

        assertEquals(requiredErrors, MobileControlErrorCodes.EXPECTED)
        assertTrue(exact.supportsSessionControl())
        assertFalse(
            exact.copy(
                controlAvailableMethods = MobileControlMethods.IMPLEMENTED -
                    MobileControlMethods.PROMPT_SUBMIT,
            ).supportsSessionControl(),
        )
        assertFalse(
            exact.copy(
                controlErrorCodes = requiredErrors - "effect_unknown",
            ).supportsSessionControl(),
        )
        assertFalse(
            exact.copy(
                controlErrorCodes = requiredErrors + ("effect_unknown" to 4306),
            ).supportsSessionControl(),
        )
    }

    @Test
    fun `Cloud control ready rejects advertised method and error catalog subsets`() = runBlocking {
        val closeCode = CompletableDeferred<Int>()
        val ready = CompletableDeferred<Unit>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.send(
                            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"control_contract":1,"connection_role":"control","control_available_methods":[],"control_error_codes":{"live_runtime_unavailable":4202,"method_not_allowed":4209}}}}""",
                        )
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        closeCode.complete(code)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val connection = client.connect(
            endpoint(),
            WebSocketTicket("ticket", 30),
            observer = object : GatewaySocketObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }

                override fun onEvent(event: GatewayEvent) = Unit
            },
        )

        assertEquals(1002, withTimeout(3_000) { closeCode.await() })
        assertFalse(ready.isCompleted)
        assertFalse(connection.state is GatewaySocketState.Ready)
        connection.close()
    }

    @Test
    fun `observer capability is available only on an observer role connection`() {
        val observerCapabilities = GatewayCapabilities(
            observerContractVersion = 1,
            connectionRole = GatewayConnectionRole.OBSERVER,
        )
        val controlCapabilities = GatewayCapabilities(
            observerContractVersion = 1,
            controlContractVersion = 1,
            connectionRole = GatewayConnectionRole.CONTROL,
            controlAvailableMethods = MobileControlMethods.IMPLEMENTED,
            controlErrorCodes = MobileControlErrorCodes.EXPECTED,
        )
        val incompleteControlCapabilities = controlCapabilities.copy(controlAvailableMethods = null)
        val subsetControlCapabilities = controlCapabilities.copy(
            controlAvailableMethods = emptySet(),
            controlErrorCodes = mapOf(
                "live_runtime_unavailable" to 4202,
                "method_not_allowed" to 4209,
            ),
        )
        val unknownErrorCapabilities = subsetControlCapabilities.copy(
            controlErrorCodes = mapOf("new_error" to 4202),
        )
        val wrongErrorCodeCapabilities = subsetControlCapabilities.copy(
            controlErrorCodes = mapOf("live_runtime_unavailable" to 4215),
        )
        val duplicateSemanticCodeCapabilities = subsetControlCapabilities.copy(
            controlErrorCodes = mapOf(
                "live_runtime_unavailable" to 4202,
                "method_not_allowed" to 4202,
            ),
        )

        assertTrue(observerCapabilities.supportsSessionObserver(1))
        assertFalse(controlCapabilities.supportsSessionObserver())
        assertTrue(controlCapabilities.supportsSessionControl())
        assertFalse(subsetControlCapabilities.supportsSessionControl())
        assertFalse(incompleteControlCapabilities.supportsSessionControl())
        assertFalse(unknownErrorCapabilities.supportsSessionControl())
        assertFalse(wrongErrorCodeCapabilities.supportsSessionControl())
        assertFalse(duplicateSemanticCodeCapabilities.supportsSessionControl())
    }

    private fun endpoint(): GatewayEndpoint =
        GatewayEndpoint.parse(server.url("/base/").toString()).getOrThrow()

    private companion object {
        const val CONTROL_READY_PAYLOAD =
            """{"observer_contract":1,"control_contract":1,"connection_role":"control","control_available_methods":["approval.respond","clarify.respond","prompt.submit","session.command.status","session.control.acquire","session.control.release","session.control.renew","session.control.status","session.interrupt","session.steer"],"control_error_codes":{"control_role_required":4200,"control_contract_unsupported":4201,"live_runtime_unavailable":4202,"controller_conflict":4203,"lease_required":4204,"lease_expired":4205,"lease_mismatch":4206,"request_id_payload_conflict":4207,"pending_request_conflict":4208,"method_not_allowed":4209,"command_unknown":4210,"revision_conflict":4211,"session_binding_mismatch":4212,"invalid_pending_response":4213,"owner_adapter_unavailable":4214,"relay_overloaded":4215,"deadline_exceeded_before_effect":4306,"effect_unknown":4307}}"""
        val NOOP_OBSERVER = object : GatewaySocketObserver {
            override fun onStateChanged(state: GatewaySocketState) = Unit
            override fun onEvent(event: GatewayEvent) = Unit
        }
    }
}
