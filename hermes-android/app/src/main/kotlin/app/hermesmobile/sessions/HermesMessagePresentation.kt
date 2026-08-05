package app.hermesmobile.sessions

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

internal data class HermesToolCallPresentation(
    val label: String,
    val details: List<ConversationToolDetailUiModel>,
) {
    fun visibleText(): String = buildString {
        append(label)
        details.forEach { detail ->
            append('\n')
            append(detail.label)
            append(": ")
            append(detail.value)
        }
    }
}

internal data class HermesReadablePayload(
    val text: String,
    val details: List<ConversationToolDetailUiModel>,
) {
    fun visibleText(): String = buildString {
        append(text)
        details.forEach { detail ->
            if (isNotEmpty()) append('\n')
            append(detail.label)
            append(": ")
            append(detail.value)
        }
    }
}

internal object HermesMessagePresentation {
    private const val MAX_DECODE_DEPTH = 6
    private const val MAX_DETAIL_ROWS = 200
    private const val MAX_PRESENTATION_NODES = 400
    private const val MAX_VISIBLE_VALUE_CHARS = 8_000
    internal const val MAX_LONG_OUTPUT_CODE_POINTS = 128 * 1024
    private const val MAX_VISIBLE_LABEL_CHARS = 160
    private const val MAX_ENCODED_JSON_CHARS = 64_000
    private const val OMITTED_VALUE = "[additional structured data omitted]"

    private val json = Json { ignoreUnknownKeys = true }
    private val primaryTextKeys = listOf(
        "output",
        "result_text",
        "result",
        "summary",
        "message",
        "content",
        "text",
        "rendered",
        "answer",
        "file_preview",
        "diff",
        "error",
    )
    private val contextKeys = listOf(
        "command",
        "path",
        "url",
        "query",
        "question",
        "goal",
        "pattern",
        "name",
    )
    private val sensitiveLabelTerms = listOf(
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "cookie",
        "authorization",
        "api key",
        "apikey",
        "private key",
        "privatekey",
        "ssh key",
        "sshkey",
        "credential",
        "bearer",
        "authentication",
        "ticket",
        "lease id",
        "leaseid",
    )
    private val sensitiveAssignment = Regex(
        """(?i)\b(access[_ -]?token|refresh[_ -]?token|session[_ -]?token|token|api[_ -]?key|password|passwd|passphrase|secret|client[_ -]?secret|private[_ -]?key|ssh[_ -]?key|credential|cookie|auth|authorization|ws[_ -]?ticket|websocket[_ -]?ticket|ticket|control[_ -]?lease[_ -]?id|lease[_ -]?id)\b(\s*[:=]\s*)(?:(?:bearer|basic)\s+)?(?:"[^"]*"|'[^']*'|[^\s,;&]+)""",
    )
    private val sensitiveCliFlag = Regex(
        """(?i)((?<![a-z0-9_-])--?(?:access[-_]?token|refresh[-_]?token|session[-_]?token|token|api[-_]?key|password|passwd|passphrase|secret|client[-_]?secret|private[-_]?key|ssh[-_]?key|credential|cookie|auth|authorization|ws[-_]?ticket|websocket[-_]?ticket|ticket|control[-_]?lease[-_]?id|lease[-_]?id)(?:=|\s+))(?:"[^"]*"|'[^']*'|[^\s,;&]+)""",
    )
    private val authorizationHeader = Regex(
        """(?i)(authorization\s*:\s*)(?:(?:bearer|basic)\s+)?[^\s,'";]+""",
    )
    private val bearerValue = Regex("""(?i)\bbearer\s+[^\s,'";]+""")
    private val basicValue = Regex("""(?i)\bbasic\s+[^\s,'";]+""")
    private val urlUserInfo = Regex("""(?i)([a-z][a-z0-9+.-]*://)[^/@\s]*@""")
    private val cliUserFlag = Regex(
        """(?i)((?:^|\s)(?:-u(?:=|\s*)|--user(?:=|\s+)))(?:"[^"]*"|'[^']*'|[^\s,;&]+)""",
    )
    private val toolProtocolKeys = setOf(
        "id",
        "tool_id",
        "call_id",
        "name",
        "tool_name",
        "arguments",
        "args",
        "args_text",
        "input",
        "parameters",
        "context",
        "status",
        "duration_s",
        "duration_seconds",
        "sequence",
        "seq",
    )

