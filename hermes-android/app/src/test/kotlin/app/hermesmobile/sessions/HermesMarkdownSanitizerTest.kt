package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals

class HermesMarkdownSanitizerTest {
    @Test
    fun `CSI styling is removed while visible text remains`() {
        assertEquals(
            "before red after",
            HermesMarkdownSanitizer.sanitize("before \u001B[31mred\u001B[0m after"),
        )
    }

    @Test
    fun `OSC 8 metadata is removed while linked text remains`() {
        assertEquals(
            "before docs after",
            HermesMarkdownSanitizer.sanitize(
                "before \u001B]8;;https://example.com\u001B\\docs\u001B]8;;\u001B\\ after",
            ),
        )
    }

    @Test
    fun `BEL controls and unterminated OSC payloads are removed`() {
        assertEquals(
            "beforeafter",
            HermesMarkdownSanitizer.sanitize("before\u0007after"),
        )
        assertEquals(
            "before ",
            HermesMarkdownSanitizer.sanitize("before \u001B]8;;https://unsafe.example"),
        )
    }

    @Test
    fun `all terminal control string payloads are removed for 7 bit and C1 forms`() {
        listOf(
            "\u001BP" to "\u001B\\",
            "\u001B_" to "\u001B\\",
            "\u001B^" to "\u001B\\",
            "\u001BX" to "\u001B\\",
            "\u0090" to "\u009C",
            "\u009F" to "\u009C",
            "\u009E" to "\u009C",
            "\u0098" to "\u009C",
        ).forEach { (introducer, terminator) ->
            assertEquals(
                "before  after",
                HermesMarkdownSanitizer.sanitize(
                    "before $introducer[hidden](https://example.com)$terminator after",
                ),
                "introducer=${introducer.codePoints().toArray().contentToString()}",
            )
        }
        assertEquals(
            "before red after",
            HermesMarkdownSanitizer.sanitize("before \u009B31mred\u009B0m after"),
        )
    }

    @Test
    fun `terminal control introducers restart from escape and CSI states`() {
        listOf(
            "\u001B\u001B]8;;" to "\u0007",
            "\u001B[31\u001B]8;;" to "\u0007",
            "\u001B\u009D8;;" to "\u009C",
            "\u001B[31\u009D8;;" to "\u009C",
            "\u001B\u001BP" to "\u001B\\",
            "\u001B[31\u0090" to "\u009C",
        ).forEach { (introducer, terminator) ->
            assertEquals(
                "before  after",
                HermesMarkdownSanitizer.sanitize(
                    "before $introducer[hidden](https://example.com)$terminator after",
                ),
                "introducer=${introducer.codePoints().toArray().contentToString()}",
            )
        }
    }
}
