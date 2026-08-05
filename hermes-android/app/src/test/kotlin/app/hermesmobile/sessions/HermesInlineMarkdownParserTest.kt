package app.hermesmobile.sessions

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.LinkAnnotation
import kotlin.test.Test
import kotlin.test.assertEquals

class HermesInlineMarkdownParserTest {
    @Test
    fun `emphasis code and safe links become semantic inline spans`() {
        val spans = HermesInlineMarkdownParser.parse(
            "Use **bold**, *care*, `pwd`, and [docs](https://example.com).",
        )

        assertEquals(
            listOf(
                HermesInlineSpan.Text("Use "),
                HermesInlineSpan.Bold("bold"),
                HermesInlineSpan.Text(", "),
                HermesInlineSpan.Italic("care"),
                HermesInlineSpan.Text(", "),
                HermesInlineSpan.Code("pwd"),
                HermesInlineSpan.Text(", and "),
                HermesInlineSpan.Link("docs", "https://example.com"),
                HermesInlineSpan.Text("."),
            ),
            spans,
        )
    }

    @Test
    fun `unsafe link scheme remains plain text`() {
        val spans = HermesInlineMarkdownParser.parse(
            "Open [unsafe](javascript:alert(1)) only if safe.",
        )

        assertEquals(
            listOf(
                HermesInlineSpan.Text("Open "),
                HermesInlineSpan.Text("unsafe"),
                HermesInlineSpan.Text(" only if safe."),
            ),
            spans,
        )
    }

    @Test
    fun `intraword underscores remain literal text`() {
        assertEquals(
            listOf(HermesInlineSpan.Text("snake_case_name")),
            HermesInlineMarkdownParser.parse("snake_case_name"),
        )
    }

    @Test
    fun `dunder identifiers remain literal text`() {
        assertEquals(
            listOf(HermesInlineSpan.Text("__init__ __name__")),
            HermesInlineMarkdownParser.parse("__init__ __name__"),
        )
    }

    @Test
    fun `safe links require normalized credential-free destinations`() {
        assertEquals(
            "https://example.com/b",
            HermesSafeLinkPolicy.normalizeOrNull("https://example.com/a/../b"),
        )
        assertEquals(
            "mailto:person@example.com?subject=Hello",
            HermesSafeLinkPolicy.normalizeOrNull("mailto:person@example.com?subject=Hello"),
        )
        listOf(
            "javascript:alert(1)",
            "https:relative",
            "https:///missing-host",
            "https://user:password@example.com/private",
            "https://example.com/path with spaces",
            "https://example.com/\u0007bell",
            "https://example.com/?access_token=secret",
            "https://example.com/?access%5Ftoken=secret",
            "https://example.com/?access%255Ftoken=secret",
            "https://example.com/?access%2525255Ftoken=secret",
            "https://example.com/?acc%25252565ss_token=secret",
            "https://example.com/?acc%25252565ss_tok%25252565n=secret",
            "https://example.com/#access_token=secret",
            "https://example.com/#route?access%255Ftoken=secret",
            "https://example.com/?safe%250Akey=value",
            "https://example.com/?auth_token_v2=secret",
            "https://example.com/?approval_ticket_id=secret",
            "https://example.com/?control-lease=secret",
            "https://example.com/?clientSecretValue=secret",
            "https://example.com/?db.password=secret",
            "mailto:not-an-address",
            "mailto:person@example.com?access_token=secret",
        ).forEach { unsafe ->
            assertEquals(null, HermesSafeLinkPolicy.normalizeOrNull(unsafe), unsafe)
        }
    }

    @Test
    fun `unsafe credential-bearing links remain non-clickable text`() {
        assertEquals(
            listOf(HermesInlineSpan.Text("open")),
            HermesInlineMarkdownParser.parse(
                "[open](https://user:password@example.com/?access_token=secret)",
            ),
        )
        assertEquals(
            listOf(HermesInlineSpan.Text("fragment")),
            HermesInlineMarkdownParser.parse(
                "[fragment](https://example.com/#access%255Ftoken=secret)",
            ),
        )
    }

    @Test
    fun `unsafe destinations never reach annotated string link sink`() {
        val unsafe = buildHermesInlineAnnotatedString(
            text = "[open](https://example.com/#access%255Ftoken=secret)",
            linkColor = Color.Blue,
            codeBackground = Color.Gray,
            highlightBackground = Color.Yellow,
            mathColor = Color.Blue,
        )
        val safe = buildHermesInlineAnnotatedString(
            text = "[docs](https://example.com/docs)",
            linkColor = Color.Blue,
            codeBackground = Color.Gray,
            highlightBackground = Color.Yellow,
            mathColor = Color.Blue,
        )

        assertEquals(emptyList(), unsafe.getLinkAnnotations(0, unsafe.length))
        assertEquals(
            "https://example.com/docs",
            (safe.getLinkAnnotations(0, safe.length).single().item as LinkAnnotation.Url).url,
        )
    }

    @Test
    fun `strike delimiters become a typed inline span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("before "),
                HermesInlineSpan.Strike("removed"),
                HermesInlineSpan.Text(" after"),
            ),
            HermesInlineMarkdownParser.parse("before ~~removed~~ after"),
        )
    }

    @Test
    fun `highlight delimiters become a typed inline span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("read "),
                HermesInlineSpan.Highlight("this"),
                HermesInlineSpan.Text(" first"),
            ),
            HermesInlineMarkdownParser.parse("read ==this== first"),
        )
    }

    @Test
    fun `dollar delimiters become a typed inline math span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("solve "),
                HermesInlineSpan.Math("x^2 + y^2"),
                HermesInlineSpan.Text(" now"),
            ),
            HermesInlineMarkdownParser.parse("solve \$x^2 + y^2\$ now"),
        )
    }

    @Test
    fun `backslash parenthesis delimiters become a typed inline math span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("derive "),
                HermesInlineSpan.Math("x + y"),
                HermesInlineSpan.Text(" later"),
            ),
            HermesInlineMarkdownParser.parse("derive \\(x + y\\) later"),
        )
    }

    @Test
    fun `safe angle autolink becomes a normalized link span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("Visit "),
                HermesInlineSpan.Link(
                    label = "https://example.com/a/../docs",
                    url = "https://example.com/docs",
                ),
                HermesInlineSpan.Text(" now"),
            ),
            HermesInlineMarkdownParser.parse(
                "Visit <https://example.com/a/../docs> now",
            ),
        )
    }

    @Test
    fun `angle email becomes a safe mailto link span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("Email "),
                HermesInlineSpan.Link(
                    label = "person@example.com",
                    url = "mailto:person@example.com",
                ),
                HermesInlineSpan.Text(" today"),
            ),
            HermesInlineMarkdownParser.parse("Email <person@example.com> today"),
        )
    }

    @Test
    fun `footnote reference becomes a typed inline span`() {
        assertEquals(
            listOf(
                HermesInlineSpan.Text("Claim"),
                HermesInlineSpan.FootnoteReference("source-1"),
                HermesInlineSpan.Text(" continues"),
            ),
            HermesInlineMarkdownParser.parse("Claim[^source-1] continues"),
        )
    }
}