    fun toolCall(
        name: String?,
        arguments: String?,
        context: String? = null,
    ): HermesToolCallPresentation = toolCall(
        name = name,
        arguments = arguments?.takeIf(String::isNotBlank)?.parseJsonOrText(),
        context = context,
    )

    fun toolCall(
        name: String?,
        arguments: JsonElement?,
        context: String? = null,
    ): HermesToolCallPresentation {
        val normalizedArguments = arguments?.normalizeForPresentation()
        val details = normalizedArguments?.toDetailRows().orEmpty()
        val explicitContext = context
            ?.takeIf(String::isNotBlank)
            ?.let(::payloadText)
            ?.visibleText()
            ?.takeIf(String::isNotBlank)
        val argumentContext = (normalizedArguments as? JsonObject)
            ?.let { body ->
                contextKeys.firstNotNullOfOrNull { key ->
                    body[key]?.readableScalar(key.humanLabel())?.takeIf(String::isNotBlank)
                }
            }
            ?: normalizedArguments?.readableScalar().orEmpty().takeIf(String::isNotBlank)
        val derivedContext = argumentContext ?: explicitContext.orEmpty()
        val label = toolLabel(name)
        val preview = derivedContext.compactPreview(64)
        return HermesToolCallPresentation(
            label = if (preview.isBlank()) label else "$label(\"$preview\")",
            details = details,
        )
    }

    fun payload(element: JsonElement?): HermesReadablePayload =
        payload(element, MAX_VISIBLE_VALUE_CHARS)

    private fun payload(
        element: JsonElement?,
        maxTextCodePoints: Int,
    ): HermesReadablePayload {
        if (element == null || element is JsonNull) return HermesReadablePayload("", emptyList())
        return payloadNormalized(element.normalizeForPresentation(), maxTextCodePoints)
    }

    private fun payloadNormalized(
        element: JsonElement,
        maxTextCodePoints: Int,
    ): HermesReadablePayload {
        if (element is JsonPrimitive) {
            return HermesReadablePayload(element.asReadableText(maxTextCodePoints), emptyList())
        }
        if (element !is JsonObject) {
            return HermesReadablePayload("", element.toDetailRows())
        }
        val primaryKey = primaryTextKeys.firstOrNull { key ->
            element[key]?.asReadableText(maxTextCodePoints)?.isNotBlank() == true
        }
        val primary = primaryKey?.let { key ->
            payloadNormalized(element.getValue(key), maxTextCodePoints)
        }
            ?: HermesReadablePayload("", emptyList())
        val details = primary.details + element
            .filterKeys { key -> key != primaryKey }
            .let(::JsonObject)
            .toDetailRows()
        return HermesReadablePayload(primary.text, details.distinct())
    }

    fun payloadText(payload: String?): HermesReadablePayload = payload
        ?.takeIf(String::isNotBlank)
        ?.parseJsonOrText()
        ?.let(::payload)
        ?: HermesReadablePayload("", emptyList())

    fun toolOutput(payload: String?): HermesReadablePayload = payload
        ?.takeIf(String::isNotBlank)
        ?.parseJsonOrText()
        ?.let(::toolOutput)
        ?: HermesReadablePayload("", emptyList())

    fun toolOutput(payload: JsonElement?): HermesReadablePayload =
        payload(payload, MAX_LONG_OUTPUT_CODE_POINTS)

    fun toolResult(
        payload: JsonObject?,
        maxTextCodePoints: Int = MAX_VISIBLE_VALUE_CHARS,
    ): HermesReadablePayload {
        if (payload == null || payload.isEmpty()) return HermesReadablePayload("", emptyList())
        return payload(
            JsonObject(
                payload
                    .filterKeys { key -> key !in toolProtocolKeys }
                    .filterValues { value -> readableText(value).isNotBlank() },
            ),
            maxTextCodePoints,
        )
    }

    fun readableText(payload: JsonElement?): String = when (payload) {
        null, JsonNull -> ""
        else -> payload.normalizeForPresentation().readableTextNormalized()
    }

    fun safeText(
        value: String?,
        maxCodePoints: Int = MAX_VISIBLE_VALUE_CHARS,
    ): String? = value
        ?.takeIf(String::isNotBlank)
        ?.let { sanitizeVisibleText(it, maxCodePoints) }
        ?.takeIf(String::isNotBlank)

    fun structuredObject(payload: JsonElement?): JsonObject? =
        payload?.normalizeForPresentation() as? JsonObject

