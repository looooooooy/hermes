package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import java.security.MessageDigest
import java.util.Base64
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull

internal const val OBSERVER_V2_MAX_SAFE_INTEGER = 9_007_199_254_740_991L
internal const val OBSERVER_V2_MAX_NESTING_DEPTH = 32
internal const val OBSERVER_V2_MAX_OBJECT_FIELDS = 1_024
internal const val OBSERVER_V2_MAX_ARRAY_ITEMS = 1_024
internal const val OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS = 4_096
internal const val OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS = 131_072

internal fun String.hasAtMostObserverV2CodePoints(maximum: Int): Boolean =
    codePointCount(0, length) <= maximum

object CloudObserverEventContract {
    val eventTypes: Set<String>
        get() = GeneratedObserverV2Authority.eventTypes

    private val v1EventTypes = setOf(
        "message.start",
        "message.delta",
        "message.complete",
        "agent.terminal.output",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    )

    fun eventTypes(contractVersion: Int): Set<String> = when (contractVersion) {
        1 -> v1EventTypes
        2 -> eventTypes
        else -> emptySet()
    }

    fun accepts(type: String, payload: JsonObject?): Boolean = accepts(2, type, payload)

    fun accepts(contractVersion: Int, type: String, payload: JsonObject?): Boolean {
        payload ?: return false
        if (type !in eventTypes(contractVersion)) return false
        val shapeIsValid = when (contractVersion) {
            1 -> acceptsV1(type, payload)
            2 -> acceptsV2(type, payload)
            else -> false
        }
        return shapeIsValid && (
            contractVersion != 2 || payload.isDisplaySafeObserverValue(depth = 0)
            )
    }

    internal fun acceptsDisplaySafeV2(value: JsonElement): Boolean =
        value.isDisplaySafeObserverValue(depth = 0)

    fun decodeV2SessionEvent(value: JsonObject): GatewayEvent? {
        if (!V2_EVENT_FIELDS.containsAll(value.keys) || !value.keys.containsAll(REQUIRED_V2_EVENT_FIELDS)) {
            return null
        }
        if (value.requiredInt("observer_contract") != 2) return null
        val profile = value.requiredBoundedString("profile", 128)
            ?.takeIf(PROFILE_PATTERN::matches)
            ?: return null
        val generation = value.requiredBoundedString("runtime_generation", 128) ?: return null
        val sessionKey = value.requiredBoundedString("session_key", 256)?.let(::SessionKey)
            ?: return null
        val runtimeSessionId = value.requiredBoundedString("session_id", 256)
            ?.let(::RuntimeSessionId)
            ?: return null
        val type = value.requiredString("type")?.takeIf { it in eventTypes } ?: return null
        val sequence = value.requiredPositiveLong("event_sequence") ?: return null
        val sequenceStart = when (val raw = value["event_sequence_start"]) {
            null -> sequence
            is JsonPrimitive -> raw.takeUnless { it.isString }?.longOrNull
                ?.takeIf { it in 1..sequence && it <= OBSERVER_V2_MAX_SAFE_INTEGER }
                ?: return null
            else -> return null
        }
        if (sequenceStart < sequence && type !in MERGEABLE_V2_EVENT_TYPES) return null
        val payload = value["payload"] as? JsonObject ?: return null
        if (!accepts(2, type, payload)) return null
        if (type in GeneratedObserverV2Authority.lifecycleEventTypes) {
            val first = payload.requiredPositiveLong("first_event_sequence") ?: return null
            if (first > sequence) return null
        }
        val extensions = value["extensions"]
        if (extensions != null && !extensions.isValidExtensions()) return null
        return GatewayEvent(
            type = type,
            runtimeSessionId = runtimeSessionId,
            payload = payload,
            sessionKey = sessionKey,
            eventSequence = sequence,
            observerContractVersion = 2,
            profile = profile,
            runtimeGeneration = generation,
            eventSequenceStart = sequenceStart,
            transportDigest = canonicalTransportDigest(value),
        )
    }

