package app.hermesmobile.protocol.auth

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.gateway.GatewayConnectionRole
import app.hermesmobile.protocol.sessions.SessionKey
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertTrue

class NativeAuthClientTest {
    private lateinit var server: MockWebServer
    private lateinit var endpoint: GatewayEndpoint
    private lateinit var client: NativeAuthClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        endpoint = GatewayEndpoint.parse(server.url("/edge/").toString()).getOrThrow()
        client = NativeAuthClient(OkHttpClient())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `secure cookie jar login mints observer and control tickets without bearer headers`() = runTest {
        server.shutdown()
        val serverCertificate = HeldCertificate.Builder()
            .commonName("localhost")
            .addSubjectAlternativeName("localhost")
            .build()
        val serverCertificates = HandshakeCertificates.Builder()
            .heldCertificate(serverCertificate)
            .build()
        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(serverCertificate.certificate)
            .build()
        server = MockWebServer().apply {
            useHttps(serverCertificates.sslSocketFactory(), false)
            start()
        }
        endpoint = GatewayEndpoint.parse(server.url("/edge/").toString()).getOrThrow()
        val cookieJar = HermesSessionCookieJar()
        client = NativeAuthClient(
            OkHttpClient.Builder()
                .sslSocketFactory(
                    clientCertificates.sslSocketFactory(),
                    clientCertificates.trustManager,
                )
                .cookieJar(cookieJar)
                .build(),
        )
        server.enqueue(
            MockResponse()
                .addHeader(
                    "Set-Cookie",
                    "hermes_session_at=access-secret; Max-Age=300; Path=/; Secure; HttpOnly; SameSite=Strict",
                )
                .addHeader(
                    "Set-Cookie",
                    "hermes_session_rt=refresh-secret; Path=/; Secure; HttpOnly; SameSite=Strict",
                )
                .addHeader(
                    "Set-Cookie",
                    "hermes_session_provider=basic; Path=/; Secure; HttpOnly; SameSite=Strict",
                )
                .setHeader("Content-Type", "application/json")
                .setBody("""{"ok":true}"""),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"ticket":"observer-ticket","ttl_seconds":30,"connection_role":"observer"}""",
                ),
        )
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"ticket":"control-ticket","ttl_seconds":30,"connection_role":"control"}""",
                ),
        )

        assertIs<NativeAuthResult.Success<NativeTokens>>(
            client.passwordLogin(endpoint, "mobile-user", "correct-password"),
        )
        val clientInstanceId = ClientInstanceId(
            "11111111-1111-4111-8111-111111111111",
        )
        assertIs<NativeAuthResult.Success<ScopedWebSocketTicket>>(
            client.mintCookieWebSocketTicket(
                endpoint,
                ScopedWebSocketTicketRequest(
                    connectionRole = GatewayConnectionRole.OBSERVER,
                    clientInstanceId = clientInstanceId,
                    observerContractVersion = 1,
                ),
            ),
        )
        assertIs<NativeAuthResult.Success<ScopedWebSocketTicket>>(
            client.mintCookieWebSocketTicket(
                endpoint,
                ScopedWebSocketTicketRequest(
                    connectionRole = GatewayConnectionRole.CONTROL,
                    clientInstanceId = clientInstanceId,
                    sessionKey = SessionKey("durable-root-1"),
                    profile = "default",
                ),
            ),
        )

        server.takeRequest()
        val observerRequest = server.takeRequest()
        val controlRequest = server.takeRequest()
        val expectedOrigin = "https://localhost:${server.port}"
        listOf(observerRequest, controlRequest).forEach { request ->
            assertEquals(expectedOrigin, request.getHeader("Origin"))
            assertEquals(null, request.getHeader("Authorization"))
            val cookieHeader = requireNotNull(request.getHeader("Cookie"))
            assertTrue("hermes_session_at=" in cookieHeader)
            assertTrue("hermes_session_rt=" in cookieHeader)
            assertFalse("correct-password" in cookieHeader)
        }
        val observerBody = Json.parseToJsonElement(
            observerRequest.body.readUtf8(),
        ).jsonObject
        assertEquals(
            setOf("connection_role", "client_instance_id"),
            observerBody.keys,
        )
        assertEquals("observer", observerBody.getValue("connection_role").jsonPrimitive.content)
        assertEquals(
            clientInstanceId.value,
            observerBody.getValue("client_instance_id").jsonPrimitive.content,
        )
        assertEquals(
            setOf("connection_role", "client_instance_id", "session_key", "profile"),
            Json.parseToJsonElement(controlRequest.body.readUtf8()).jsonObject.keys,
        )
    }

    @Test
    fun `authorization URL preserves ingress prefix and binds PKCE state`() {
        val credentials = PkceCredentials(
            verifier = "v".repeat(43),
            challenge = "challenge",
            state = "state-value",
        )

        val url = client.authorizationUrl(
            endpoint = endpoint,
            redirectUri = "http://127.0.0.1:54321/oauth/callback",
            credentials = credentials,
            provider = "nous",
        )

        assertEquals("/edge/auth/native/authorize", url.encodedPath)
        assertEquals("challenge", url.queryParameter("code_challenge"))
        assertEquals("S256", url.queryParameter("code_challenge_method"))
        assertEquals("http://127.0.0.1:54321/oauth/callback", url.queryParameter("redirect_uri"))
        assertEquals("state-value", url.queryParameter("state"))
        assertEquals("nous", url.queryParameter("provider"))
    }

    @Test
    fun `authorization URL rejects non-loopback callback`() {
        val credentials = PkceCredentials("v".repeat(43), "challenge", "state")

        val result = runCatching {
            client.authorizationUrl(
                endpoint,
                "https://mobile.example.com/callback",
                credentials,
            )
        }

        assertTrue(result.isFailure)
    }

    @Test
    fun `one-time code is exchanged for redacted bearer tokens`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"access_token":"access-secret","refresh_token":"refresh-secret","token_type":"Bearer","expires_at":1900000000,"provider":"nous","user_id":"user-1"}""",
                ),
        )

        val result = client.exchangeCode(endpoint, "gateway-code", "v".repeat(43))

        val tokens = assertIs<NativeAuthResult.Success<NativeTokens>>(result).value
        assertEquals("access-secret", tokens.accessToken)
        assertEquals("refresh-secret", tokens.refreshToken)
        assertEquals("nous", tokens.provider)
        assertFalse(tokens.toString().contains("access-secret"))
        assertFalse(tokens.toString().contains("refresh-secret"))

        val request = server.takeRequest()
        assertEquals("/edge/auth/native/token", request.path)
        assertEquals("application/json; charset=utf-8", request.getHeader("Content-Type"))
        assertTrue(request.body.readUtf8().contains("\"code\":\"gateway-code\""))
        assertEquals(null, request.getHeader("Authorization"))
    }

    @Test
    fun `password login extracts provider tokens from secure session cookies`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .addHeader(
                    "Set-Cookie",
                    "__Secure-hermes_session_at=access-secret; Max-Age=43200; Path=/edge; Secure; HttpOnly; SameSite=Lax",
                )
                .addHeader(
                    "Set-Cookie",
                    "__Secure-hermes_session_rt=refresh-secret; Max-Age=2592000; Path=/edge; Secure; HttpOnly; SameSite=Lax",
                )
                .addHeader(
                    "Set-Cookie",
                    "__Secure-hermes_session_provider=basic; Max-Age=2592000; Path=/edge; Secure; HttpOnly; SameSite=Lax",
                )
                .setBody("""{"ok":true,"next":"/"}"""),
        )

        val result = client.passwordLogin(
            endpoint = endpoint,
            username = "mobile-user",
            password = "correct horse battery staple",
        )

        val tokens = assertIs<NativeAuthResult.Success<NativeTokens>>(result).value
        assertEquals("access-secret", tokens.accessToken)
        assertEquals("refresh-secret", tokens.refreshToken)
        assertEquals("basic", tokens.provider)
        assertEquals("mobile-user", tokens.userId)
        assertTrue(tokens.expiresAtEpochSeconds > 0)
        assertFalse(tokens.toString().contains("access-secret"))
        assertFalse(tokens.toString().contains("refresh-secret"))

        val request = server.takeRequest()
        assertEquals("/edge/auth/password-login", request.path)
        assertEquals("application/json; charset=utf-8", request.getHeader("Content-Type"))
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals(
            setOf("provider", "username", "password", "next"),
            body.keys,
        )
        assertEquals("basic", body.getValue("provider").jsonPrimitive.content)
        assertEquals("mobile-user", body.getValue("username").jsonPrimitive.content)
        assertEquals(
            "correct horse battery staple",
            body.getValue("password").jsonPrimitive.content,
        )
        assertEquals("", body.getValue("next").jsonPrimitive.content)
        assertEquals(null, request.getHeader("Authorization"))
    }

    @Test
    fun `refresh rotates both tokens and prioritizes original provider`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"access_token":"new-access","refresh_token":"new-refresh","token_type":"Bearer","expires_at":1900000100,"provider":"nous","user_id":"user-1"}""",
                ),
        )
        val current = NativeTokens(
            accessToken = "old-access",
            refreshToken = "old-refresh",
            tokenType = "Bearer",
            expiresAtEpochSeconds = 1800000000,
            provider = "nous",
            userId = "user-1",
        )

        val result = client.refresh(endpoint, current)

        val rotated = assertIs<NativeAuthResult.Success<NativeTokens>>(result).value
        assertEquals("new-access", rotated.accessToken)
        assertEquals("new-refresh", rotated.refreshToken)
        val request = server.takeRequest()
        assertEquals("/edge/auth/native/refresh", request.path)
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"refresh_token\":\"old-refresh\""))
        assertTrue(body.contains("\"provider\":\"nous\""))
    }

    @Test
    fun `websocket ticket uses bearer token and parses short TTL`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"ticket":"one-time-ticket","ttl_seconds":30}"""),
        )

        val result = client.mintWebSocketTicket(endpoint, "access-secret")

        val ticket = assertIs<NativeAuthResult.Success<WebSocketTicket>>(result).value
        assertEquals("one-time-ticket", ticket.value)
        assertEquals(30, ticket.ttlSeconds)
        val request = server.takeRequest()
        assertEquals("/edge/api/auth/ws-ticket", request.path)
        assertEquals("Bearer access-secret", request.getHeader("Authorization"))
        assertEquals(emptySet(), Json.parseToJsonElement(request.body.readUtf8()).jsonObject.keys)
    }

    @Test
    fun `scoped websocket ticket sends typed control role and verifies echoed role`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"ticket":"control-ticket-secret","ttl_seconds":30,"connection_role":"control"}""",
                ),
        )
        val request = ScopedWebSocketTicketRequest(
            connectionRole = GatewayConnectionRole.CONTROL,
            clientInstanceId = ClientInstanceId("123E4567-E89B-12D3-A456-426614174000"),
            sessionKey = SessionKey("durable-root-1"),
            profile = "default",
        )

        val result = client.mintWebSocketTicket(endpoint, "access-secret", request)

        val ticket = assertIs<NativeAuthResult.Success<ScopedWebSocketTicket>>(result).value
        assertEquals("control-ticket-secret", ticket.value)
        assertEquals(30, ticket.ttlSeconds)
        assertEquals(GatewayConnectionRole.CONTROL, ticket.connectionRole)
        assertFalse(ticket.toString().contains("control-ticket-secret"))

        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertEquals(
            setOf("connection_role", "client_instance_id", "session_key", "profile"),
            body.keys,
        )
        assertEquals("control", body.getValue("connection_role").jsonPrimitive.content)
        assertEquals(
            "123e4567-e89b-12d3-a456-426614174000",
            body.getValue("client_instance_id").jsonPrimitive.content,
        )
        assertEquals("durable-root-1", body.getValue("session_key").jsonPrimitive.content)
        assertEquals("default", body.getValue("profile").jsonPrimitive.content)
    }

    @Test
    fun `scoped observer websocket ticket defaults to an exact v2 selection`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"ticket":"observer-ticket-secret","ttl_seconds":30,"connection_role":"observer","observer_contract":2}""",
                ),
        )

        val result = client.mintWebSocketTicket(
            endpoint = endpoint,
            accessToken = "access-secret",
            request = ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.OBSERVER,
                clientInstanceId = ClientInstanceId("33333333-3333-4333-8333-333333333333"),
            ),
        )

        assertIs<NativeAuthResult.Success<ScopedWebSocketTicket>>(result)
        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertEquals(
            setOf("connection_role", "client_instance_id", "observer_contract"),
            body.keys,
        )
        assertEquals(2, body.getValue("observer_contract").jsonPrimitive.content.toInt())
    }

    @Test
    fun `scoped observer websocket ticket fails closed on a v1 response echo`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"ticket":"observer-ticket-secret","ttl_seconds":30,"connection_role":"observer","observer_contract":1}""",
                ),
        )

        val result = client.mintWebSocketTicket(
            endpoint = endpoint,
            accessToken = "access-secret",
            request = ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.OBSERVER,
                clientInstanceId = ClientInstanceId("33333333-3333-4333-8333-333333333333"),
            ),
        )

        assertIs<NativeAuthResult.InvalidResponse>(result)
    }

    @Test
    fun `control websocket ticket request requires immutable session and profile`() {
        assertFailsWith<IllegalArgumentException> {
            ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.CONTROL,
                clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
                profile = "default",
            )
        }
        assertFailsWith<IllegalArgumentException> {
            ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.CONTROL,
                clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
                sessionKey = SessionKey("durable-root-1"),
            )
        }
        assertFailsWith<IllegalArgumentException> {
            ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.CONTROL,
                clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
                sessionKey = SessionKey(" durable-root-1 "),
                profile = "default",
            )
        }
    }

    @Test
    fun `observer websocket ticket request rejects control target claims`() {
        val clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111")
        assertFailsWith<IllegalArgumentException> {
            ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.OBSERVER,
                clientInstanceId = clientInstanceId,
                sessionKey = SessionKey("durable-root-1"),
            )
        }
        assertFailsWith<IllegalArgumentException> {
            ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.OBSERVER,
                clientInstanceId = clientInstanceId,
                profile = "default",
            )
        }
        assertFailsWith<IllegalArgumentException> {
            ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.OBSERVER,
                clientInstanceId = clientInstanceId,
                sessionKey = SessionKey("durable-root-1"),
                profile = "default",
            )
        }
    }

    @Test
    fun `scoped websocket ticket rejects a response echoing another role`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"ticket":"ticket-secret","ttl_seconds":30,"connection_role":"observer"}""",
                ),
        )

        val result = client.mintWebSocketTicket(
            endpoint = endpoint,
            accessToken = "access-secret",
            request = ScopedWebSocketTicketRequest(
                connectionRole = GatewayConnectionRole.CONTROL,
                clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
                sessionKey = SessionKey("durable-root-1"),
                profile = "default",
            ),
        )

        assertIs<NativeAuthResult.InvalidResponse>(result)
    }

    @Test
    fun `HTTP failure does not expose response payload`() = runTest {
        server.enqueue(MockResponse().setResponseCode(401).setBody("credential diagnostic"))

        val result = client.exchangeCode(endpoint, "bad-code", "v".repeat(43))

        val failure = assertIs<NativeAuthResult.HttpFailure>(result)
        assertEquals(401, failure.statusCode)
        assertFalse(failure.summary.contains("credential diagnostic"))
    }
}
