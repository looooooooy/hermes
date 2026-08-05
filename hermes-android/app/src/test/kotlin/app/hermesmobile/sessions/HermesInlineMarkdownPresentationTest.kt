package app.hermesmobile.sessions

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextDecoration
import kotlin.test.Test
import kotlin.test.assertEquals

class HermesInlineMarkdownPresentationTest {
    @Test
    fun `typed strike highlight and math spans retain visual semantics`() {
        val highlightColor = Color(0xFFE2DFA2)
        val mathColor = Color(0xFF245EB5)

        val annotated = buildHermesInlineAnnotatedString(
            text = "~~removed~~ ==marked== \$x\$",
            linkColor = Color.Blue,
            codeBackground = Color.LightGray,
            highlightBackground = highlightColor,
            mathColor = mathColor,
        )

        assertEquals("removed marked x", annotated.text)
        assertEquals(TextDecoration.LineThrough, annotated.spanStyles[0].item.textDecoration)
        assertEquals(0 until 7, annotated.spanStyles[0].start until annotated.spanStyles[0].end)
        assertEquals(highlightColor, annotated.spanStyles[1].item.background)
        assertEquals(8 until 14, annotated.spanStyles[1].start until annotated.spanStyles[1].end)
        assertEquals(FontFamily.Monospace, annotated.spanStyles[2].item.fontFamily)
        assertEquals(mathColor, annotated.spanStyles[2].item.color)
        assertEquals(15 until 16, annotated.spanStyles[2].start until annotated.spanStyles[2].end)
    }
}
