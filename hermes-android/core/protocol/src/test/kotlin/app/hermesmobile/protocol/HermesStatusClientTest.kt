package app.hermesmobile.protocol

import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

class HermesStatusClientTest {
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
    fun `discovers public Hermes status without credentials`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """
                    {
                      "version": "0.14.0",
                      "release_date": "2026-07-01",
                      "gateway_running": true,
                      "gateway_state": "running",
                      "auth_required": true,
                      "auth_providers": ["basic"],
                      "auth_flows": ["cookie", "native_pkce"],
                      "overall": "ok",
                      "future_field": {"ignored": true}
                    }
                    """.trimIndent(),
                ),
        )
        val endpoint = GatewayEndpoint.parse(server.url("/edge/").toString()).getOrThrow()

        val result = HermesStatusClient(OkHttpClient()).discover(endpoint)

        val reachable = assertIs<DiscoveryResult.Reachable>(result)
        assertEquals("0.14.0", reachable.status.version)
        assertTrue(reachable.status.gatewayRunning)
        assertTrue(reachable.status.authRequired)
        assertTrue(reachable.status.supportsNativePkce)
        assertTrue(reachable.status.supportsPassword)
        assertEquals(setOf("basic"), reachable.status.authProviders)
        assertEquals("ok", reachable.status.overall)

        val request = server.takeRequest()
        assertEquals("/edge/api/status", request.path)
        assertEquals("application/json", request.getHeader("Accept"))
        assertEquals(null, request.getHeader("Authorization"))
    }

    @Test
    fun `http failure exposes status code but never response payload`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(502)
                .setBody("upstream secret-bearing diagnostic"),
        )
        val endpoint = GatewayEndpoint.parse(server.url("/").toString()).getOrThrow()

        val result = HermesStatusClient(OkHttpClient()).discover(endpoint)

        val failure = assertIs<DiscoveryResult.HttpFailure>(result)
        assertEquals(502, failure.statusCode)
        assertFalse(failure.summary.contains("secret-bearing"))
    }

    @Test
    fun `invalid json is reported as an incompatible response`() = runTest {
        server.enqueue(MockResponse().setResponseCode(200).setBody("not-json"))
        val endpoint = GatewayEndpoint.parse(server.url("/").toString()).getOrThrow()

        val result = HermesStatusClient(OkHttpClient()).discover(endpoint)

        assertIs<DiscoveryResult.InvalidResponse>(result)
    }
}
