package app.hermesmobile.sessions

import java.net.URI
import java.net.URLDecoder
import java.nio.charset.StandardCharsets

sealed interface HermesInlineSpan {
    sealed interface Text : HermesInlineSpan {
        val text: String

        data class Literal(override val text: String) : Text

        companion object {
            operator fun invoke(text: String): Text = Literal(text)
        }
    }

    data class Bold(val text: String) : HermesInlineSpan
    data class Italic(val text: String) : HermesInlineSpan
    data class Strike(override val text: String) : Text
    data class Highlight(override val text: String) : Text
    data class Math(override val text: String) : Text
    data class Code(val text: String) : HermesInlineSpan
    data class Link(val label: String, val url: String) : HermesInlineSpan
    data class FootnoteReference(val label: String) : HermesInlineSpan
}

object HermesInlineMarkdownParser {
    fun parse(text: String): List<HermesInlineSpan> {
        if (text.isEmpty()) return emptyList()
        val spans = mutableListOf<HermesInlineSpan>()
        val plain = StringBuilder()
        var index = 0

        fun flushPlain() {
            if (plain.isNotEmpty()) {
                spans += HermesInlineSpan.Text(plain.toString())
                plain.clear()
            }
        }

        while (index < text.length) {
            val parsed = parseDelimited(text, index, "**")
                ?.let { (value, end) -> HermesInlineSpan.Bold(value) to end }
                ?: parseDelimited(text, index, "__")
                    ?.let { (value, end) -> HermesInlineSpan.Bold(value) to end }
                ?: parseDelimited(text, index, "~~")
                    ?.let { (value, end) -> HermesInlineSpan.Strike(value) to end }
                ?: parseDelimited(text, index, "==")
                    ?.let { (value, end) -> HermesInlineSpan.Highlight(value) to end }
                ?: parseDelimited(text, index, "`")
                    ?.let { (value, end) -> HermesInlineSpan.Code(value) to end }
                ?: parseFootnoteReference(text, index)
                ?: parseLink(text, index)
                ?: parseAngleAutolink(text, index)
                ?: parseDelimited(text, index, "\$")
                    ?.let { (value, end) -> HermesInlineSpan.Math(value) to end }
                ?: parseDelimited(text, index, "\\(", "\\)")
                    ?.let { (value, end) -> HermesInlineSpan.Math(value) to end }
                ?: parseDelimited(text, index, "*")
                    ?.let { (value, end) -> HermesInlineSpan.Italic(value) to end }
                ?: parseDelimited(text, index, "_")
                    ?.let { (value, end) -> HermesInlineSpan.Italic(value) to end }

            if (parsed == null) {
                plain.append(text[index])
                index += 1
            } else {
                flushPlain()
                spans += parsed.first
                index = parsed.second
            }
        }
        flushPlain()
        return spans
    }

    private fun parseDelimited(
        text: String,
        start: Int,
        delimiter: String,
        closingDelimiter: String = delimiter,
    ): Pair<String, Int>? {
        if (!text.startsWith(delimiter, start)) return null
        if (
            delimiter.all { it == '_' } &&
            text.getOrNull(start - 1)?.isIdentifierCharacter() == true &&
            text.getOrNull(start + delimiter.length)?.isIdentifierCharacter() == true
        ) {
            return null
        }
        val contentStart = start + delimiter.length
        val end = text.indexOf(closingDelimiter, contentStart)
        if (end <= contentStart) return null
        val content = text.substring(contentStart, end)
        if (content.first().isWhitespace() || content.last().isWhitespace()) return null
        if (
            delimiter == "__" &&
            content.all { it.isIdentifierCharacter() }
        ) {
            return null
        }
        return content to (end + closingDelimiter.length)
    }

    private fun parseLink(text: String, start: Int): Pair<HermesInlineSpan, Int>? {
        if (text.getOrNull(start) != '[') return null
        val labelEnd = text.indexOf("](", startIndex = start + 1)
        if (labelEnd <= start + 1) return null
        val urlStart = labelEnd + 2
        val urlEnd = findClosingParenthesis(text, urlStart) ?: return null
        val label = text.substring(start + 1, labelEnd)
        val url = text.substring(urlStart, urlEnd)
        val normalizedUrl = HermesSafeLinkPolicy.normalizeOrNull(url)
        val span = if (normalizedUrl != null) {
            HermesInlineSpan.Link(label, normalizedUrl)
        } else {
            HermesInlineSpan.Text(label)
        }
        return span to (urlEnd + 1)
    }

