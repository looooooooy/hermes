package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.GatewayEvent
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class CloudObserverEventPolicyTest {
    @Test
    fun `only frozen Cloud P0 observer events are accepted`() {
        listOf(
            "message.start",
            "message.delta",
            "message.complete",
            "agent.terminal.output",
            "reasoning.delta",
            "status.update",
            "thinking.delta",
            "tool.output.delta",
        ).forEach { type ->
            assertTrue(CloudObserverEventPolicy.accepts(type), type)
        }

        assertFalse(CloudObserverEventPolicy.accepts("tool.start"))
        assertFalse(CloudObserverEventPolicy.accepts("subagent.started"))
        assertFalse(CloudObserverEventPolicy.accepts("unknown.event"))
    }

    @Test
    fun `live observer payload must match the frozen event schema`() {
        assertTrue(
            CloudObserverEventPolicy.accepts(
                GatewayEvent(
                    type = "message.delta",
                    runtimeSessionId = null,
                    payload = buildJsonObject { put("text", "hello") },
                ),
            ),
        )
        assertFalse(
            CloudObserverEventPolicy.accepts(
                GatewayEvent(
                    type = "message.delta",
                    runtimeSessionId = null,
                    payload = buildJsonObject {},
                ),
            ),
        )
        assertFalse(
            CloudObserverEventPolicy.accepts(
                GatewayEvent(
                    type = "status.update",
                    runtimeSessionId = null,
                    payload = buildJsonObject {
                        put("status", "working")
                        put("running", false)
                    },
                ),
            ),
        )
    }
}
