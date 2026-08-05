package app.hermesmobile.sessions

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.withStyle

internal fun buildHermesCodeFenceAnnotatedString(
    language: String?,
    code: String,
    additionColor: Color,
    deletionColor: Color,
    hunkColor: Color,
    fileHeaderColor: Color,
): AnnotatedString = if (language?.lowercase() in setOf("diff", "patch")) {
    buildHermesDiffAnnotatedString(
        text = code,
        additionColor = additionColor,
        deletionColor = deletionColor,
        hunkColor = hunkColor,
        fileHeaderColor = fileHeaderColor,
    )
} else {
    AnnotatedString(code)
}

internal fun buildHermesDiffAnnotatedString(
    text: String,
    additionColor: Color,
    deletionColor: Color,
    hunkColor: Color,
    fileHeaderColor: Color,
): AnnotatedString = buildAnnotatedString {
    val lines = text.split('\n')
    lines.forEachIndexed { index, line ->
        val color = when {
            line.startsWith("--- ") || line.startsWith("+++ ") -> fileHeaderColor
            line.startsWith("@@") -> hunkColor
            line.startsWith('+') -> additionColor
            line.startsWith('-') -> deletionColor
            else -> null
        }
        if (color == null) {
            append(line)
        } else {
            withStyle(SpanStyle(color = color)) {
                append(line)
            }
        }
        if (index < lines.lastIndex) append('\n')
    }
}