    private fun JsonElement.readableTextNormalized(): String =
        readableTextNormalized(MAX_VISIBLE_VALUE_CHARS)

    private fun JsonElement.readableTextNormalized(maxCodePoints: Int): String = when (this) {
        JsonNull -> ""
        is JsonPrimitive -> sanitizeVisibleText(contentOrNull.orEmpty(), maxCodePoints)
        is JsonArray -> mapNotNull { item ->
            item.asReadableText(maxCodePoints).takeIf(String::isNotBlank)
        }.joinToString("\n").let { sanitizeVisibleText(it, maxCodePoints) }
        is JsonObject -> {
            val primary = primaryTextKeys.firstNotNullOfOrNull { key ->
                get(key)?.asReadableText(maxCodePoints)?.takeIf(String::isNotBlank)
            }
            primary ?: toDetailRows().joinToString("\n") { detail ->
                "${detail.label}: ${detail.value}"
            }.let { sanitizeVisibleText(it, maxCodePoints) }
        }
    }

    private fun String.parseJsonOrText(): JsonElement = when {
        !looksLikeEncodedJson() -> JsonPrimitive(this)
        length > MAX_ENCODED_JSON_CHARS -> JsonPrimitive(OMITTED_VALUE)
        else -> runCatching { json.parseToJsonElement(this) }.getOrElse { JsonPrimitive(this) }
    }

    private fun JsonElement.toDetailRows(
        parent: String? = null,
    ): List<ConversationToolDetailUiModel> = toDetailRowsInternal(parent).take(MAX_DETAIL_ROWS)

    private fun JsonElement.toDetailRowsInternal(
        parent: String?,
    ): List<ConversationToolDetailUiModel> = when (this) {
        JsonNull -> emptyList()
        is JsonPrimitive -> redactedValue(parent, contentOrNull.orEmpty())
            .takeIf(String::isNotBlank)
            ?.let { value ->
                listOf(
                    ConversationToolDetailUiModel(
                        label = parent.orEmpty().ifBlank { "Value" },
                        value = value,
                    ),
                )
            }
            .orEmpty()
        is JsonObject -> entries.flatMap { (key, value) ->
            val label = listOfNotNull(parent, key.humanLabel())
                .joinToString(" · ")
                .compactPreview(MAX_VISIBLE_LABEL_CHARS)
            value.toDetailRowsInternal(label)
        }
        is JsonArray -> flatMapIndexed { index, value ->
            val label = listOfNotNull(parent, "Item ${index + 1}")
                .joinToString(" · ")
                .compactPreview(MAX_VISIBLE_LABEL_CHARS)
            value.toDetailRowsInternal(label)
        }
    }

    private fun JsonElement.readableScalar(label: String? = null): String = when (this) {
        is JsonPrimitive -> redactedValue(label, contentOrNull.orEmpty())
        else -> ""
    }

    private fun JsonElement.asReadableText(
        maxCodePoints: Int = MAX_VISIBLE_VALUE_CHARS,
    ): String = normalizeForPresentation().readableTextNormalized(maxCodePoints)

    private fun String.humanLabel(): String {
        val parts = sanitizeVisibleText(this)
            .compactPreview(MAX_VISIBLE_LABEL_CHARS)
            .replace(Regex("([a-z0-9])([A-Z])"), "$1_$2")
            .split('_', '-', ' ')
            .filter(String::isNotBlank)
        return parts.mapIndexed { index, part ->
            if (index == 0) part.lowercase().replaceFirstChar(Char::titlecase) else part.lowercase()
        }.joinToString(" ").compactPreview(MAX_VISIBLE_LABEL_CHARS)
    }

    private fun toolLabel(name: String?): String {
        val presented = payloadText(name)
        val safeName = presented.details
            .firstOrNull { detail -> detail.label.equals("Name", ignoreCase = true) }
            ?.value
            ?: presented.text.takeIf(String::isNotBlank)
            ?: presented.visibleText().takeIf(String::isNotBlank)
        return safeName
            ?.compactPreview(80)
        ?.split('_')
        ?.filter(String::isNotBlank)
        ?.joinToString(" ") { part -> part.replaceFirstChar(Char::titlecase) }
        ?.takeIf(String::isNotBlank)
        ?: "Tool"
    }

