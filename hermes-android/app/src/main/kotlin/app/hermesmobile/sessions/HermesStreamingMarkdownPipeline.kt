package app.hermesmobile.sessions

import java.util.Collections

data class HermesStreamingMarkdownPipelineSnapshot(
    val sanitizedMarkdown: String,
    val settledMarkdown: List<String>,
    val tailMarkdown: String,
    val parsedSettledBlocks: List<List<HermesMarkdownBlock>>,
    val parsedTailBlocks: List<HermesMarkdownBlock>,
    val generation: Long,
    val settledSegmentIds: List<Long>,
    val tailSegmentId: Long,
) {
    val blocks: List<HermesMarkdownBlock> by lazy(LazyThreadSafetyMode.NONE) {
        immutableList(parsedSettledBlocks.flatten() + parsedTailBlocks)
    }
}

class HermesStreamingMarkdownPipeline {
    private val sanitizer = HermesStreamingMarkdownSanitizer()
    private val scanner = HermesStreamingMarkdownScanner()
    private val parsedSettledBlocks = mutableListOf<List<HermesMarkdownBlock>>()
    private val settledSegmentIds = mutableListOf<Long>()
    private var source = ""
    private var lastSnapshot: HermesStreamingMarkdownPipelineSnapshot? = null
    private var generation: Long = 0
    private var nextSegmentId: Long = 0

    internal val processedSourceCharacterCount: Long
        get() = sanitizer.processedSourceCharacterCount
    internal val processedSanitizedCharacterCount: Long
        get() = scanner.processedCharacterCount
    internal var settledBlockParseCount: Long = 0
        private set

    fun advance(markdown: String): HermesStreamingMarkdownPipelineSnapshot {
        if (markdown == source) {
            lastSnapshot?.let { return it }
        }
        if (!markdown.startsWith(source)) reset()

        val sanitized = sanitizer.advanceVerifiedAppend(markdown)
        val scanned = scanner.advanceVerifiedAppend(sanitized)
        for (index in parsedSettledBlocks.size until scanned.settledBlocks.size) {
            parsedSettledBlocks += immutableList(
                HermesMarkdownParser.parse(scanned.settledBlocks[index]),
            )
            settledSegmentIds += nextSegmentId
            nextSegmentId += 1
            settledBlockParseCount += 1
        }
        val tail = immutableList(HermesMarkdownParser.parse(scanned.tail))
        source = markdown
        return HermesStreamingMarkdownPipelineSnapshot(
            sanitizedMarkdown = sanitized,
            settledMarkdown = immutableList(scanned.settledBlocks),
            tailMarkdown = scanned.tail,
            parsedSettledBlocks = immutableList(parsedSettledBlocks),
            parsedTailBlocks = tail,
            generation = generation,
            settledSegmentIds = immutableList(settledSegmentIds),
            tailSegmentId = nextSegmentId,
        ).also { lastSnapshot = it }
    }

    private fun reset() {
        sanitizer.reset()
        scanner.reset()
        parsedSettledBlocks.clear()
        settledSegmentIds.clear()
        settledBlockParseCount = 0
        source = ""
        lastSnapshot = null
        generation += 1
        nextSegmentId = 0
    }
}

private fun <T> immutableList(values: Collection<T>): List<T> =
    Collections.unmodifiableList(ArrayList(values))
