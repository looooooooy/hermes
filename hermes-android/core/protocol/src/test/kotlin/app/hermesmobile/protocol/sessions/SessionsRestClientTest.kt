package app.hermesmobile.protocol.sessions

import app.hermesmobile.protocol.GatewayEndpoint
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNull

class SessionsRestClientTest {
    private lateinit var server: MockWebServer
    private lateinit var endpoint: GatewayEndpoint
    private lateinit var client: SessionsRestClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        endpoint = GatewayEndpoint.parse(server.url("/ingress/").toString()).getOrThrow()
        client = SessionsRestClient(OkHttpClient())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `session list projects compression tip onto a stable session key`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """
                    {
                      "sessions": [{
                        "id": "tip-2",
                        "_lineage_root_id": "root-1",
                        "title": "Mobile client",
                        "preview": "Continue the Android app",
                        "source": "desktop",
                        "model": "gpt-test",
                        "started_at": 100.0,
                        "ended_at": null,
                        "last_active": 120.0,
                        "message_count": 7,
                        "tool_call_count": 2,
                        "input_tokens": 30,
                        "output_tokens": 40,
                        "is_active": true,
                        "archived": false,
                        "future_field": {"ignored": true}
                      }],
                      "total": 1,
                      "limit": 20,
                      "offset": 0
                    }
                    """.trimIndent(),
                ),
        )

        val result = client.listSessions(endpoint, accessToken = "access-token")

        val page = assertIs<SessionsResult.Success<SessionPage>>(result).value
        assertEquals(1, page.total)
        val session = page.sessions.single()
        assertEquals(SessionKey("root-1"), session.sessionKey)
        assertEquals(SessionKey("root-1"), session.lineageRoot)
        assertEquals(SessionKey("tip-2"), session.lineageTip)
        assertEquals("Mobile client", session.title)
        assertEquals(7, session.messageCount)

        val request = server.takeRequest()
        assertEquals("/ingress/api/sessions?limit=20&offset=0&min_messages=1&archived=exclude&order=recent", request.path)
        assertEquals("Bearer access-token", request.getHeader("Authorization"))
    }

    @Test
    fun `transcript resolves server lineage and preserves structured message content`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """
                    {
                      "session_id": "tip-2",
                      "messages": [
                        {"id": 11, "role": "user", "content": "hello", "timestamp": 100.0},
                        {
                          "id": 12,
                          "role": "assistant",
                          "content": [{"type": "text", "text": "world"}],
                          "reasoning": "checked",
                          "tool_calls": [{"id": "call-1"}],
                          "timestamp": 101.0
                        }
                      ],
                      "pagination": {"limit": 200, "offset": 0, "returned": 2}
                    }
                    """.trimIndent(),
                ),
        )

        val result = client.getMessages(
            endpoint = endpoint,
            sessionKey = SessionKey("root / 1"),
            accessToken = "access-token",
            profile = "work profile",
        )

        val transcript = assertIs<SessionsResult.Success<SessionTranscript>>(result).value
        assertEquals(SessionKey("root / 1"), transcript.sessionKey)
        assertEquals(SessionKey("tip-2"), transcript.lineageTip)
        assertEquals(2, transcript.messages.size)
        assertEquals(JsonPrimitive("hello"), transcript.messages.first().content)
        assertEquals("checked", transcript.messages.last().reasoning)
        assertEquals(2, transcript.pagination.returned)

        val request = server.takeRequest()
        assertEquals(
            "/ingress/api/sessions/root%20%2F%201/messages?limit=200&offset=0&profile=work%20profile",
            request.path,
        )
    }

    @Test
    fun `detail response remains a non-authoritative server projection`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"id":"tip-2","_lineage_root_id":"root-1","title":null,"source":"cli","started_at":100,"ended_at":110,"message_count":1}""",
                ),
        )

        val result = client.getSession(endpoint, SessionKey("root-1"), "access-token")

        val session = assertIs<SessionsResult.Success<SessionProjection>>(result).value
        assertEquals(SessionKey("root-1"), session.sessionKey)
        assertEquals(SessionKey("tip-2"), session.lineageTip)
        assertNull(session.title)
    }

    @Test
    fun `HTTP failure never exposes server response body`() = runTest {
        server.enqueue(MockResponse().setResponseCode(401).setBody("bearer token diagnostic"))

        val result = client.listSessions(endpoint, "expired-token")

        val failure = assertIs<SessionsResult.HttpFailure>(result)
        assertEquals(401, failure.statusCode)
        assertFalse(failure.summary.contains("bearer token diagnostic"))
    }

    @Test
    fun `unprotected endpoint omits authorization header`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"sessions":[],"total":0,"limit":20,"offset":0}"""),
        )

        val result = client.listSessions(endpoint, accessToken = "")

        assertIs<SessionsResult.Success<SessionPage>>(result)
        assertNull(server.takeRequest().getHeader("Authorization"))
    }
}
