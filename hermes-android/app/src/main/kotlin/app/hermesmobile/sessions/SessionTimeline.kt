package app.hermesmobile.sessions

import kotlinx.serialization.json.JsonObject

/** A stable-key item in the transcript baseline plus live runtime projection. */
sealed interface SessionTimelineItem {
    val key: String

    data class User(
        override val key: String,
        val text: String,
    ) : SessionTimelineItem

    data class AssistantTurn(
        override val key: String,
        val turnKey: String = key,
        val segmentKind: AssistantSegmentKind? = null,
        val text: String = "",
        val reasoning: String = "",
        val thinking: String = "",
        val statusText: String = "",
        val status: AssistantTurnStatus = AssistantTurnStatus.STREAMING,
        val error: String? = null,
    ) : SessionTimelineItem

    data class ToolActivity(
        override val key: String,
        val toolId: String,
        val name: String? = null,
        val context: String? = null,
        val args: String? = null,
        val output: String = "",
        val result: String? = null,
        val summary: String? = null,
        val diff: String? = null,
        val durationSeconds: Double? = null,
        val status: ToolActivityStatus = ToolActivityStatus.RUNNING,
        val error: String? = null,
        val payload: JsonObject = JsonObject(emptyMap()),
    ) : SessionTimelineItem

    /** Fixes the first result/completion occurrence without duplicating the tool node. */
    data class ToolResultActivity(
        override val key: String,
        val toolKey: String,
    ) : SessionTimelineItem

    data class ProcessActivity(
        override val key: String,
        val turnKey: String,
    ) : SessionTimelineItem

    data class StatusActivity(
        override val key: String,
        val kind: String? = null,
        val text: String,
    ) : SessionTimelineItem
}

enum class AssistantSegmentKind {
    REASONING,
    THINKING,
    ACTIVITY,
    RESPONSE,
}

enum class AssistantTurnStatus {
    STREAMING,
    COMPLETE,
    ERROR,
}

enum class ToolActivityStatus {
    RUNNING,
    COMPLETE,
    ERROR,
    INTERRUPTED,
    UNKNOWN,
}
