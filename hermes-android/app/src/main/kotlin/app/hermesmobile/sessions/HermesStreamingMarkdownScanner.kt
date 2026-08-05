package app.hermesmobile.sessions

data class HermesStreamingMarkdownSnapshot(
    val settledBlocks: List<String>,
    val tail: String,
) {
    val settled: String = settledBlocks.joinToString(separator = "")
}

/**
 * Incrementally scans an append-only Markdown stream.
 *
 * Only complete, newline-terminated lines become scanner state. Settled blocks
 * are appended at blank-line boundaries and never rescanned while [text]
 * continues to extend the previously scanned prefix.
 */
class HermesStreamingMarkdownScanner {
    private enum class DisplayMathDelimiter(
        val opener: String,
        val closer: String,
    ) {
        Dollars("$$", "$$"),
        Brackets("\\[", "\\]"),
    }

    private val settledBlocks = mutableListOf<String>()
    private var source = ""
    private var currentLineStart = 0
    private var settledLength = 0
    private var fenceCharacter: Char? = null
    private var fenceLength = 0
    private var mathDelimiter: DisplayMathDelimiter? = null
    internal var processedCompleteLineCount: Long = 0
        private set
    internal var processedCharacterCount: Long = 0
        private set

    fun advance(text: String): HermesStreamingMarkdownSnapshot {
        if (!text.startsWith(source)) reset()

        return advanceVerifiedAppend(text)
    }

    internal fun advanceVerifiedAppend(text: String): HermesStreamingMarkdownSnapshot {
        require(text.length >= source.length)
        for (index in source.length until text.length) {
            processedCharacterCount += 1
            if (text[index] != '\n') continue
            processedCompleteLineCount += 1

            val line = text.substring(currentLineStart, index).trim()
            if (line.isNotEmpty()) {
                applyLine(line)
            } else if (
                index > 0 &&
                fenceCharacter == null &&
                mathDelimiter == null
            ) {
                val block = text.substring(settledLength, index + 1)
                if (block.any { !it.isWhitespace() }) {
                    settledBlocks += block
                    settledLength = index + 1
                }
            }
            currentLineStart = index + 1
        }
        source = text

        return HermesStreamingMarkdownSnapshot(
            settledBlocks = settledBlocks.toList(),
            tail = text.substring(settledLength),
        )
    }

    private fun applyLine(line: String) {
        val openCharacter = fenceCharacter
        if (openCharacter != null) {
            val markerLength = line.takeWhile { it == openCharacter }.length
            if (
                markerLength >= fenceLength &&
                line.drop(markerLength).isBlank()
            ) {
                fenceCharacter = null
                fenceLength = 0
            }
            return
        }

        val openMath = mathDelimiter
        if (openMath != null) {
            if (line.endsWith(openMath.closer)) mathDelimiter = null
            return
        }

        val marker = line.firstOrNull()?.takeIf { it == '`' || it == '~' }
        if (marker != null) {
            val markerLength = line.takeWhile { it == marker }.length
            if (markerLength >= 3) {
                fenceCharacter = marker
                fenceLength = markerLength
                return
            }
        }

        mathDelimiter = DisplayMathDelimiter.entries.firstOrNull { delimiter ->
            line.startsWith(delimiter.opener) && !isSingleLineMath(line, delimiter)
        }
    }

    private fun isSingleLineMath(
        line: String,
        delimiter: DisplayMathDelimiter,
    ): Boolean = line.length >= delimiter.opener.length + delimiter.closer.length &&
        line.removePrefix(delimiter.opener).endsWith(delimiter.closer)

    fun reset() {
        settledBlocks.clear()
        source = ""
        currentLineStart = 0
        settledLength = 0
        fenceCharacter = null
        fenceLength = 0
        mathDelimiter = null
        processedCompleteLineCount = 0
        processedCharacterCount = 0
    }
}
