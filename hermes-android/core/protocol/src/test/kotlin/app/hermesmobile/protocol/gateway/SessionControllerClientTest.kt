package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.ScopedWebSocketTicket
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
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
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SessionControllerClientTest {
    private lateinit var server: MockWebServer
    private lateinit var client: SessionControllerClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        client = SessionControllerClient(
            GatewayWebSocketClient(
                OkHttpClient.Builder()
                    .readTimeout(0, TimeUnit.MILLISECONDS)
                    .build(),
            ),
        )
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `control connection verifies scoped role and acquires a redacted typed lease`() = runBlocking {
        val requestFrame = CompletableDeferred<JsonObject>()
        server.enqueue(
            controlSocket { webSocket, request ->
                requestFrame.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"lease_id":"lease-secret","expires_at_epoch_ms":1900000000000,"control_revision":7,"controller_kind":"mobile","controller_label":"Hermes Mobile","pending_input":null}}""",
                )
            },
        )
        val ready = CompletableDeferred<Unit>()
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = controlTicket(),
            observer = object : SessionControllerObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }
            },
        )
        withTimeout(3_000) { ready.await() }

        val result = connection.acquire(
            SessionControlAcquireRequest(
                sessionKey = SessionKey("durable-root-1"),
                profile = "default",
                runtimeSessionId = RuntimeSessionId("runtime-1"),
                clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
            ),
        )

        assertEquals(
            SessionControlReady(
                controlContractVersion = 1,
                connectionRole = GatewayConnectionRole.CONTROL,
                availableMethods = MobileControlMethods.IMPLEMENTED,
                errorCodes = MobileControlErrorCodes.EXPECTED,
            ),
            connection.ready,
        )
        val request = withTimeout(3_000) { requestFrame.await() }
        assertEquals(MobileControlMethods.ACQUIRE, request.getValue("method").jsonPrimitive.content)
        assertEquals(
            setOf("session_key", "profile", "runtime_session_id", "client_instance_id"),
            request.getValue("params").jsonObject.keys,
        )
        val lease = assertIs<SessionControllerResult.Success<SessionControlLease>>(result).value
        assertEquals("lease-secret", lease.leaseId.value)
        assertEquals(1_900_000_000_000L, lease.expiresAtEpochMs)
        assertEquals(7L, lease.controlRevision)
        assertEquals(SessionControllerKind.MOBILE, lease.controllerKind)
        assertFalse(lease.toString().contains("lease-secret"))
        connection.close()
    }

    @Test
    fun `incomplete method catalog rejects control handshake without sending RPC`() = runBlocking {
        val rpcRequest = CompletableDeferred<JsonObject>()
        val protocolError = CompletableDeferred<Unit>()
        server.enqueue(
            controlSocket(
                availableMethods = setOf(MobileControlMethods.ACQUIRE),
                onRequest = { _, request -> rpcRequest.complete(request) },
            ),
        )
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = controlTicket(),
            observer = object : SessionControllerObserver {
                override fun onStateChanged(state: GatewaySocketState) = Unit

                override fun onProtocolError() {
                    protocolError.complete(Unit)
                }
            },
        )

        withTimeout(3_000) { protocolError.await() }
        assertEquals(null, connection.ready)
        assertFalse(rpcRequest.isCompleted)
        connection.close()
    }

    @Test
    fun `steer sends an exact lease-bound mutation and decodes the command result`() = runBlocking {
        val requestFrame = CompletableDeferred<JsonObject>()
        server.enqueue(
            controlSocket { webSocket, request ->
                requestFrame.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"status":"accepted","client_request_id":"steer-1"}}""",
                )
            },
        )
        val connection = readyConnection()

        val result = connection.steer(
            SessionSteerRequest(
                sessionKey = SessionKey("durable-root-1"),
                runtimeSessionId = RuntimeSessionId("runtime-1"),
                leaseId = SessionControlLeaseId("lease-secret"),
                clientRequestId = ClientRequestId("steer-1"),
                text = "Also inspect the authorization path",
            ),
        )

        val request = withTimeout(3_000) { requestFrame.await() }
        assertEquals(MobileControlMethods.SESSION_STEER, request.getValue("method").jsonPrimitive.content)
        val params = request.getValue("params").jsonObject
        assertEquals(
            setOf(
                "session_key",
                "runtime_session_id",
                "lease_id",
                "client_request_id",
                "text",
            ),
            params.keys,
        )
        assertEquals("Also inspect the authorization path", params.getValue("text").jsonPrimitive.content)
        assertEquals(
            SessionSteerResponse(
                status = SessionCommandState.ACCEPTED,
                clientRequestId = ClientRequestId("steer-1"),
            ),
            assertIs<SessionControllerResult.Success<SessionSteerResponse>>(result).value,
        )
        connection.close()
    }

    @Test
    fun `command mutations reject mismatched echoed request and turn identities`() = runBlocking {
        server.enqueue(
            controlSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                val method = request.getValue("method").jsonPrimitive.content
                val params = request.getValue("params").jsonObject
                val requestId = params.getValue("client_request_id").jsonPrimitive.content
                val result = when (method) {
                    MobileControlMethods.PROMPT_SUBMIT -> {
                        val turnId = params.getValue("client_turn_id").jsonPrimitive.content
                        if (requestId == "prompt-wrong-request") {
                            """{"status":"accepted","client_request_id":"other-request","client_turn_id":"$turnId"}"""
                        } else {
                            """{"status":"accepted","client_request_id":"$requestId","client_turn_id":"other-turn"}"""
                        }
                    }
                    MobileControlMethods.SESSION_INTERRUPT ->
                        """{"status":"accepted","client_request_id":"other-interrupt"}"""
                    MobileControlMethods.SESSION_STEER ->
                        """{"status":"accepted","client_request_id":"other-steer"}"""
                    else -> error("Unexpected method: $method")
                }
                webSocket.send("""{"jsonrpc":"2.0","id":$id,"result":$result}""")
            },
        )
        val connection = readyConnection()
        val lease = SessionControlLeaseId("lease-secret")
        val sessionKey = SessionKey("durable-root-1")

        val results = listOf(
            connection.submitPrompt(
                PromptSubmitRequest(
                    sessionKey = sessionKey,
                    leaseId = lease,
                    clientRequestId = ClientRequestId("prompt-wrong-request"),
                    clientTurnId = ClientTurnId("turn-1"),
                    text = "First prompt",
                ),
            ),
            connection.submitPrompt(
                PromptSubmitRequest(
                    sessionKey = sessionKey,
                    leaseId = lease,
                    clientRequestId = ClientRequestId("prompt-wrong-turn"),
                    clientTurnId = ClientTurnId("turn-2"),
                    text = "Second prompt",
                ),
            ),
            connection.interrupt(
                SessionInterruptRequest(
                    sessionKey = sessionKey,
                    leaseId = lease,
                    clientRequestId = ClientRequestId("interrupt-1"),
                ),
            ),
            connection.steer(
                SessionSteerRequest(
                    sessionKey = sessionKey,
                    leaseId = lease,
                    clientRequestId = ClientRequestId("steer-1"),
                    text = "Steer",
                ),
            ),
        )

        assertTrue(results.all { it is SessionControllerResult.InvalidResponse })
        connection.close()
    }

    @Test
    fun `command status rejects a response for a different client request`() = runBlocking {
        server.enqueue(
            controlSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                assertEquals(
                    MobileControlMethods.APPROVAL_RESPOND,
                    request.getValue("params").jsonObject.getValue("method").jsonPrimitive.content,
                )
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"status":"accepted","client_request_id":"different-request"}}""",
                )
            },
        )
        val connection = readyConnection()

        val result = connection.commandStatus(
            SessionCommandStatusRequest(
                sessionKey = SessionKey("durable-root-1"),
                method = MobileControlMethods.APPROVAL_RESPOND,
                clientRequestId = ClientRequestId("expected-request"),
            ),
        )

        assertIs<SessionControllerResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `command status rejects unknown as a successful wire result`() = runBlocking {
        server.enqueue(
            controlSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"status":"unknown","client_request_id":"request-unknown"}}""",
                )
            },
        )
        val connection = readyConnection()

        val result = connection.commandStatus(
            SessionCommandStatusRequest(
                sessionKey = SessionKey("durable-root-1"),
                method = MobileControlMethods.SESSION_INTERRUPT,
                clientRequestId = ClientRequestId("request-unknown"),
            ),
        )

        assertIs<SessionControllerResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `command status rejects method specific result fields`() = runBlocking {
        server.enqueue(
            controlSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"status":"accepted","client_request_id":"request-approval","kind":"approval"}}""",
                )
            },
        )
        val connection = readyConnection()

        val result = connection.commandStatus(
            SessionCommandStatusRequest(
                sessionKey = SessionKey("durable-root-1"),
                method = MobileControlMethods.APPROVAL_RESPOND,
                clientRequestId = ClientRequestId("request-approval"),
            ),
        )

        assertIs<SessionControllerResult.InvalidResponse>(result)
        connection.close()
    }

    @Test
    fun `status decodes a typed server-authoritative approval without exposing payloads`() = runBlocking {
        val result = statusResult(
            pendingInput = """{
                "request_id":"approval-request-secret",
                "kind":"approval",
                "title":"Run command",
                "description":"Sensitive approval description",
                "command":"rm -rf sensitive-path",
                "choices":["allow_once","deny"],
                "expires_at_epoch_ms":1900000000000
            }""".trimIndent(),
        )

        val status = assertIs<SessionControllerResult.Success<SessionControlStatus>>(result).value
        val approval = assertIs<SessionPendingInput.Approval>(status.pendingInput)
        assertEquals("approval-request-secret", approval.requestId)
        assertEquals("Run command", approval.title)
        assertEquals("Sensitive approval description", approval.description)
        assertEquals("rm -rf sensitive-path", approval.command)
        assertEquals(
            listOf(SessionApprovalChoice.ALLOW_ONCE, SessionApprovalChoice.DENY),
            approval.choices,
        )
        assertEquals(1_900_000_000_000L, approval.expiresAtEpochMs)
        val rendered = approval.toString()
        assertFalse(rendered.contains("approval-request-secret"))
        assertFalse(rendered.contains("Sensitive approval description"))
        assertFalse(rendered.contains("rm -rf sensitive-path"))
    }

    @Test
    fun `status decodes a typed server-authoritative clarify without exposing payloads`() = runBlocking {
        val result = statusResult(
            pendingInput = """{
                "request_id":"clarify-request-secret",
                "kind":"clarify",
                "question":"Which sensitive environment?",
                "choices":[
                    {"id":"choice-production","label":"Production"},
                    {"id":"choice-staging","label":"Staging"}
                ],
                "allow_other":true,
                "expires_at_epoch_ms":1900000000001
            }""".trimIndent(),
        )

        val status = assertIs<SessionControllerResult.Success<SessionControlStatus>>(result).value
        val clarify = assertIs<SessionPendingInput.Clarify>(status.pendingInput)
        assertEquals("clarify-request-secret", clarify.requestId)
        assertEquals("Which sensitive environment?", clarify.question)
        assertEquals(
            listOf(
                SessionClarifyChoice("choice-production", "Production"),
                SessionClarifyChoice("choice-staging", "Staging"),
            ),
            clarify.choices,
        )
        assertEquals(true, clarify.allowOther)
        assertEquals(1_900_000_000_001L, clarify.expiresAtEpochMs)
        val rendered = clarify.toString()
        assertFalse(rendered.contains("clarify-request-secret"))
        assertFalse(rendered.contains("Which sensitive environment?"))
        assertFalse(rendered.contains("Production"))
        assertFalse(clarify.choices.first().toString().contains("choice-production"))
    }

    @Test
    fun `status rejects a string boolean in clarify pending input`() = runBlocking {
        val result = statusResult(
            pendingInput = """{
                "request_id":"clarify-request-1",
                "kind":"clarify",
                "question":"Which environment?",
                "choices":[{"id":"production","label":"Production"}],
                "allow_other":"true",
                "expires_at_epoch_ms":1900000000001
            }""".trimIndent(),
        )

        assertIs<SessionControllerResult.InvalidResponse>(result)
        Unit
    }

    @Test
    fun `status rejects unknown pending kinds`() = runBlocking {
        assertInvalidPending(
            """{"request_id":"request-1","kind":"sudo","expires_at_epoch_ms":1}""",
        )
    }

    @Test
    fun `status rejects unknown duplicate or empty approval choices`() = runBlocking {
        assertInvalidPending(
            """{"request_id":"request-1","kind":"approval","title":"Title","description":"","command":"","choices":["allow_once","future_choice"],"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"approval","title":"Title","description":"","command":"","choices":["deny","deny"],"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"approval","title":"Title","description":"","command":"","choices":[],"expires_at_epoch_ms":1}""",
        )
    }

    @Test
    fun `status rejects blank or duplicate clarify choice ids and labels`() = runBlocking {
        assertInvalidPending(
            """{"request_id":"request-1","kind":"clarify","question":"Question","choices":[{"id":" ","label":"One"}],"allow_other":false,"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"clarify","question":"Question","choices":[{"id":"one","label":" "}],"allow_other":false,"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"clarify","question":"Question","choices":[{"id":"same","label":"One"},{"id":"same","label":"Two"}],"allow_other":false,"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"clarify","question":"Question","choices":[{"id":"one","label":"Same"},{"id":"two","label":"Same"}],"allow_other":false,"expires_at_epoch_ms":1}""",
        )
    }

    @Test
    fun `status rejects blank request ids and malformed pending field types`() = runBlocking {
        assertInvalidPending(
            """{"request_id":" ","kind":"approval","title":"Title","description":"","command":"","choices":["deny"],"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"approval","title":7,"description":"","command":"","choices":["deny"],"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"approval","title":"Title","description":null,"command":"","choices":["deny"],"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"approval","title":"Title","description":"","command":false,"choices":["deny"],"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"approval","title":"Title","description":"","command":"","choices":"deny","expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"clarify","question":[],"choices":[],"allow_other":true,"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"clarify","question":"Question","choices":{},"allow_other":true,"expires_at_epoch_ms":1}""",
            """{"request_id":"request-1","kind":"clarify","question":"Question","choices":[],"allow_other":true,"expires_at_epoch_ms":"1"}""",
        )
    }

    @Test
    fun `approval response sends exact request-bound mutation and decodes accepted result`() = runBlocking {
        val requestFrame = CompletableDeferred<JsonObject>()
        server.enqueue(
            controlSocket { webSocket, request ->
                requestFrame.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"status":"accepted","kind":"approval","request_id":"approval-1","client_request_id":"client-1","control_revision":9}}""",
                )
            },
        )
        val connection = readyConnection()

        val result = connection.respondApproval(
            ApprovalRespondRequest(
                sessionKey = SessionKey("durable-root-1"),
                runtimeSessionId = RuntimeSessionId("runtime-1"),
                leaseId = SessionControlLeaseId("lease-secret"),
                clientRequestId = ClientRequestId("client-1"),
                requestId = "approval-1",
                choice = SessionApprovalChoice.ALLOW_ONCE,
            ),
        )

        val request = withTimeout(3_000) { requestFrame.await() }
        assertEquals(MobileControlMethods.APPROVAL_RESPOND, request.getValue("method").jsonPrimitive.content)
        val params = request.getValue("params").jsonObject
        assertEquals(
            setOf(
                "session_key",
                "runtime_session_id",
                "lease_id",
                "client_request_id",
                "request_id",
                "choice",
            ),
            params.keys,
        )
        assertEquals("approval-1", params.getValue("request_id").jsonPrimitive.content)
        assertEquals("allow_once", params.getValue("choice").jsonPrimitive.content)
        val response = assertIs<SessionControllerResult.Success<PendingInputRespondResponse>>(result).value
        assertEquals(PendingInputKind.APPROVAL, response.kind)
        assertEquals("approval-1", response.requestId)
        assertEquals(ClientRequestId("client-1"), response.clientRequestId)
        assertEquals(9L, response.controlRevision)
        connection.close()
    }

    @Test
    fun `clarify response encodes exactly one authoritative answer form`() = runBlocking {
        val requests = mutableListOf<JsonObject>()
        val bothReceived = CompletableDeferred<Unit>()
        server.enqueue(
            controlSocket { webSocket, request ->
                requests += request
                val params = request.getValue("params").jsonObject
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"status":"accepted","kind":"clarify","request_id":"clarify-1","client_request_id":"${params.getValue("client_request_id").jsonPrimitive.content}","control_revision":10}}""",
                )
                if (requests.size == 2) bothReceived.complete(Unit)
            },
        )
        val connection = readyConnection()
        val scope = ClarifyRespondRequest.Scope(
            sessionKey = SessionKey("durable-root-1"),
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            leaseId = SessionControlLeaseId("lease-secret"),
            requestId = "clarify-1",
        )

        assertIs<SessionControllerResult.Success<PendingInputRespondResponse>>(
            connection.respondClarify(
                ClarifyRespondRequest(
                    scope = scope,
                    clientRequestId = ClientRequestId("client-choice"),
                    answer = SessionClarifyAnswer.Choice("choice-production"),
                ),
            ),
        )
        assertIs<SessionControllerResult.Success<PendingInputRespondResponse>>(
            connection.respondClarify(
                ClarifyRespondRequest(
                    scope = scope,
                    clientRequestId = ClientRequestId("client-other"),
                    answer = SessionClarifyAnswer.Other("A custom answer"),
                ),
            ),
        )

        withTimeout(3_000) { bothReceived.await() }
        val choiceParams = requests[0].getValue("params").jsonObject
        val otherParams = requests[1].getValue("params").jsonObject
        assertEquals(MobileControlMethods.CLARIFY_RESPOND, requests[0].getValue("method").jsonPrimitive.content)
        assertEquals("choice-production", choiceParams.getValue("choice_id").jsonPrimitive.content)
        assertNull(choiceParams["other_text"])
        assertEquals("A custom answer", otherParams.getValue("other_text").jsonPrimitive.content)
        assertNull(otherParams["choice_id"])
        connection.close()
    }

    @Test
    fun `pending input response rejects non-accepted or mismatched kinds`() = runBlocking {
        listOf(
            """{"status":"queued","kind":"approval","request_id":"approval-1","client_request_id":"client-1","control_revision":9}""",
            """{"status":"accepted","kind":"clarify","request_id":"approval-1","client_request_id":"client-1","control_revision":9}""",
            """{"status":"accepted","kind":"approval","request_id":"approval-2","client_request_id":"client-1","control_revision":9}""",
            """{"status":"accepted","kind":"approval","request_id":"approval-1","client_request_id":"client-2","control_revision":9}""",
            """{"status":"accepted","kind":"approval","request_id":"approval-1","client_request_id":"client-1","control_revision":-1}""",
            """{"status":"accepted","kind":"approval","request_id":"approval-1","client_request_id":"client-1","control_revision":"9"}""",
            """{"kind":"approval","request_id":"approval-1","client_request_id":"client-1","control_revision":9}""",
            """[]""",
        ).forEach { encodedResult ->
            server.enqueue(
                controlSocket { webSocket, request ->
                    val id = request.getValue("id").jsonPrimitive.content
                    webSocket.send("""{"jsonrpc":"2.0","id":$id,"result":$encodedResult}""")
                },
            )
            val connection = readyConnection()
            val result = connection.respondApproval(
                ApprovalRespondRequest(
                    sessionKey = SessionKey("durable-root-1"),
                    leaseId = SessionControlLeaseId("lease-secret"),
                    clientRequestId = ClientRequestId("client-1"),
                    requestId = "approval-1",
                    choice = SessionApprovalChoice.DENY,
                ),
            )
            assertIs<SessionControllerResult.InvalidResponse>(result)
            connection.close()
        }
    }

    @Test
    fun `status normalizes local owner to canonical desktop and enforces nullable labels`() = runBlocking {
        val local = assertIs<SessionControllerResult.Success<SessionControlStatus>>(
            statusResult(controllerKind = "local", controllerLabel = "Hermes Local"),
        ).value
        val none = assertIs<SessionControllerResult.Success<SessionControlStatus>>(
            statusResult(controllerKind = "none", controllerLabel = null),
        ).value

        assertEquals(SessionControllerKind.DESKTOP, local.controllerKind)
        assertEquals("Hermes Local", local.controllerLabel)
        assertEquals(SessionControllerKind.NONE, none.controllerKind)
        assertNull(none.controllerLabel)
        listOf(
            "none" to "Hermes",
            "desktop" to null,
            "desktop" to "",
            "mobile" to null,
        ).forEach { (controllerKind, controllerLabel) ->
            assertIs<SessionControllerResult.InvalidResponse>(
                statusResult(
                    controllerKind = controllerKind,
                    controllerLabel = controllerLabel,
                ),
            )
        }
        Unit
    }

    @Test
    fun `control connection rejects an observer scoped ticket before opening a socket`() {
        assertFailsWith<IllegalArgumentException> {
            client.connect(
                endpoint = endpoint(),
                ticket = ScopedWebSocketTicket(
                    value = "observer-ticket",
                    ttlSeconds = 30,
                    connectionRole = GatewayConnectionRole.OBSERVER,
                ),
                observer = NOOP_OBSERVER,
            )
        }
        assertEquals(0, server.requestCount)
    }

    private suspend fun assertInvalidPending(vararg pendingInputs: String) {
        pendingInputs.forEach { pendingInput ->
            assertIs<SessionControllerResult.InvalidResponse>(statusResult(pendingInput))
        }
    }

    private suspend fun statusResult(pendingInput: String): SessionControllerResult<SessionControlStatus> {
        val encodedPendingInput = Json.parseToJsonElement(pendingInput).toString()
        server.enqueue(
            controlSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"controller_kind":"mobile","controller_label":"Hermes Mobile","control_revision":8,"lease_expires_at_epoch_ms":1900000000000,"pending_input":$encodedPendingInput}}""",
                )
            },
        )
        val ready = CompletableDeferred<Unit>()
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = controlTicket(),
            observer = object : SessionControllerObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }
            },
        )
        withTimeout(3_000) { ready.await() }
        return connection.status(
            SessionControlStatusRequest(sessionKey = SessionKey("durable-root-1")),
        ).also { connection.close() }
    }

    private suspend fun statusResult(
        controllerKind: String,
        controllerLabel: String?,
    ): SessionControllerResult<SessionControlStatus> {
        val encodedLabel = controllerLabel?.let { Json.encodeToString(it) } ?: "null"
        server.enqueue(
            controlSocket { webSocket, request ->
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"controller_kind":"$controllerKind","controller_label":$encodedLabel,"control_revision":8,"lease_expires_at_epoch_ms":1900000000000,"pending_input":null}}""",
                )
            },
        )
        val connection = readyConnection()
        return connection.status(
            SessionControlStatusRequest(sessionKey = SessionKey("durable-root-1")),
        ).also { connection.close() }
    }

    private suspend fun readyConnection(): SessionControllerConnection {
        val ready = CompletableDeferred<Unit>()
        val connection = client.connect(
            endpoint = endpoint(),
            ticket = controlTicket(),
            observer = object : SessionControllerObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    if (state == GatewaySocketState.Ready) ready.complete(Unit)
                }
            },
        )
        withTimeout(3_000) { ready.await() }
        return connection
    }

    private fun endpoint(): GatewayEndpoint =
        GatewayEndpoint.parse(server.url("/base/").toString()).getOrThrow()

    private fun controlTicket() = ScopedWebSocketTicket(
        value = "control-ticket-secret",
        ttlSeconds = 30,
        connectionRole = GatewayConnectionRole.CONTROL,
    )

    private fun controlSocket(
        availableMethods: Set<String> = MobileControlMethods.IMPLEMENTED,
        onRequest: (WebSocket, JsonObject) -> Unit,
    ): MockResponse {
        val encodedMethods = availableMethods.sorted().joinToString(
            prefix = "[",
            postfix = "]",
        ) { method -> "\"$method\"" }
        return MockResponse()
            .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
            .withWebSocketUpgrade(
        object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(
                    """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"control_contract":1,"connection_role":"control","control_available_methods":$encodedMethods,"control_error_codes":{"control_role_required":4200,"control_contract_unsupported":4201,"live_runtime_unavailable":4202,"controller_conflict":4203,"lease_required":4204,"lease_expired":4205,"lease_mismatch":4206,"request_id_payload_conflict":4207,"pending_request_conflict":4208,"method_not_allowed":4209,"command_unknown":4210,"revision_conflict":4211,"session_binding_mismatch":4212,"invalid_pending_response":4213,"owner_adapter_unavailable":4214,"relay_overloaded":4215,"deadline_exceeded_before_effect":4306,"effect_unknown":4307}}}}""",
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

    private companion object {
        val NOOP_OBSERVER = object : SessionControllerObserver {
            override fun onStateChanged(state: GatewaySocketState) = Unit
        }
    }
}
