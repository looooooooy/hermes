package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import kotlinx.serialization.SerializationException
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

class GatewayEvent(
    val type: String,
    val runtimeSessionId: RuntimeSessionId?,
    val payload: JsonObject?,
    val sessionKey: SessionKey? = null,
    val eventSequence: Long? = null,
    val observerContractVersion: Int? = null,
    val profile: String? = null,
    val runtimeGeneration: String? = null,
    val eventSequenceStart: Long? = null,
    val transportDigest: String? = null,
) {
    override fun toString(): String =
        "GatewayEvent(type=$type, runtimeSessionId=$runtimeSessionId, payloadKeys=${payload?.keys})"
}

data class JsonRpcError(
    val code: Int,
    val message: String,
    val data: JsonElement?,
)

sealed interface JsonRpcInbound {
    data class Result(
        val id: Long,
        val result: JsonElement,
    ) : JsonRpcInbound

    data class Error(
        val id: Long?,
        val error: JsonRpcError,
    ) : JsonRpcInbound

    data class Event(
        val event: GatewayEvent,
    ) : JsonRpcInbound

    data class Notification(
        val method: String,
        val params: JsonObject?,
    ) : JsonRpcInbound

    data object Invalid : JsonRpcInbound
}

class JsonRpcCodec(
    private val json: Json = Json { ignoreUnknownKeys = true },
) {
    fun encodeRequest(
        id: Long,
        method: String,
        params: JsonObject = JsonObject(emptyMap()),
    ): String {
        require(id > 0) { "JSON-RPC request id must be positive." }
        require(method.isNotBlank()) { "JSON-RPC method is required." }
        return json.encodeToString(
            buildJsonObject {
                put("jsonrpc", "2.0")
                put("id", id)
                put("method", method)
                put("params", params)
            },
        )
    }

    fun decode(document: String): JsonRpcInbound {
        val frameBytes = runCatching {
            document.encodeToByteArray(throwOnInvalidSequence = true).size
        }.getOrNull() ?: return JsonRpcInbound.Invalid
        if (frameBytes > MAX_FRAME_BYTES) return JsonRpcInbound.Invalid
        val rootElement = try {
            json.parseToJsonElement(document)
        } catch (_: SerializationException) {
            return JsonRpcInbound.Invalid
        } catch (_: IllegalArgumentException) {
            return JsonRpcInbound.Invalid
        }
        if (!rootElement.hasValidContractShape()) return JsonRpcInbound.Invalid
        val root = runCatching { rootElement.jsonObject }.getOrNull()
            ?: return JsonRpcInbound.Invalid
        if (root.string("jsonrpc") != "2.0") return JsonRpcInbound.Invalid

        val method = root.string("method")
        if (method != null) {
            val params = root["params"] as? JsonObject
            return if (method == EVENT_METHOD) {
                decodeEvent(params)
            } else if (method.isNotBlank()) {
                JsonRpcInbound.Notification(method, params)
            } else {
                JsonRpcInbound.Invalid
            }
        }

        val id = (root["id"] as? JsonPrimitive)?.longOrNull
        val error = root["error"] as? JsonObject
        if (error != null) {
            val code = (error["code"] as? JsonPrimitive)?.intOrNull
                ?: return JsonRpcInbound.Invalid
            val message = error.string("message")?.takeIf(String::isNotBlank)
                ?: return JsonRpcInbound.Invalid
            return JsonRpcInbound.Error(
                id = id,
                error = JsonRpcError(code, message, error["data"]),
            )
        }

        val result = root["result"] ?: return JsonRpcInbound.Invalid
        return if (id != null) {
            JsonRpcInbound.Result(id, result)
        } else {
            JsonRpcInbound.Invalid
        }
    }

    private fun decodeEvent(params: JsonObject?): JsonRpcInbound {
        params ?: return JsonRpcInbound.Invalid
        if ("observer_contract" in params) {
            return CloudObserverEventContract.decodeV2SessionEvent(params)
                ?.let(JsonRpcInbound::Event)
                ?: JsonRpcInbound.Invalid
        }
        val type = params.string("type")?.takeIf(String::isNotBlank)
            ?: return JsonRpcInbound.Invalid
        val runtimeSessionId = params.string("session_id")
            ?.takeIf(String::isNotBlank)
            ?.let(::RuntimeSessionId)
        val sessionKeyElement = params["session_key"]
        val sessionKey = when (sessionKeyElement) {
            null -> null
            is JsonPrimitive -> sessionKeyElement
                .takeIf { it.isString }
                ?.content
                ?.takeIf(String::isNotBlank)
                ?.let(::SessionKey)
                ?: return JsonRpcInbound.Invalid
            else -> return JsonRpcInbound.Invalid
        }
        val eventSequenceElement = params["event_sequence"]
        val eventSequence = when (eventSequenceElement) {
            null -> null
            is JsonPrimitive -> eventSequenceElement
                .takeUnless { it.isString }
                ?.longOrNull
                ?.takeIf { it > 0 }
                ?: return JsonRpcInbound.Invalid
            else -> return JsonRpcInbound.Invalid
        }
        val payloadElement = params["payload"]
        val payload = when (payloadElement) {
            null -> null
            is JsonObject -> payloadElement
            else -> return JsonRpcInbound.Invalid
        }
        return JsonRpcInbound.Event(
            GatewayEvent(
                type = type,
                runtimeSessionId = runtimeSessionId,
                payload = payload,
                sessionKey = sessionKey,
                eventSequence = eventSequence,
            ),
        )
    }

    private fun JsonObject.string(key: String): String? =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun JsonElement.hasValidContractShape(): Boolean {
        val pending = ArrayDeque<Pair<JsonElement, Int>>()
        pending.add(this to 0)
        while (pending.isNotEmpty()) {
            val (element, depth) = pending.removeLast()
            if (depth > MAX_NESTING_DEPTH) return false
            when (element) {
                is JsonObject -> {
                    if (element.size > MAX_OBJECT_FIELDS) return false
                    element.forEach { (key, value) ->
                        if (!key.isContractString()) return false
                        pending.add(value to depth + 1)
                    }
                }
                is JsonArray -> {
                    if (element.size > MAX_ARRAY_ITEMS) return false
                    element.forEach { pending.add(it to depth + 1) }
                }
                is JsonPrimitive -> {
                    if (element.isString && !element.content.isContractString()) return false
                }
            }
        }
        return true
    }

    private fun String.isContractString(): Boolean {
        val bytes = runCatching {
            encodeToByteArray(throwOnInvalidSequence = true).size
        }.getOrNull() ?: return false
        return bytes <= MAX_STRING_BYTES
    }

    companion object {
        const val MAX_FRAME_BYTES = 256 * 1024
        const val MAX_FRAME_CHARS = MAX_FRAME_BYTES
        const val MAX_STRING_BYTES = 128 * 1024
        const val MAX_NESTING_DEPTH = 32
        const val MAX_OBJECT_FIELDS = 1024
        const val MAX_ARRAY_ITEMS = 1024
        private const val EVENT_METHOD = "event"
    }
}
