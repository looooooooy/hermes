package app.hermesmobile.sessions

enum class HermesMarkdownListKind {
    UNORDERED,
    ORDERED,
}

enum class HermesMarkdownTaskState {
    UNCHECKED,
    CHECKED,
}

data class HermesMarkdownListItem(
    val text: String,
    val depth: Int,
    val kind: HermesMarkdownListKind = HermesMarkdownListKind.UNORDERED,
    val ordinal: Int? = null,
    val taskState: HermesMarkdownTaskState? = null,
)

data class HermesMarkdownQuoteLine(
    val text: String,
    val depth: Int,
)

sealed interface HermesMarkdownBlock {
    data class Heading(val level: Int, val text: String) : HermesMarkdownBlock
    data class Paragraph(val text: String) : HermesMarkdownBlock
    data class CodeFence(
        val language: String?,
        val code: String,
        val isComplete: Boolean,
    ) : HermesMarkdownBlock
    data class BulletList(
        val items: List<String>,
        val structuredItems: List<HermesMarkdownListItem> = items.map { item ->
            HermesMarkdownListItem(text = item, depth = 0)
        },
    ) : HermesMarkdownBlock
    data class OrderedList(
        val items: List<String>,
        val structuredItems: List<HermesMarkdownListItem> = items.mapIndexed { index, item ->
            HermesMarkdownListItem(
                text = item,
                depth = 0,
                kind = HermesMarkdownListKind.ORDERED,
                ordinal = index + 1,
            )
        },
    ) : HermesMarkdownBlock
    data class Quote(
        val text: String,
        val structuredLines: List<HermesMarkdownQuoteLine> = text.lines().map { line ->
            HermesMarkdownQuoteLine(text = line, depth = 1)
        },
    ) : HermesMarkdownBlock
    data class Table(
        val headers: List<String>,
        val rows: List<List<String>>,
    ) : HermesMarkdownBlock
    data class FootnoteDefinition(
        val label: String,
        val text: String,
    ) : HermesMarkdownBlock
    data class DisplayMath(val text: String) : HermesMarkdownBlock
    data class Media(val path: String) : HermesMarkdownBlock
    data object HorizontalRule : HermesMarkdownBlock
}

data class HermesStreamingMarkdownSplit(
    val settled: String,
    val tail: String,
)

object HermesStreamingMarkdown {
    fun splitStableBoundary(text: String): HermesStreamingMarkdownSplit {
        val snapshot = HermesStreamingMarkdownScanner().advance(text)
        return HermesStreamingMarkdownSplit(
            settled = snapshot.settledBlocks.joinToString(separator = ""),
            tail = snapshot.tail,
        )
    }
}

object HermesMarkdownParser {
    private val headingPattern = Regex("^(#{1,6})\\s+(.+)$")
    private val setextHeadingPattern = Regex("^(=+|-+)\\s*$")
    private val bulletPattern = Regex("^(\\s*)[-*+]\\s+(.+)$")
    private val orderedPattern = Regex("^(\\s*)(\\d+)[.)]\\s+(.+)$")
    private val taskPattern = Regex("^\\[([ xX])]\\s+(.*)$")
    private val quotePattern = Regex("^\\s*((?:>\\s*)+)(.*)$")
    private val footnoteDefinitionPattern = Regex("^\\[\\^([^]\\s]+)]\\s*:\\s*(.*)$")
    private val footnoteContinuationPattern = Regex("^\\s{2,}(.*)$")
    private val horizontalRulePattern = Regex(
        "^(?:(?:-\\s*){3,}|(?:\\*\\s*){3,}|(?:_\\s*){3,})$",
    )
    private val mediaPattern = Regex("^[`\"']?MEDIA:\\s*(\\S+?)[`\"']?$")
    private val audioDirectivePattern = Regex("^\\[\\[audio_as_voice]]$")

