package app.hermesmobile.protocol.gateway

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue

class CloudObserverEventContractV2Test {
    @Test
    fun `production allowlist stays synchronized with generated v2 authority`() {
        val authority = requireNotNull(
            javaClass.classLoader.getResourceAsStream("contracts/observer-output-parity-v2.json"),
        ).bufferedReader().use { reader ->
            Json.parseToJsonElement(reader.readText()).jsonObject
        }
        val generatedEventTypes = authority.getValue("event_types")
            .jsonArray
            .map { it.jsonPrimitive.content }
            .toSet()

        assertEquals(generatedEventTypes, CloudObserverEventContract.eventTypes)

        val realtime = requireNotNull(
            javaClass.classLoader.getResourceAsStream("contracts/cloud-realtime-v2.json"),
        ).bufferedReader().use { reader ->
            Json.parseToJsonElement(reader.readText()).jsonObject
        }
        assertEquals(
            listOf("schemas/cloud/payloads/session-event-v2.schema.json"),
            realtime.getValue("schema_dependencies").jsonArray.map { it.jsonPrimitive.content },
        )
        val eventSchema = requireNotNull(
            javaClass.classLoader.getResourceAsStream(
                "contracts/schemas/cloud/payloads/session-event-v2.schema.json",
            ),
        ).bufferedReader().use { reader ->
            Json.parseToJsonElement(reader.readText()).jsonObject
        }
        assertEquals(
            generatedEventTypes,
            eventSchema.getValue("properties")
                .jsonObject
                .getValue("type")
                .jsonObject
                .getValue("enum")
                .jsonArray
                .map { it.jsonPrimitive.content }
                .toSet(),
        )
    }

    @Test
    fun `v2 lifecycle payloads accept display-safe fields and reject raw values`() {
        val safeTool = payload(
            """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"running","name":"Tests","call_label":"Run focused tests"}""",
        )
        assertTrue(CloudObserverEventContract.accepts("tool.update", safeTool))

        listOf(
            "raw_args" to "{\"command\":\"secret\"}",
            "output" to "\"full output\"",
            "approval_payload" to "{\"secret\":\"value\"}",
            "token" to "\"credential\"",
        ).forEach { (field, value) ->
            val unsafe = JsonObject(safeTool + (field to Json.parseToJsonElement(value)))
            assertFalse(CloudObserverEventContract.accepts("tool.update", unsafe))
        }
    }

    @Test
    fun `v2 todo requires unique stable items and subagent progress is bounded`() {
        val duplicateTodo = payload(
            """{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"same","label":"One","status":"pending"},{"id":"same","label":"Two","status":"pending"}]}""",
        )
        val invalidProgress = payload(
            """{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"","summary":null,"status":"running","progress":{"current":4,"total":3}}""",
        )

        assertFalse(CloudObserverEventContract.accepts("todo.update", duplicateTodo))
        assertFalse(CloudObserverEventContract.accepts("subagent.update", invalidProgress))
    }

    @Test
    fun `v2 transport digest is canonical across object member order`() {
        val first = payload(
            """{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"todo.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"移动端","status":"pending"}]}}""",
        )
        val reordered = payload(
            """{"payload":{"items":[{"status":"pending","label":"移动端","id":"item-1"}],"status":"in_progress","operation":"upsert","first_event_sequence":1,"revision":1,"section_id":"todo-1","turn_id":"turn-1"},"event_sequence":1,"session_key":"stored-1","session_id":"runtime-1","type":"todo.update","runtime_generation":"generation-1","profile":"work","observer_contract":2}""",
        )
        val changed = JsonObject(
            first + (
                "payload" to JsonObject(
                    first.getValue("payload").jsonObject +
                        (
                            "items" to Json.parseToJsonElement(
                                """[{"id":"item-1","label":"Changed","status":"pending"}]""",
                            )
                        ),
                )
                ),
        )

        val firstDigest = requireNotNull(
            CloudObserverEventContract.decodeV2SessionEvent(first),
        ).transportDigest
        val reorderedDigest = requireNotNull(
            CloudObserverEventContract.decodeV2SessionEvent(reordered),
        ).transportDigest
        val changedDigest = requireNotNull(
            CloudObserverEventContract.decodeV2SessionEvent(changed),
        ).transportDigest

        assertTrue(requireNotNull(firstDigest).matches(Regex("^[0-9a-f]{64}$")))
        assertEquals(firstDigest, reorderedDigest)
        assertNotEquals(firstDigest, changedDigest)
    }

