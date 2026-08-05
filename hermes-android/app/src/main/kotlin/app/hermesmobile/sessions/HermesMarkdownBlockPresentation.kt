package app.hermesmobile.sessions

internal data class HermesMarkdownListRow(
    val marker: String,
    val text: String,
    val depth: Int,
)

internal fun presentHermesMarkdownListItems(
    items: List<HermesMarkdownListItem>,
): List<HermesMarkdownListRow> = items.map { item ->
    HermesMarkdownListRow(
        marker = when (item.taskState) {
            HermesMarkdownTaskState.UNCHECKED -> "☐"
            HermesMarkdownTaskState.CHECKED -> "☑"
            null -> when (item.kind) {
                HermesMarkdownListKind.UNORDERED -> "•"
                HermesMarkdownListKind.ORDERED -> "${item.ordinal ?: 1}."
            }
        },
        text = item.text,
        depth = item.depth,
    )
}

internal data class HermesMarkdownQuoteRow(
    val text: String,
    val railDepth: Int,
)

internal fun presentHermesMarkdownQuoteLines(
    lines: List<HermesMarkdownQuoteLine>,
): List<HermesMarkdownQuoteRow> = lines.map { line ->
    HermesMarkdownQuoteRow(
        text = line.text,
        railDepth = line.depth.coerceAtLeast(1),
    )
}

internal enum class HermesMarkdownMediaKind(val label: String) {
    IMAGE("Image"),
    AUDIO("Audio"),
    VIDEO("Video"),
    FILE("File"),
}

internal data class HermesMarkdownMediaPresentation(
    val kind: HermesMarkdownMediaKind,
    val label: String,
    val openUrl: String?,
)

internal fun presentHermesMarkdownMedia(path: String): HermesMarkdownMediaPresentation {
    val pathWithoutQuery = path.substringBefore('?').substringBefore('#')
    val extension = pathWithoutQuery.substringAfterLast('.', missingDelimiterValue = "").lowercase()
    val kind = when (extension) {
        "png", "jpg", "jpeg", "webp", "gif", "bmp", "svg" -> HermesMarkdownMediaKind.IMAGE
        "mp3", "ogg", "opus", "wav", "m4a", "aac", "flac" -> HermesMarkdownMediaKind.AUDIO
        "mp4", "webm", "mov", "m4v" -> HermesMarkdownMediaKind.VIDEO
        else -> HermesMarkdownMediaKind.FILE
    }
    val fileName = pathWithoutQuery
        .replace('\\', '/')
        .substringAfterLast('/')
        .ifBlank { "Attachment" }
    val safeLabel = HermesMessagePresentation.safeText(fileName, maxCodePoints = 128)
        .orEmpty()
        .ifBlank { "Attachment" }
    val openUrl = HermesSafeLinkPolicy.normalizeOrNull(path)
        ?.takeIf { it.startsWith("https://") || it.startsWith("http://") }
    return HermesMarkdownMediaPresentation(
        kind = kind,
        label = safeLabel,
        openUrl = openUrl,
    )
}