    fun parse(markdown: String): List<HermesMarkdownBlock> {
        if (markdown.isBlank()) return emptyList()
        val lines = markdown.replace("\r\n", "\n").replace('\r', '\n').split('\n')
        val blocks = mutableListOf<HermesMarkdownBlock>()
        var index = 0

        while (index < lines.size) {
            val line = lines[index]
            if (line.isBlank()) {
                index += 1
                continue
            }

            val trimmed = line.trimStart()
            if (audioDirectivePattern.matches(trimmed)) {
                index += 1
                continue
            }
            val fenceMarker = fenceMarker(trimmed)
            if (fenceMarker != null) {
                val language = trimmed.removePrefix(fenceMarker).trim().ifBlank { null }
                index += 1
                val codeLines = mutableListOf<String>()
                var complete = false
                while (index < lines.size) {
                    val closingLine = lines[index].trimStart()
                    val closingLength = closingLine.takeWhile { it == fenceMarker.first() }.length
                    if (
                        closingLength >= fenceMarker.length &&
                        closingLine.drop(closingLength).isBlank()
                    ) {
                        complete = true
                        index += 1
                        break
                    }
                    codeLines += lines[index]
                    index += 1
                }
                blocks += HermesMarkdownBlock.CodeFence(
                    language = language,
                    code = codeLines.joinToString("\n"),
                    isComplete = complete,
                )
                continue
            }

            val media = mediaPattern.matchEntire(trimmed)
            if (media != null) {
                blocks += HermesMarkdownBlock.Media(media.groupValues[1])
                index += 1
                continue
            }

            val footnoteDefinition = footnoteDefinitionPattern.matchEntire(trimmed)
            if (footnoteDefinition != null) {
                val body = mutableListOf(footnoteDefinition.groupValues[2].trimEnd())
                index += 1
                while (index < lines.size) {
                    val continuation = footnoteContinuationPattern.matchEntire(lines[index]) ?: break
                    body += continuation.groupValues[1].trimEnd()
                    index += 1
                }
                blocks += HermesMarkdownBlock.FootnoteDefinition(
                    label = footnoteDefinition.groupValues[1],
                    text = body.joinToString("\n"),
                )
                continue
            }

            val mathOpener = mathOpener(trimmed)
            if (mathOpener != null) {
                val expectedCloser = mathCloserFor(mathOpener)
                val remainder = trimmed.removePrefix(mathOpener)
                val inlineCloser = expectedCloser.takeIf {
                    it.length < remainder.length && remainder.endsWith(it)
                }
                if (inlineCloser != null) {
                    blocks += HermesMarkdownBlock.DisplayMath(
                        remainder.substring(0, remainder.length - inlineCloser.length).trim(),
                    )
                    index += 1
                    continue
                }
                val closingIndex = (index + 1 until lines.size).firstOrNull { candidate ->
                    lines[candidate].trimEnd().endsWith(expectedCloser)
                }
                if (closingIndex == null) {
                    val paragraphLines = mutableListOf(line.trimEnd())
                    index += 1
                    while (index < lines.size && lines[index].isNotBlank()) {
                        if (isBlockStart(lines[index])) break
                        paragraphLines += lines[index].trimEnd()
                        index += 1
                    }
                    blocks += HermesMarkdownBlock.Paragraph(paragraphLines.joinToString("\n"))
                    continue
                }
                val body = mutableListOf(remainder)
                index += 1
                while (index <= closingIndex) {
                    val closingLine = lines[index].trimEnd()
                    if (closingLine.endsWith(expectedCloser)) {
                        body += closingLine.substring(0, closingLine.length - expectedCloser.length)
                        index += 1
                        break
                    }
                    body += lines[index]
                    index += 1
                }
                blocks += HermesMarkdownBlock.DisplayMath(body.joinToString("\n").trim())
                continue
            }

            val heading = headingPattern.matchEntire(trimmed)
            if (heading != null) {
                blocks += HermesMarkdownBlock.Heading(
                    level = heading.groupValues[1].length,
                    text = heading.groupValues[2].trimEnd(),
                )
                index += 1
                continue
            }

            val setextUnderline = lines.getOrNull(index + 1)
                ?.trimStart()
                ?.let(setextHeadingPattern::matchEntire)
            if (setextUnderline != null) {
                blocks += HermesMarkdownBlock.Heading(
                    level = if (setextUnderline.value.startsWith('=')) 1 else 2,
                    text = line.trim(),
                )
                index += 2
                continue
            }

            if (horizontalRulePattern.matches(trimmed)) {
                blocks += HermesMarkdownBlock.HorizontalRule
                index += 1
                continue
            }

            val tableHeaders = tableCells(line)
            val tableDivider = lines.getOrNull(index + 1)?.let(::tableCells)
            if (tableHeaders != null && tableDivider?.all(::isTableDividerCell) == true) {
                index += 2
                val rows = mutableListOf<List<String>>()
                while (index < lines.size) {
                    val row = tableCells(lines[index]) ?: break
                    rows += row.padTo(tableHeaders.size)
                    index += 1
                }
                blocks += HermesMarkdownBlock.Table(
                    headers = tableHeaders,
                    rows = rows,
                )
                continue
            }

            val firstBullet = bulletPattern.matchEntire(line)
            val firstOrdered = orderedPattern.matchEntire(line)
            if (firstBullet != null || firstOrdered != null) {
                val rootKind = if (firstOrdered != null) {
                    HermesMarkdownListKind.ORDERED
                } else {
                    HermesMarkdownListKind.UNORDERED
                }
                val rootIndentation = (firstBullet ?: firstOrdered!!).groupValues[1].length
                val items = mutableListOf<String>()
                val structuredItems = mutableListOf<HermesMarkdownListItem>()
                val indentationLevels = mutableListOf<Int>()
                while (index < lines.size) {
                    val bullet = bulletPattern.matchEntire(lines[index])
                    val ordered = orderedPattern.matchEntire(lines[index])
                    if (bullet == null && ordered == null) break
                    val kind = if (ordered != null) {
                        HermesMarkdownListKind.ORDERED
                    } else {
                        HermesMarkdownListKind.UNORDERED
                    }
                    val match = bullet ?: ordered!!
                    val indentation = match.groupValues[1].length
                    if (indentation < rootIndentation || indentation == rootIndentation && kind != rootKind) {
                        break
                    }
                    while (indentationLevels.lastOrNull()?.let { it > indentation } == true) {
                        indentationLevels.removeAt(indentationLevels.lastIndex)
                    }
                    if (indentationLevels.lastOrNull()?.let { it < indentation } != false) {
                        indentationLevels += indentation
                    }
                    val rawText = match.groupValues[if (ordered == null) 2 else 3].trimEnd()
                    val task = taskPattern.matchEntire(rawText)
                    items += rawText
                    structuredItems += HermesMarkdownListItem(
                        text = task?.groupValues?.get(2) ?: rawText,
                        depth = indentationLevels.lastIndex,
                        kind = kind,
                        ordinal = ordered?.groupValues?.get(2)?.toIntOrNull(),
                        taskState = task?.groupValues?.get(1)?.let { marker ->
                            if (marker.equals("x", ignoreCase = true)) {
                                HermesMarkdownTaskState.CHECKED
                            } else {
                                HermesMarkdownTaskState.UNCHECKED
                            }
                        },
                    )
                    index += 1
                }
                blocks += if (rootKind == HermesMarkdownListKind.ORDERED) {
                    HermesMarkdownBlock.OrderedList(
                        items = items,
                        structuredItems = structuredItems,
                    )
                } else {
                    HermesMarkdownBlock.BulletList(
                        items = items,
                        structuredItems = structuredItems,
                    )
                }
                continue
            }

            if (quotePattern.matches(line)) {
                val quoteLines = mutableListOf<String>()
                val structuredLines = mutableListOf<HermesMarkdownQuoteLine>()
                while (index < lines.size) {
                    val quote = quotePattern.matchEntire(lines[index]) ?: break
                    val depth = quote.groupValues[1].count { it == '>' }
                    val text = quote.groupValues[2].trimEnd()
                    quoteLines += if (depth == 1) {
                        text
                    } else {
                        List(depth - 1) { ">" }.joinToString(" ") +
                            if (text.isEmpty()) "" else " $text"
                    }
                    structuredLines += HermesMarkdownQuoteLine(text = text, depth = depth)
                    index += 1
                }
                blocks += HermesMarkdownBlock.Quote(
                    text = quoteLines.joinToString("\n"),
                    structuredLines = structuredLines,
                )
                continue
            }

            val paragraphLines = mutableListOf<String>()
            while (index < lines.size && lines[index].isNotBlank()) {
                if (paragraphLines.isNotEmpty() && isBlockStart(lines[index])) break
                paragraphLines += lines[index].trimEnd()
                index += 1
            }
            blocks += HermesMarkdownBlock.Paragraph(paragraphLines.joinToString("\n"))
        }

        return blocks
    }