    @Test
    fun `v2 integers accept max safe integer and reject max plus one`() {
        val maximum = 9_007_199_254_740_991L
        val overMaximum = maximum + 1
        val validPayload = payload(
            """{"turn_id":"turn-1","subagent_id":"agent-1","revision":$maximum,"first_event_sequence":$maximum,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"","summary":null,"status":"running","duration_ms":$maximum,"progress":{"current":$maximum,"total":$maximum},"token_counts":{"input":$maximum,"output":$maximum,"reasoning":$maximum},"api_calls":$maximum}""",
        )
        assertTrue(CloudObserverEventContract.accepts("subagent.update", validPayload))

        listOf("revision", "first_event_sequence", "duration_ms", "api_calls").forEach { field ->
            assertFalse(
                CloudObserverEventContract.accepts(
                    "subagent.update",
                    JsonObject(validPayload + (field to Json.parseToJsonElement(overMaximum.toString()))),
                ),
                field,
            )
        }
        listOf("current", "total").forEach { field ->
            val progress = validPayload.getValue("progress").jsonObject
            val invalid = JsonObject(
                validPayload + (
                    "progress" to JsonObject(
                        progress + (field to Json.parseToJsonElement(overMaximum.toString())),
                    )
                    ),
            )
            assertFalse(CloudObserverEventContract.accepts("subagent.update", invalid), field)
        }
        listOf("input", "output", "reasoning").forEach { field ->
            val counts = validPayload.getValue("token_counts").jsonObject
            val invalid = JsonObject(
                validPayload + (
                    "token_counts" to JsonObject(
                        counts + (field to Json.parseToJsonElement(overMaximum.toString())),
                    )
                    ),
            )
            assertFalse(CloudObserverEventContract.accepts("subagent.update", invalid), field)
        }

        fun event(sequence: Long): JsonObject = payload(
            """{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"subagent.update","session_id":"runtime-1","session_key":"stored-1","event_sequence":$sequence,"payload":$validPayload}""",
        )
        assertTrue(CloudObserverEventContract.decodeV2SessionEvent(event(maximum)) != null)
        assertTrue(CloudObserverEventContract.decodeV2SessionEvent(event(overMaximum)) == null)
    }

    @Test
    fun `v2 extensions recursively reject sensitive content and retain display-safe metadata`() {
        val base = payload(
            """{"observer_contract":2,"profile":"work","runtime_generation":"generation-1","type":"message.start","session_id":"runtime-1","session_key":"stored-1","event_sequence":1,"payload":{}}""",
        )
        val safe = JsonObject(
            base + (
                "extensions" to payload(
                    """{"com.example.mobile":{"label":"Build 7","metrics":{"count":3},"tags":["safe",true],"notes":["Basic authentication is disabled.","Basic YWJjZA== is not a user-password credential.","a.b.c","Release 1.2.3","docs.example.com","tokenizer pathology"]}}""",
                )
                ),
        )
        assertTrue(CloudObserverEventContract.decodeV2SessionEvent(safe) != null)

        listOf(
            """{"com.example.mobile":{"client_secret":"hidden"}}""",
            """{"com.example.mobile":{"nested":{"api_token":"hidden"}}}""",
            """{"com.example.mobile":{"items":[{"tool_args":{"command":"hidden"}}]}}""",
            """{"com.example.mobile":{"label":"Bearer hidden"}}""",
            """{"com.example.mobile":{"label":"Basic dXNlcjpwYXNz"}}""",
            """{"com.example.mobile":{"label":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln"}}""",
            """{"com.example.mobile":{"label":"client_secret = hidden"}}""",
            """{"com.example.mobile":{"label":"AKIAIOSFODNN7EXAMPLE"}}""",
            """{"com.example.mobile":{"label":"AIzaSyD-ExampleProviderCredential1234567"}}""",
            """{"com.example.mobile":{"label":"sk-ant-api03-example"}}""",
            """{"com.example.mobile":{"label":"sk_live_example"}}""",
            """{"com.example.mobile":{"label":"hf_example"}}""",
            """{"com.example.mobile":{"nested":{"approval_payload":{"allow":true}}}}""",
        ).forEach { document ->
            val unsafe = JsonObject(base + ("extensions" to payload(document)))
            assertTrue(
                CloudObserverEventContract.decodeV2SessionEvent(unsafe) == null,
                document,
            )
        }
    }