    private fun parseFootnoteReference(text: String, start: Int): Pair<HermesInlineSpan, Int>? {
        if (!text.startsWith("[^", start)) return null
        val end = text.indexOf(']', startIndex = start + 2)
        if (end <= start + 2) return null
        val label = text.substring(start + 2, end)
        if (label.any(Char::isWhitespace)) return null
        return HermesInlineSpan.FootnoteReference(label) to (end + 1)
    }

    private fun parseAngleAutolink(text: String, start: Int): Pair<HermesInlineSpan, Int>? {
        if (text.getOrNull(start) != '<') return null
        val end = text.indexOf('>', startIndex = start + 1)
        if (end <= start + 1) return null
        val label = text.substring(start + 1, end)
        val destination = if (':' in label) label else "mailto:$label"
        val normalizedUrl = HermesSafeLinkPolicy.normalizeOrNull(destination) ?: return null
        return HermesInlineSpan.Link(label, normalizedUrl) to (end + 1)
    }

    private fun Char.isIdentifierCharacter(): Boolean = isLetterOrDigit() || this == '_'

    private fun findClosingParenthesis(text: String, start: Int): Int? {
        var depth = 0
        for (index in start until text.length) {
            when (text[index]) {
                '(' -> depth += 1
                ')' -> if (depth == 0) return index else depth -= 1
            }
        }
        return null
    }
}

internal object HermesSafeLinkPolicy {
    private val sensitiveQueryTerms = setOf(
        "access token",
        "refresh token",
        "session token",
        "token",
        "api key",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "client secret",
        "credential",
        "cookie",
        "authorization",
        "ws ticket",
        "websocket ticket",
        "ticket",
        "lease",
        "control lease id",
        "lease id",
    )

    fun normalizeOrNull(rawUrl: String): String? {
        if (rawUrl.isBlank() || rawUrl.any { it.isWhitespace() || it.isISOControl() }) return null
        val uri = runCatching { URI(rawUrl).normalize() }.getOrNull() ?: return null
        val scheme = uri.scheme?.lowercase() ?: return null
        val rawQuery = uri.rawQuery ?: if (scheme == "mailto") {
            uri.rawSchemeSpecificPart
                ?.substringAfter('?', missingDelimiterValue = "")
                ?.takeIf(String::isNotEmpty)
        } else {
            null
        }
        if (
            hasSensitiveParameters(rawQuery) ||
            hasSensitiveParameters(uri.rawFragment)
        ) return null
        return when (scheme) {
            "http", "https" -> uri
                .takeIf { it.host?.isNotBlank() == true && it.rawUserInfo == null }
                ?.toASCIIString()
            "mailto" -> uri
                .takeIf {
                    val address = it.rawSchemeSpecificPart?.substringBefore('?')
                    it.rawUserInfo == null &&
                        address != null &&
                        '@' in address &&
                        '/' !in address
                }
                ?.toASCIIString()
            else -> null
        }
    }

    private fun hasSensitiveParameters(rawComponent: String?): Boolean = rawComponent
        ?.split('&', ';', '?', '#', '/')
        ?.any { parameter ->
            val rawKey = parameter.substringBefore('=')
            val decodedKey = decodeToFixedPointOrNull(rawKey) ?: return@any true
            val normalizedWords = decodedKey
                .replace(Regex("([a-z0-9])([A-Z])"), "$1 $2")
                .lowercase()
                .split(Regex("[^a-z0-9]+"))
                .filter(String::isNotBlank)
            sensitiveQueryTerms.any { term ->
                val termWords = term.split(' ')
                normalizedWords.windowed(termWords.size).any { window -> window == termWords }
            }
        }
        ?: false

    private fun decodeToFixedPointOrNull(rawValue: String): String? {
        var current = rawValue
        repeat(MAXIMUM_DECODE_ROUNDS) {
            val decoded = runCatching {
                URLDecoder.decode(current, StandardCharsets.UTF_8.name())
            }.getOrElse { return current }
            if (decoded.any(Char::isISOControl)) return null
            if (decoded == current) return current
            current = decoded
        }
        val next = runCatching {
            URLDecoder.decode(current, StandardCharsets.UTF_8.name())
        }.getOrElse { return current }
        return current.takeIf { next == current }
    }

    private const val MAXIMUM_DECODE_ROUNDS = 4
}
