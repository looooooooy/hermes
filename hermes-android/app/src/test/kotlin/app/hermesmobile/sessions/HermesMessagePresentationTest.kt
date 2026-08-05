package app.hermesmobile.sessions

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class HermesMessagePresentationTest {
    @Test
    fun `terminal payload becomes a readable call and labeled result details`() {
        val call = HermesMessagePresentation.toolCall(
            name = "terminal",
            arguments = """{"command":"pwd","workdir":"/workspace"}""",
        )
        val result = HermesMessagePresentation.payload(
            buildJsonObject {
                put("output", "/workspace\n")
                put("exit_code", 0)
                put(
                    "metadata",
                    buildJsonObject {
                        put("duration_ms", 12)
                    },
                )
            },
        )

        assertEquals("Terminal(\"pwd\")", call.label)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Command", "pwd"),
                ConversationToolDetailUiModel("Workdir", "/workspace"),
            ),
            call.details,
        )
        assertEquals("/workspace\n", result.text)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Exit code", "0"),
                ConversationToolDetailUiModel("Metadata · Duration ms", "12"),
            ),
            result.details,
        )
        assertFalse(call.visibleText().contains('{'))
        assertFalse(result.visibleText().contains('{'))
    }

    @Test
    fun `full tool output stays available after sanitization beyond the compact presentation limit`() {
        val output = "begin\n" + "x".repeat(9_000) + "\npassword=tool-secret\nend"

        val presentation = HermesMessagePresentation.toolOutput(output)
        val safeOutput = HermesMessagePresentation.safeText(
            output,
            maxCodePoints = HermesMessagePresentation.MAX_LONG_OUTPUT_CODE_POINTS,
        )

        assertEquals(safeOutput, presentation.text)
        assertTrue(presentation.text.length > 8_000)
        assertTrue(presentation.text.endsWith("password=[redacted]\nend"), presentation.text.takeLast(80))
        assertFalse(presentation.text.contains("tool-secret"))
    }

    @Test
    fun `unknown nested payload becomes readable rows and redacts secrets`() {
        val presentation = HermesMessagePresentation.payload(
            buildJsonObject {
                put("status", "complete")
                put(
                    "items",
                    kotlinx.serialization.json.buildJsonArray {
                        add(
                            buildJsonObject {
                                put("file", "SessionBrowserScreen.kt")
                                put("line", 481)
                            },
                        )
                    },
                )
                put("access_token", "must-not-render")
            },
        )

        assertEquals("", presentation.text)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Status", "complete"),
                ConversationToolDetailUiModel("Items · Item 1 · File", "SessionBrowserScreen.kt"),
                ConversationToolDetailUiModel("Items · Item 1 · Line", "481"),
                ConversationToolDetailUiModel("Access token", "[redacted]"),
            ),
            presentation.details,
        )
        assertFalse(presentation.visibleText().contains("must-not-render"))
        assertFalse(presentation.visibleText().contains('{'))
    }

    @Test
    fun `websocket tickets and controller lease identifiers are recursively redacted`() {
        val presentation = HermesMessagePresentation.payload(
            buildJsonObject {
                put("ws_ticket", "ticket-field-secret")
                put("lease_id", "lease-field-secret")
                put(
                    "nested",
                    JsonPrimitive(
                        buildJsonObject {
                            put("ticket", "nested-ticket-secret")
                            put("control_lease_id", "nested-lease-secret")
                            put(
                                "command",
                                "connect --ws-ticket cli-ticket-secret " +
                                    "--lease-id=cli-lease-secret " +
                                    "ticket=assignment-ticket-secret",
                            )
                        }.toString(),
                    ),
                )
            },
        )
        val visible = presentation.visibleText()

        assertTrue(visible.contains("[redacted]"), visible)
        listOf(
            "ticket-field-secret",
            "lease-field-secret",
            "nested-ticket-secret",
            "nested-lease-secret",
            "cli-ticket-secret",
            "cli-lease-secret",
            "assignment-ticket-secret",
        ).forEach { secret ->
            assertFalse(visible.contains(secret), "Rendered protected value")
        }
    }

    @Test
    fun `json encoded tool content is recursively presented instead of rendered raw`() {
        val presentation = HermesMessagePresentation.payload(
            JsonPrimitive("""{"output":"done","exit_code":0}"""),
        )

        assertEquals("done", presentation.text)
        assertEquals(
            listOf(ConversationToolDetailUiModel("Exit code", "0")),
            presentation.details,
        )
        assertFalse(presentation.visibleText().contains('{'))
        assertFalse(presentation.visibleText().contains("\"output\""))
    }

    @Test
    fun `double encoded tool arguments recursively redact structured and embedded credentials`() {
        val secretObject = buildJsonObject {
            put(
                "command",
                "curl 'https://example.com?access_token=query-secret' " +
                    "-H 'Authorization: " + "Bearer " + "sensitive-bearer-value'",
            )
            put("private_key", "private-secret")
            put("authentication", "authentication-sensitive-value")
            put("note", "Basic basic-sensitive-value")
            put("nested", """{"passwd":"nested-secret","safe":"visible"}""")
        }
        val doubleEncoded = JsonPrimitive(JsonPrimitive(secretObject.toString()).toString())

        val call = HermesMessagePresentation.toolCall(
            name = "terminal",
            arguments = doubleEncoded,
        )
        val visible = call.visibleText()

        assertTrue(visible.contains("[redacted]"))
        assertTrue(visible.contains("visible"), visible)
        listOf(
            "query-secret",
            "sensitive-bearer-value",
            "private-secret",
            "authentication-sensitive-value",
            "basic-sensitive-value",
            "nested-secret",
        ).forEach { secret ->
            assertFalse(visible.contains(secret), "Rendered secret: $secret")
        }
        assertFalse(visible.contains('{'))
        assertFalse(visible.contains("\"passwd\""))
    }

    @Test
    fun `tool names and embedded user credentials are sanitized before display`() {
        val call = HermesMessagePresentation.toolCall(
            name = """{"name":"terminal","access_token":"name-secret"}""",
            arguments = buildJsonObject {
                put("ssh_key", "ssh-secret")
                put("url", "https://mobile-user:url-secret@example.com/private")
                put("command", "curl -u cli-user:cli-secret https://example.com")
            },
        )
        val visible = call.visibleText()

        assertEquals("Terminal", call.label.substringBefore('('))
        assertTrue(visible.contains("[redacted]"), visible)
        listOf("name-secret", "ssh-secret", "url-secret", "cli-secret").forEach { secret ->
            assertFalse(visible.contains(secret), "Rendered secret: $secret")
        }
        assertFalse(visible.contains('{'), visible)
    }

    @Test
    fun `attached curl userinfo and authorization assignments are sanitized`() {
        val presentation = HermesMessagePresentation.payload(
            buildJsonObject {
                put("attached_user", "curl -uattached:attached-secret https://example.com")
                put("endpoint_one", "https://token-secret@example.com/private")
                put("endpoint_two", "https://:password-secret@example.com/private")
                put("status", "Authorization=Bearer assignment-secret")
            },
        )
        val visible = presentation.visibleText()

        assertTrue(visible.contains("[redacted]"), visible)
        listOf(
            "attached-secret",
            "token-secret",
            "password-secret",
            "assignment-secret",
        ).forEach { secret ->
            assertFalse(visible.contains(secret), "Rendered secret: $secret")
        }
    }

    @Test
    fun `blank structured values are not presentable content`() {
        val blankPayload = buildJsonObject {
            put("text", "")
            put("nested", buildJsonObject { put("value", "") })
        }

        val presentation = HermesMessagePresentation.payload(blankPayload)

        assertEquals("", presentation.text)
        assertEquals(emptyList(), presentation.details)
        assertEquals("", HermesMessagePresentation.readableText(blankPayload))
    }

    @Test
    fun `uri credentials and hostile property names are sanitized and bounded`() {
        val presentation = HermesMessagePresentation.payload(
            buildJsonObject {
                put("endpoint", "ssh://mobile-user:ssh-secret@example.com/private")
                put("Authorization=Bearer key-secret", "safe value")
                put("x".repeat(10_000), "bounded value")
            },
        )
        val visible = presentation.visibleText()

        assertFalse(visible.contains("ssh-secret"), visible)
        assertFalse(visible.contains("key-secret"), visible)
        assertTrue(presentation.details.all { detail -> detail.label.length <= 160 })
        assertTrue(visible.contains("[redacted]"), visible)
    }

    @Test
    fun `json strings nested inside arrays are recursively humanized`() {
        val encodedItem = JsonPrimitive(
            JsonPrimitive(
                buildJsonObject {
                    put("status", "complete")
                    put("credential", "array-secret")
                }.toString(),
            ).toString(),
        )

        val presentation = HermesMessagePresentation.payload(
            buildJsonArray { add(encodedItem) },
        )
        val visible = presentation.visibleText()

        assertTrue(visible.contains("Status: complete"))
        assertTrue(visible.contains("Credential: [redacted]"))
        assertFalse(visible.contains("array-secret"))
        assertFalse(visible.contains('{'))
    }

    @Test
    fun `presentation bounds recursive decoding detail rows and visible value length`() {
        var deeplyEncoded = JsonPrimitive("safe leaf")
        repeat(12) {
            deeplyEncoded = JsonPrimitive(
                buildJsonObject { put("nested", deeplyEncoded) }.toString(),
            )
        }
        val deepPresentation = HermesMessagePresentation.payload(deeplyEncoded)
        val widePresentation = HermesMessagePresentation.payload(
            buildJsonObject {
                repeat(500) { index -> put("field_$index", "value-$index") }
            },
        )
        val longPresentation = HermesMessagePresentation.payload(
            JsonPrimitive("x".repeat(9_000)),
        )
        val oversizedEncodedPresentation = HermesMessagePresentation.payload(
            JsonPrimitive("{\"output\":\"${"x".repeat(70_000)}\"}"),
        )

        assertFalse(deepPresentation.visibleText().contains('{'))
        assertTrue(deepPresentation.visibleText().contains("omitted"))
        assertTrue(widePresentation.details.size <= 200)
        assertTrue(longPresentation.text.length <= 8_000)
        assertTrue(oversizedEncodedPresentation.visibleText().contains("omitted"))
        assertFalse(oversizedEncodedPresentation.visibleText().contains('{'))
    }
}