    private fun String.compactPreview(max: Int): String {
        val oneLine = replace(Regex("\\s+"), " ").trim()
        return if (oneLine.length <= max) oneLine else oneLine.take(max - 1) + "…"
    }

    private fun String.truncateCodePoints(maxCodePoints: Int): String {
        require(maxCodePoints > 0) { "Visible text limit must be positive." }
        if (codePointCount(0, length) <= maxCodePoints) return this
        val visibleEnd = offsetByCodePoints(0, maxCodePoints - 1)
        return substring(0, visibleEnd) + "…"
    }

    private fun redactedValue(label: String?, value: String): String {
        val normalized = label.orEmpty()
            .lowercase()
            .replace(Regex("[^a-z0-9]+"), " ")
            .trim()
        val words = normalized.split(' ').filter(String::isNotBlank)
        return if (sensitiveLabelTerms.any(normalized::contains) || "auth" in words) {
            "[redacted]"
        } else {
            sanitizeVisibleText(value)
        }
    }

    private fun sanitizeVisibleText(
        value: String,
        maxCodePoints: Int = MAX_VISIBLE_VALUE_CHARS,
    ): String {
        require(maxCodePoints > 0) { "Visible text limit must be positive." }
        val scanLimit = maxCodePoints * 2
        val boundedInput = if (value.codePointCount(0, value.length) <= scanLimit) {
            value
        } else {
            value.substring(0, value.offsetByCodePoints(0, scanLimit))
        }
        val sanitized = boundedInput
            .replace(authorizationHeader) { match -> "${match.groupValues[1]}[redacted]" }
            .replace(sensitiveCliFlag) { match -> "${match.groupValues[1]}[redacted]" }
            .replace(cliUserFlag) { match -> "${match.groupValues[1]}[redacted]" }
            .replace(urlUserInfo) { match -> "${match.groupValues[1]}[redacted]@" }
            .replace(basicValue, "Basic [redacted]")
            .replace(sensitiveAssignment) { match ->
                "${match.groupValues[1]}${match.groupValues[2]}[redacted]"
            }
            .replace(bearerValue, "Bearer [redacted]")
        return sanitized.truncateCodePoints(maxCodePoints)
    }

    private fun JsonElement.normalizeForPresentation(
        depth: Int = 0,
        budget: PresentationBudget = PresentationBudget(),
    ): JsonElement {
        if (!budget.claim() || depth >= MAX_DECODE_DEPTH) return JsonPrimitive(OMITTED_VALUE)
        return when (this) {
            JsonNull -> JsonNull
            is JsonPrimitive -> {
                if (!isString) {
                    this
                } else {
                    val content = contentOrNull.orEmpty()
                    if (content.length > MAX_ENCODED_JSON_CHARS && content.looksLikeEncodedJson()) {
                        JsonPrimitive(OMITTED_VALUE)
                    } else {
                        val parsed = content
                            .takeIf { encoded -> encoded.looksLikeEncodedJson() }
                            ?.let { encoded -> runCatching { json.parseToJsonElement(encoded) }.getOrNull() }
                        if (parsed == null || parsed == this) this else parsed.normalizeForPresentation(depth + 1, budget)
                    }
                }
            }
            is JsonObject -> {
                val normalized = linkedMapOf<String, JsonElement>()
                for ((key, value) in entries) {
                    if (budget.remaining <= 0) {
                        normalized["additional_data"] = JsonPrimitive(OMITTED_VALUE)
                        break
                    }
                    normalized[key] = value.normalizeForPresentation(depth + 1, budget)
                }
                JsonObject(normalized)
            }
            is JsonArray -> {
                val normalized = mutableListOf<JsonElement>()
                for (value in this) {
                    if (budget.remaining <= 0) {
                        normalized += JsonPrimitive(OMITTED_VALUE)
                        break
                    }
                    normalized += value.normalizeForPresentation(depth + 1, budget)
                }
                JsonArray(normalized)
            }
        }
    }

    private class PresentationBudget(
        var remaining: Int = MAX_PRESENTATION_NODES,
    ) {
        fun claim(): Boolean {
            if (remaining <= 0) return false
            remaining -= 1
            return true
        }
    }

    private fun String.looksLikeEncodedJson(): Boolean {
        val candidate = trim()
        return (candidate.startsWith('{') && candidate.endsWith('}')) ||
            (candidate.startsWith('[') && candidate.endsWith(']')) ||
            (candidate.startsWith('"') && candidate.endsWith('"'))
    }
}