    private fun isBlockStart(line: String): Boolean {
        val trimmed = line.trimStart()
        return fenceMarker(trimmed) != null ||
            mediaPattern.matches(trimmed) ||
            footnoteDefinitionPattern.matches(trimmed) ||
            mathOpener(trimmed) != null ||
            headingPattern.matches(trimmed) ||
            horizontalRulePattern.matches(trimmed) ||
            bulletPattern.matches(line) ||
            orderedPattern.matches(line) ||
            quotePattern.matches(line)
    }

    private fun fenceMarker(line: String): String? {
        val marker = line.firstOrNull()?.takeIf { it == '`' || it == '~' } ?: return null
        val markerLength = line.takeWhile { it == marker }.length
        return marker.toString().repeat(markerLength).takeIf { markerLength >= 3 }
    }

    private fun mathOpener(line: String): String? = when {
        line.startsWith("$$") -> "$$"
        line.startsWith("\\[") -> "\\["
        else -> null
    }

    private fun mathCloserFor(opener: String): String = when (opener) {
        "$$" -> "$$"
        "\\[" -> "\\]"
        else -> error("Unsupported display math opener: $opener")
    }

    private fun tableCells(line: String): List<String>? {
        if ('|' !in line) return null
        val trimmed = line.trim().removePrefix("|").removeSuffix("|")
        val cells = trimmed.split('|').map(String::trim)
        return cells.takeIf { it.isNotEmpty() && it.any(String::isNotBlank) }
    }

    private fun isTableDividerCell(cell: String): Boolean =
        cell.matches(Regex("^:?-{3,}:?$"))

    private fun List<String>.padTo(size: Int): List<String> =
        take(size) + List((size - this.size).coerceAtLeast(0)) { "" }
}
