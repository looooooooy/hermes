package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

class JsonRpcCodecTest {
    private val codec = JsonRpcCodec()

    @Test
    fun `encodes Hermes JSON RPC request envelope`() {
        val encoded = codec.encodeRequest(
            id = 42,
            method = "session.resume",
            params = buildJsonObject {
                put("session_id", "stored-1")
                put("cols", 80)
            },
        )

        assertEquals(
            "{\"jsonrpc\":\"2.0\",\"id\":42,\"method\":\"session.resume\",\"params\":{\"session_id\":\"stored-1\",\"cols\":80}}",
            encoded,
        )
    }

    @Test
    fun `decodes gateway event with optional runtime session id`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"runtime-1","payload":{"text":"hello"}}}""",
        )

        val event = assertIs<JsonRpcInbound.Event>(frame).event
        assertEquals("message.delta", event.type)
        assertEquals(RuntimeSessionId("runtime-1"), event.runtimeSessionId)
        assertNull(event.sessionKey)
        assertNull(event.eventSequence)
        assertEquals("hello", event.payload?.get("text")?.toString()?.trim('"'))
    }

    @Test
    fun `decodes durable session key and positive event sequence`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"event","params":{"type":"tool.result","session_id":"runtime-1","session_key":"durable-1","event_sequence":17,"payload":{"output":"private body"}}}""",
        )

        val event = assertIs<JsonRpcInbound.Event>(frame).event
        assertEquals(SessionKey("durable-1"), event.sessionKey)
        assertEquals(17L, event.eventSequence)
        assertEquals(setOf("output"), event.payload?.keys)
    }

    @Test
    fun `decodes exact v2 observer metadata with the full event authority`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"event","params":{"observer_contract":2,"profile":"default","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"durable-1","event_sequence":2,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Run tests","status":"completed"}]}}}""",
        )

        val event = assertIs<JsonRpcInbound.Event>(frame).event
        assertEquals(2, event.observerContractVersion)
        assertEquals("default", event.profile)
        assertEquals("generation-1", event.runtimeGeneration)
        assertEquals(2L, event.eventSequenceStart)
    }

    @Test
    fun `rejects blank durable session key`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_key":" ","payload":{}}}""",
        )

        assertIs<JsonRpcInbound.Invalid>(frame)
    }

    @Test
    fun `rejects event sequence when it is not positive`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","event_sequence":0,"payload":{}}}""",
        )

        assertIs<JsonRpcInbound.Invalid>(frame)
    }

    @Test
    fun `gateway ready is valid without a session id`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"skin":"hermes"}}}""",
        )

        val event = assertIs<JsonRpcInbound.Event>(frame).event
        assertEquals("gateway.ready", event.type)
        assertNull(event.runtimeSessionId)
    }

    @Test
    fun `decodes correlated result and structured error`() {
        val result = codec.decode("""{"jsonrpc":"2.0","id":7,"result":{"status":"streaming"}}""")
        val error = codec.decode("""{"jsonrpc":"2.0","id":8,"error":{"code":4009,"message":"session busy","data":{"retry":false}}}""")

        assertEquals(7, assertIs<JsonRpcInbound.Result>(result).id)
        val rpcError = assertIs<JsonRpcInbound.Error>(error)
        assertEquals(8, rpcError.id)
        assertEquals(4009, rpcError.error.code)
        assertEquals("session busy", rpcError.error.message)
    }

    @Test
    fun `unknown notifications remain forward compatible`() {
        val frame = codec.decode(
            """{"jsonrpc":"2.0","method":"future.notification","params":{"value":1}}""",
        )

        val notification = assertIs<JsonRpcInbound.Notification>(frame)
        assertEquals("future.notification", notification.method)
    }

    @Test
    fun `invalid or oversized documents are rejected without retaining source text`() {
        assertIs<JsonRpcInbound.Invalid>(codec.decode("not-json"))
        assertIs<JsonRpcInbound.Invalid>(codec.decode(" ".repeat(JsonRpcCodec.MAX_FRAME_CHARS + 1)))
    }

    @Test
    fun `rejects a JSON string larger than 128 KiB in UTF-8`() {
        val text = "中".repeat(44_000)
        val document =
            """{"jsonrpc":"2.0","method":"notice","params":{"text":"$text"}}"""
        assertTrue(document.encodeToByteArray().size < JsonRpcCodec.MAX_FRAME_BYTES)
        assertTrue(text.encodeToByteArray().size > 128 * 1024)

        assertIs<JsonRpcInbound.Invalid>(codec.decode(document))
    }

    @Test
    fun `rejects JSON nesting deeper than 32 levels`() {
        val nested = (1..40).fold("0") { value, _ -> "[$value]" }

        assertIs<JsonRpcInbound.Invalid>(
            codec.decode(
                """{"jsonrpc":"2.0","method":"notice","params":{"value":$nested}}""",
            ),
        )
    }

    @Test
    fun `rejects an object with more than 1024 fields`() {
        val fields = (0..1024).joinToString(",") { index -> "\"f$index\":$index" }

        assertIs<JsonRpcInbound.Invalid>(
            codec.decode(
                """{"jsonrpc":"2.0","method":"notice","params":{"value":{$fields}}}""",
            ),
        )
    }

    @Test
    fun `rejects an array with more than 1024 items`() {
        val values = (0..1024).joinToString(",")

        assertIs<JsonRpcInbound.Invalid>(
            codec.decode(
                """{"jsonrpc":"2.0","method":"notice","params":{"value":[$values]}}""",
            ),
        )
    }
}