    private fun canonicalTransportDigest(value: JsonObject): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.canonicalJson().encodeToByteArray())
            .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }

    private fun JsonElement.canonicalJson(): String = when (this) {
        is JsonObject -> entries
            .sortedBy(Map.Entry<String, JsonElement>::key)
            .joinToString(prefix = "{", postfix = "}") { (key, value) ->
                "${JsonPrimitive(key)}:${value.canonicalJson()}"
            }
        is JsonArray -> joinToString(prefix = "[", postfix = "]") { it.canonicalJson() }
        else -> toString()
    }

    private fun acceptsV1(type: String, payload: JsonObject): Boolean = when (type) {
        "message.start" -> payload.keys.all { it == "message_id" || it == "role" } &&
            payload.optionalBoundedString("message_id", 256) &&
            payload.optionalExactString("role", "assistant")
        "message.delta", "reasoning.delta", "thinking.delta" ->
            payload.keys == setOf("text") &&
                payload.hasBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true)
        "message.complete" -> payload.keys.all { it in MESSAGE_COMPLETE_FIELDS } &&
            payload.requiredString("status") in setOf("complete", "error") &&
            payload.optionalBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true) &&
            payload.optionalNullableString("error", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS)
        "agent.terminal.output" -> payload.keys.all { it in V1_TERMINAL_OUTPUT_FIELDS } &&
            payload.hasBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true) &&
            payload.optionalBoundedString("process_id", 256) &&
            payload.optionalEnumString("stream", setOf("stdout", "stderr")) &&
            payload.optionalNonNegativeLong("sequence")
        "status.update" -> payload.validStatusUpdate()
        "tool.output.delta" -> payload.keys.all { it in V1_TOOL_OUTPUT_FIELDS } &&
            payload.hasBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true) &&
            payload.optionalBoundedString("tool_call_id", 256) &&
            payload.optionalBoundedString("tool_name", 256) &&
            payload.optionalNonNegativeLong("sequence")
        else -> false
    }

    private fun acceptsV2(type: String, payload: JsonObject): Boolean = when (type) {
        "message.start" -> payload.keys.all { it == "message_id" || it == "role" } &&
            payload.optionalBoundedString("message_id", 256) &&
            payload.optionalExactString("role", "assistant")
        "message.delta", "reasoning.delta", "thinking.delta" ->
            payload.keys == setOf("text") &&
                payload.hasBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true)
        "message.complete" -> payload.keys.all { it in MESSAGE_COMPLETE_FIELDS } &&
            payload.requiredString("status") in setOf("complete", "error") &&
            payload.optionalBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true) &&
            payload.optionalNullableString("error", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS)
        "agent.terminal.output" -> payload.keys.all { it in V2_TERMINAL_OUTPUT_FIELDS } &&
            payload.requiredBoundedString("turn_id", 256) != null &&
            payload.requiredBoundedString("process_id", 256) != null &&
            payload.requiredString("stream") in setOf("stdout", "stderr") &&
            payload.hasBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true) &&
            payload.optionalNonNegativeLong("sequence")
        "status.update" -> payload.validStatusUpdate()
        "tool.output.delta" -> payload.keys.all { it in V2_TOOL_OUTPUT_FIELDS } &&
            payload.requiredBoundedString("turn_id", 256) != null &&
            payload.requiredBoundedString("tool_call_id", 256) != null &&
            payload.hasBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true) &&
            payload.optionalBoundedString("tool_name", 256) &&
            payload.optionalNonNegativeLong("sequence")
        "todo.update" -> payload.validTodoUpdate()
        "subagent.update" -> payload.validSubagentUpdate()
        "tool.update" -> payload.validToolUpdate()
        "terminal.update" -> payload.validTerminalUpdate()
        else -> false
    }

    private fun JsonObject.validTodoUpdate(): Boolean {
        if (!validLifecycleIdentity("section_id")) return false
        return when (requiredString("operation")) {
            "delete" -> keys == TODO_DELETE_FIELDS
            "upsert" -> {
                if (keys.any { it !in TODO_UPSERT_FIELDS }) return false
                if (requiredString("status") !in TODO_STATUSES) return false
                val items = get("items") as? JsonArray ?: return false
                if (items.isEmpty() || items.size > 256) return false
                val identities = HashSet<String>()
                items.all { raw ->
                    val item = raw as? JsonObject ?: return@all false
                    if (item.keys != TODO_ITEM_FIELDS) return@all false
                    val id = item.requiredBoundedString("id", 256) ?: return@all false
                    identities.add(id) &&
                        item.requiredBoundedString("label", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS) != null &&
                        item.requiredString("status") in TODO_STATUSES
                }
            }
            else -> false
        }
    }

    private fun JsonObject.validSubagentUpdate(): Boolean {
        if (!validLifecycleIdentity("subagent_id")) return false
        return when (requiredString("operation")) {
            "delete" -> keys == SUBAGENT_DELETE_FIELDS
            "upsert" -> {
                if (keys.any { it !in SUBAGENT_UPSERT_FIELDS }) return false
                if (!requiredNullableIdentifier("parent_subagent_id")) return false
                if (requiredBoundedString("name", 160) == null) return false
                if (!hasBoundedString("goal", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS, allowEmpty = true)) return false
                if (!requiredNullableString("summary", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS)) return false
                if (requiredString("status") !in SUBAGENT_STATUSES) return false
                if (!optionalBoundedString("model", 160)) return false
                if (!optionalNonNegativeLong("duration_ms")) return false
                if (!optionalNonNegativeLong("api_calls")) return false
                val progress = get("progress")
                if (progress != null && !progress.isValidProgress()) return false
                val tokenCounts = get("token_counts")
                if (tokenCounts != null && !tokenCounts.isValidTokenCounts()) return false
                true
            }
            else -> false
        }
    }

    private fun JsonObject.validToolUpdate(): Boolean {
        if (!validLifecycleIdentity("tool_call_id")) return false
        return when (requiredString("operation")) {
            "delete" -> keys == TOOL_DELETE_FIELDS
            "upsert" -> keys.all { it in TOOL_UPSERT_FIELDS } &&
                requiredString("status") in LIFECYCLE_STATUSES &&
                requiredBoundedString("name", 160) != null &&
                optionalBoundedString("call_label", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS) &&
                optionalBoundedString("summary", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS, allowEmpty = true) &&
                optionalNonNegativeLong("duration_ms")
            else -> false
        }
    }

    private fun JsonObject.validTerminalUpdate(): Boolean {
        if (!validLifecycleIdentity("process_id")) return false
        return when (requiredString("operation")) {
            "delete" -> keys == TERMINAL_DELETE_FIELDS
            "upsert" -> {
                if (keys.any { it !in TERMINAL_UPSERT_FIELDS }) return false
                val status = requiredString("status")
                if (status !in LIFECYCLE_STATUSES) return false
                val exitCode = (get("exit_code") as? JsonPrimitive)
                    ?.takeUnless { it.isString }
                    ?.intOrNull
                if ("exit_code" in this && exitCode == null) return false
                if (status == "completed" && exitCode != 0) return false
                if (status == "failed" && (exitCode == null || exitCode == 0)) return false
                if (status in setOf("running", "interrupted", "unknown") && exitCode != null) return false
                optionalBoundedString("summary", OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS, allowEmpty = true) &&
                    optionalNonNegativeLong("duration_ms")
            }
            else -> false
        }
    }

    private fun JsonObject.validLifecycleIdentity(entityField: String): Boolean =
        requiredBoundedString("turn_id", 256) != null &&
            requiredBoundedString(entityField, 256) != null &&
            requiredPositiveLong("revision") != null &&
            requiredPositiveLong("first_event_sequence") != null

    private fun JsonObject.validStatusUpdate(): Boolean {
        val status = requiredBoundedString("status", 64) ?: return false
        val running = (get("running") as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.booleanOrNull
            ?: return false
        return keys.all { it in STATUS_FIELDS } &&
            running == (status in RUNNING_STATUSES) &&
            optionalBoundedString("text", OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS, allowEmpty = true)
    }

    private fun JsonObject.hasBoundedString(key: String, maxLength: Int, allowEmpty: Boolean): Boolean =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content?.let {
            (allowEmpty || it.isNotEmpty()) && it.hasAtMostObserverV2CodePoints(maxLength)
        } == true

    private fun JsonObject.requiredString(key: String): String? =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf(String::isNotBlank)

    private fun JsonObject.requiredBoundedString(key: String, maxLength: Int): String? =
        requiredString(key)?.takeIf { it.hasAtMostObserverV2CodePoints(maxLength) }

    private fun JsonObject.requiredPositiveLong(key: String): Long? =
        (get(key) as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.longOrNull
            ?.takeIf { it in 1..OBSERVER_V2_MAX_SAFE_INTEGER }

    private fun JsonObject.requiredInt(key: String): Int? =
        (get(key) as? JsonPrimitive)?.takeUnless { it.isString }?.intOrNull

    private fun JsonObject.optionalBoundedString(
        key: String,
        maxLength: Int,
        allowEmpty: Boolean = false,
    ): Boolean = key !in this || hasBoundedString(key, maxLength, allowEmpty)

    private fun JsonObject.optionalExactString(key: String, expected: String): Boolean =
        key !in this || requiredString(key) == expected

    private fun JsonObject.optionalEnumString(key: String, expected: Set<String>): Boolean =
        key !in this || requiredString(key) in expected

    private fun JsonObject.optionalNullableString(key: String, maxLength: Int): Boolean =
        when (val value = get(key)) {
            null, JsonNull -> true
            is JsonPrimitive -> value.isString && value.content.hasAtMostObserverV2CodePoints(maxLength)
            else -> false
        }

    private fun JsonObject.requiredNullableString(key: String, maxLength: Int): Boolean =
        key in this && optionalNullableString(key, maxLength)

    private fun JsonObject.requiredNullableIdentifier(key: String): Boolean = when (val value = get(key)) {
        JsonNull -> true
        is JsonPrimitive -> value.takeIf { it.isString }?.content
            ?.let { it.isNotBlank() && it.hasAtMostObserverV2CodePoints(256) } == true
        else -> false
    }

    private fun JsonObject.optionalNonNegativeLong(key: String): Boolean =
        key !in this || (get(key) as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.longOrNull
            ?.let { it in 0..OBSERVER_V2_MAX_SAFE_INTEGER } == true

    private fun JsonObject.isValidProgress(): Boolean =
        keys == setOf("current", "total") &&
            (get("current") as? JsonPrimitive)?.takeUnless { it.isString }?.longOrNull
                ?.takeIf { it in 0..OBSERVER_V2_MAX_SAFE_INTEGER }
                ?.let { current ->
                    (get("total") as? JsonPrimitive)?.takeUnless { it.isString }?.longOrNull
                        ?.let { total ->
                            total in 1..OBSERVER_V2_MAX_SAFE_INTEGER && current <= total
                        }
                } == true

    private fun JsonObject.isValidTokenCounts(): Boolean =
        keys == setOf("input", "output", "reasoning") &&
            values.all { raw ->
                (raw as? JsonPrimitive)
                    ?.takeUnless { it.isString }
                    ?.longOrNull
                    ?.let { it in 0..OBSERVER_V2_MAX_SAFE_INTEGER } == true
            }

    private fun JsonElement?.isValidProgress(): Boolean =
        (this as? JsonObject)?.isValidProgress() == true

    private fun JsonElement?.isValidTokenCounts(): Boolean =
        (this as? JsonObject)?.isValidTokenCounts() == true

    private fun JsonElement?.isValidExtensions(): Boolean {
        val value = this as? JsonObject ?: return false
        return value.size <= 16 && value.all { (key, raw) ->
            EXTENSION_KEY_PATTERN.matches(key) &&
                raw is JsonObject &&
                raw.isDisplaySafeExtensionValue(depth = 0)
        }
    }

    private fun JsonElement.isDisplaySafeObserverValue(depth: Int): Boolean {
        if (depth > OBSERVER_V2_MAX_NESTING_DEPTH) return false
        return when (this) {
            JsonNull -> true
            is JsonObject -> size <= OBSERVER_V2_MAX_OBJECT_FIELDS && values.all {
                it.isDisplaySafeObserverValue(depth + 1)
            }
            is JsonArray -> size <= OBSERVER_V2_MAX_ARRAY_ITEMS && all {
                it.isDisplaySafeObserverValue(depth + 1)
            }
            is JsonPrimitive -> when {
                isString -> content.hasAtMostObserverV2CodePoints(OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS) &&
                    content.none { it.isUnsafeObserverControl() } &&
                    !content.looksLikeSensitiveObserverValue()
                booleanOrNull != null -> true
                longOrNull != null -> longOrNull in
                    -OBSERVER_V2_MAX_SAFE_INTEGER..OBSERVER_V2_MAX_SAFE_INTEGER
                else -> doubleOrNull?.let {
                    it.isFinite() && kotlin.math.abs(it) <= OBSERVER_V2_MAX_SAFE_INTEGER.toDouble()
                } == true
            }
        }
    }

    private fun JsonElement.isDisplaySafeExtensionValue(depth: Int): Boolean {
        if (depth > OBSERVER_V2_MAX_NESTING_DEPTH) return false
        return when (this) {
            JsonNull -> true
            is JsonObject -> size <= OBSERVER_V2_MAX_OBJECT_FIELDS && all { (key, value) ->
                key.length in 1..MAX_EXTENSION_KEY_LENGTH &&
                    key.none { it.isUnsafeObserverControl() } &&
                    !key.isSensitiveExtensionKey() &&
                    value.isDisplaySafeExtensionValue(depth + 1)
            }
            is JsonArray -> size <= OBSERVER_V2_MAX_ARRAY_ITEMS && all {
                it.isDisplaySafeExtensionValue(depth + 1)
            }
            is JsonPrimitive -> when {
                isString -> content.hasAtMostObserverV2CodePoints(OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS) &&
                    content.none { it.isUnsafeObserverControl() } &&
                    !content.looksLikeSensitiveObserverValue()
                booleanOrNull != null -> true
                longOrNull != null -> longOrNull in
                    -OBSERVER_V2_MAX_SAFE_INTEGER..OBSERVER_V2_MAX_SAFE_INTEGER
                else -> doubleOrNull?.let {
                    it.isFinite() && kotlin.math.abs(it) <= OBSERVER_V2_MAX_SAFE_INTEGER.toDouble()
                } == true
            }
        }
    }

    private fun String.isSensitiveExtensionKey(): Boolean {
        val compact = lowercase().filter(Char::isLetterOrDigit)
        return SENSITIVE_EXTENSION_KEY_MARKERS.any(compact::contains)
    }

    private fun String.looksLikeSensitiveObserverValue(): Boolean {
        val candidate = trim()
        val normalized = candidate.lowercase()
        return SENSITIVE_VALUE_PREFIXES.any(normalized::startsWith) ||
            candidate.containsBasicCredential() ||
            SENSITIVE_ASSIGNMENT_PATTERN.containsMatchIn(candidate) ||
            candidate.containsJwtCredential() ||
            AWS_ACCESS_KEY_PATTERN.containsMatchIn(candidate) ||
            GOOGLE_API_KEY_PATTERN.containsMatchIn(candidate)
    }

    private fun String.containsBasicCredential(): Boolean =
        BASIC_VALUE_PATTERN.findAll(this).any { match ->
            val decoded = match.groupValues[1].decodeBasicValueOrNull()
                ?: return@any false
            decoded.indexOf(':'.code.toByte()) > 0
        }

    private fun String.decodeBasicValueOrNull(): ByteArray? =
        runCatching { Base64.getDecoder().decode(this) }.getOrNull()

    private fun String.containsJwtCredential(): Boolean =
        JWT_COMPACT_VALUE_PATTERN.findAll(this).any { match ->
            val header = match.groupValues[1].decodeJwtJsonObjectOrNull()
                ?: return@any false
            match.groupValues[2].decodeJwtJsonObjectOrNull()
                ?: return@any false
            val algorithm = (header["alg"] as? JsonPrimitive)
                ?.takeIf(JsonPrimitive::isString)
                ?.content
                ?.trim()
                ?.takeIf(String::isNotEmpty)
                ?: return@any false
            algorithm.isNotEmpty() &&
                match.groupValues[3].decodeBase64UrlOrNull()?.isNotEmpty() == true
        }

    private fun String.decodeJwtJsonObjectOrNull(): JsonObject? =
        decodeBase64UrlOrNull()?.let { decoded ->
            runCatching {
                Json.parseToJsonElement(decoded.decodeToString(throwOnInvalidSequence = true)) as? JsonObject
            }.getOrNull()
        }

    private fun String.decodeBase64UrlOrNull(): ByteArray? =
        runCatching { Base64.getUrlDecoder().decode(this) }.getOrNull()

    private fun Char.isUnsafeObserverControl(): Boolean =
        code < 0x20 && this != '\n' && this != '\r' && this != '\t'

    private val PROFILE_PATTERN = Regex("^[A-Za-z0-9_.-]+$")
    private val EXTENSION_KEY_PATTERN = Regex("^[a-z][a-z0-9]*(\\.[a-z0-9][a-z0-9-]*)+$")
    private val BASIC_VALUE_PATTERN = Regex(
        "(?:^|[^A-Za-z0-9])Basic\\s+([A-Za-z0-9+/]+={0,2})(?=$|[^A-Za-z0-9+/=])",
        RegexOption.IGNORE_CASE,
    )
    private val JWT_COMPACT_VALUE_PATTERN = Regex(
        "(?:^|[^A-Za-z0-9_-])([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)\\." +
            "([A-Za-z0-9_-]+)(?=$|[^A-Za-z0-9_-])",
    )
    private val SENSITIVE_ASSIGNMENT_PATTERN = Regex(
        "(?:^|[^A-Za-z0-9])(?:password|passwd|token|secret|api[_-]?key|client[_-]?secret|" +
            "access[_-]?key|authorization)\\s*[:=]\\s*\\S+",
        RegexOption.IGNORE_CASE,
    )
    private val AWS_ACCESS_KEY_PATTERN = Regex(
        "(?:^|[^A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)" +
            "[A-Z0-9]{16}(?:$|[^A-Z0-9])",
    )
    private val GOOGLE_API_KEY_PATTERN = Regex(
        "(?:^|[^A-Za-z0-9_-])AIza[0-9A-Za-z_-]{20,}(?:$|[^A-Za-z0-9_-])",
    )
    private val SENSITIVE_EXTENSION_KEY_MARKERS = setOf(
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "authorization",
        "cookie",
        "apikey",
        "privatekey",
        "rawargs",
        "toolargs",
        "arguments",
        "approvalpayload",
        "rawoutput",
        "tooloutput",
        "terminaloutput",
    )
    private val SENSITIVE_VALUE_PREFIXES = setOf(
        "bearer ",
        "sk-",
        "sk_live_",
        "rk_live_",
        "ghp_",
        "github_pat_",
        "xoxb-",
        "xoxp-",
        "hf_",
        "npm_",
        "pypi-",
        "-----begin private key-----",
        "-----begin rsa private key-----",
        "-----begin ec private key-----",
    )
    private const val MAX_EXTENSION_KEY_LENGTH = 128
    private val MERGEABLE_V2_EVENT_TYPES = setOf(
        "message.delta",
        "agent.terminal.output",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    )
    private val RUNNING_STATUSES = setOf("running", "working", "streaming")
    private val TODO_STATUSES = setOf("pending", "in_progress", "completed", "cancelled")
    private val SUBAGENT_STATUSES = setOf("queued", "waiting", "running", "completed", "failed", "interrupted")
    private val LIFECYCLE_STATUSES = setOf("running", "completed", "failed", "interrupted", "unknown")
    private val MESSAGE_COMPLETE_FIELDS = setOf("text", "status", "error")
    private val STATUS_FIELDS = setOf("status", "running", "text")
    private val V1_TERMINAL_OUTPUT_FIELDS = setOf("process_id", "stream", "text", "sequence")
    private val V2_TERMINAL_OUTPUT_FIELDS = V1_TERMINAL_OUTPUT_FIELDS + "turn_id"
    private val V1_TOOL_OUTPUT_FIELDS = setOf("tool_call_id", "tool_name", "text", "sequence")
    private val V2_TOOL_OUTPUT_FIELDS = V1_TOOL_OUTPUT_FIELDS + "turn_id"
    private val TODO_ITEM_FIELDS = setOf("id", "label", "status")
    private val LIFECYCLE_FIELDS = setOf("turn_id", "revision", "first_event_sequence", "operation")
    private val TODO_DELETE_FIELDS = LIFECYCLE_FIELDS + "section_id"
    private val TODO_UPSERT_FIELDS = TODO_DELETE_FIELDS + setOf("status", "items")
    private val SUBAGENT_DELETE_FIELDS = LIFECYCLE_FIELDS + "subagent_id"
    private val SUBAGENT_UPSERT_FIELDS = SUBAGENT_DELETE_FIELDS + setOf(
        "parent_subagent_id", "name", "goal", "summary", "status", "model", "duration_ms",
        "progress", "token_counts", "api_calls",
    )
    private val TOOL_DELETE_FIELDS = LIFECYCLE_FIELDS + "tool_call_id"
    private val TOOL_UPSERT_FIELDS = TOOL_DELETE_FIELDS + setOf(
        "status", "name", "call_label", "summary", "duration_ms",
    )
    private val TERMINAL_DELETE_FIELDS = LIFECYCLE_FIELDS + "process_id"
    private val TERMINAL_UPSERT_FIELDS = TERMINAL_DELETE_FIELDS + setOf(
        "status", "exit_code", "summary", "duration_ms",
    )
    private val V2_EVENT_FIELDS = setOf(
        "observer_contract", "profile", "runtime_generation", "session_key", "session_id", "type",
        "event_sequence_start", "event_sequence", "payload", "extensions",
    )
    private val REQUIRED_V2_EVENT_FIELDS = setOf(
        "observer_contract", "profile", "runtime_generation", "session_key", "session_id", "type",
        "event_sequence", "payload",
    )
}
