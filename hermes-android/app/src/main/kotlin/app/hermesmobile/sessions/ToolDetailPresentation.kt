package app.hermesmobile.sessions

internal const val TOOL_DETAIL_PREVIEW_MAX_LINES = 24
internal const val TOOL_DETAIL_PREVIEW_MAX_CODE_POINTS = 4_000

internal data class ToolDetailPresentation(
    val visibleBody: String,
    val canExpand: Boolean,
    val isTruncated: Boolean,
)

internal fun toolDetailPresentation(
    body: String,
    expanded: Boolean,
    maxLines: Int = TOOL_DETAIL_PREVIEW_MAX_LINES,
    maxCodePoints: Int = TOOL_DETAIL_PREVIEW_MAX_CODE_POINTS,
): ToolDetailPresentation {
    require(maxLines > 0)
    require(maxCodePoints > 0)

    val codePointCount = body.codePointCount(0, body.length)
    val lineBounded = body.lineSequence().take(maxLines).joinToString("\n")
    val exceedsLineLimit = lineBounded.length < body.length
    val exceedsCodePointLimit = codePointCount > maxCodePoints
    val canExpand = exceedsLineLimit || exceedsCodePointLimit

    if (expanded || !canExpand) {
        return ToolDetailPresentation(
            visibleBody = body,
            canExpand = canExpand,
            isTruncated = false,
        )
    }

    val codePointBounded = if (
        lineBounded.codePointCount(0, lineBounded.length) > maxCodePoints
    ) {
        lineBounded.substring(
            0,
            lineBounded.offsetByCodePoints(0, maxCodePoints),
        )
    } else {
        lineBounded
    }
    return ToolDetailPresentation(
        visibleBody = codePointBounded,
        canExpand = true,
        isTruncated = true,
    )
}
