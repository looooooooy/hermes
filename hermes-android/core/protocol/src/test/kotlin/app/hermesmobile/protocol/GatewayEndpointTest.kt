package app.hermesmobile.protocol

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class GatewayEndpointTest {
    @Test
    fun `host without a scheme defaults to https`() {
        val endpoint = GatewayEndpoint.parse(" hermes.example.com ").getOrThrow()

        assertEquals("https://hermes.example.com/", endpoint.baseUrl.toString())
        assertEquals("https://hermes.example.com/api/status", endpoint.statusUrl.toString())
        assertEquals("wss://hermes.example.com/api/ws", endpoint.webSocketUrl)
    }

    @Test
    fun `reverse proxy path prefix is retained`() {
        val endpoint = GatewayEndpoint.parse("https://example.com/hermes/").getOrThrow()

        assertEquals("https://example.com/hermes/", endpoint.baseUrl.toString())
        assertEquals("https://example.com/hermes/api/status", endpoint.statusUrl.toString())
        assertEquals("https://example.com/hermes/v1/capabilities", endpoint.capabilitiesUrl.toString())
        assertEquals("wss://example.com/hermes/api/ws", endpoint.webSocketUrl)
    }

    @Test
    fun `local development http endpoints are allowed`() {
        val endpoint = GatewayEndpoint.parse("http://10.0.2.2:9119").getOrThrow()

        assertEquals("http://10.0.2.2:9119/", endpoint.baseUrl.toString())
        assertEquals("ws://10.0.2.2:9119/api/ws", endpoint.webSocketUrl)
    }

    @Test
    fun `remote plain http endpoint is rejected`() {
        val result = GatewayEndpoint.parse("http://hermes.example.com")

        assertTrue(result.isFailure)
        assertTrue(result.exceptionOrNull()?.message.orEmpty().contains("HTTPS"))
    }

    @Test
    fun `credentials query and fragment are rejected`() {
        val unsafeInputs = listOf(
            "https://user:password@example.com",
            "https://example.com?token=secret",
            "https://example.com/#fragment",
        )

        unsafeInputs.forEach { input ->
            assertTrue(GatewayEndpoint.parse(input).isFailure, input)
        }
    }
}