    @Test
    fun `v2 display text recognizes semantic Basic and JWT credentials without prose false positives`() {
        listOf(
            "Basic dXNlcjpwYXNz",
            "Basic dXNlcjo=",
            "Authorization: Basic dXNlcjpwYXNz",
            "password=hunter2",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        ).forEach { credential ->
            assertFalse(
                CloudObserverEventContract.accepts(
                    2,
                    "message.delta",
                    payload("""{"text":"$credential"}"""),
                ),
                credential,
            )
        }

        listOf(
            "Basic authentication is disabled.",
            "Basic YWJjZA== is not a user-password credential.",
            "Basic OnBhc3M= has no nonempty user.",
            "Basic abcde is not valid base64.",
            "a.b.c",
            "Release 1.2.3 is ready on docs.example.com.",
            "The tokenizer handles pathology.",
            "eyJhbGciOiIifQ.eyJzdWIiOiIxIn0.c2ln",
            "eyJub3QiOiJoZWFkZXIifQ.eyJzdWIiOiIxIn0.c2ln",
            "eyJhbGciOiJIUzI1NiJ9.WzFd.c2ln",
        ).forEach { displayText ->
            assertTrue(
                CloudObserverEventContract.accepts(
                    2,
                    "message.delta",
                    payload("""{"text":"$displayText"}"""),
                ),
                displayText,
            )
        }
    }

    @Test
    fun `v2 display text rejects credentials across stream and lifecycle payloads`() {
        val unsafePayloads = listOf(
            "message.delta" to
                """{"text":"Authorization: Basic dXNlcjpwYXNz"}""",
            "message.complete" to
                """{"status":"complete","text":"password = hunter2"}""",
            "subagent.update" to
                """{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"Run checks","summary":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln","status":"running"}""",
            "tool.update" to
                """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"running","name":"Tests","summary":"AKIAIOSFODNN7EXAMPLE"}""",
            "terminal.update" to
                """{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"running","summary":"AIzaSyD-ExampleProviderCredential1234567"}""",
        )

        unsafePayloads.forEach { (type, document) ->
            assertFalse(
                CloudObserverEventContract.accepts(2, type, payload(document)),
                "$type accepted display text containing a credential",
            )
        }
    }

    @Test
    fun `v2 display text avoids vocabulary false positives and keeps aggregate token counts bounded`() {
        assertTrue(
            CloudObserverEventContract.accepts(
                2,
                "message.delta",
                payload(
                    """{"text":"The tokenizer handles pathology while token counts remain aggregate."}""",
                ),
            ),
        )
        val safeSubagent = payload(
            """{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"Review tokenizer pathology","summary":"Token counts are aggregate only","status":"running","token_counts":{"input":0,"output":12,"reasoning":3}}""",
        )
        assertTrue(CloudObserverEventContract.accepts(2, "subagent.update", safeSubagent))

        val negativeCounts = JsonObject(
            safeSubagent + (
                "token_counts" to payload(
                    """{"input":-1,"output":12,"reasoning":3}""",
                )
                ),
        )
        assertFalse(CloudObserverEventContract.accepts(2, "subagent.update", negativeCounts))
    }

    private fun payload(document: String): JsonObject =
        Json.parseToJsonElement(document).jsonObject
}
