package app.hermesmobile.sessions

/**
 * Lifecycle shared by canonical transcript sections.
 *
 * PENDING has not started, RUNNING is active non-text work, STREAMING is active
 * text, and COMPLETE/INTERRUPTED/ERROR are terminal for a given stable key.
 */
enum class HermesConversationSectionStatus {
    PENDING,
    RUNNING,
    STREAMING,
    COMPLETE,
    INTERRUPTED,
    ERROR,
}

/**
 * A source revision for content stored under a stable section key.
 *
 * Historical and current realtime projections do not expose source revisions,
 * so they use [Unversioned]. A future producer may use [Numbered], starting at
 * zero and increasing whenever it replaces content under the same key.
 */
sealed interface HermesConversationSectionRevision {
    data object Unversioned : HermesConversationSectionRevision

    data class Numbered(val value: Long) : HermesConversationSectionRevision {
        init {
            require(value >= 0) { "Section revision must be non-negative" }
        }
    }
}

/** Identity and lifecycle metadata common to every canonical section. */
data class HermesConversationSectionMetadata(
    val key: String,
    val status: HermesConversationSectionStatus,
    val revision: HermesConversationSectionRevision = HermesConversationSectionRevision.Unversioned,
) {
    init {
        require(key.isNotBlank()) { "Section key must not be blank" }
    }
}

enum class HermesConversationSectionKind {
    USER_PROMPT,
    TODO,
    THINKING,
    TOOL_GROUP,
    SUBAGENTS,
    ACTIVITY,
    RESPONSE_BOUNDARY,
    ASSISTANT_RESPONSE,
    EVENT,
    DIFF,
    ERROR,
    TOKEN_SUMMARY,
    PENDING_INPUT,
}

/**
 * One stable-key block in a Hermes conversation turn.
 *
 * Section order is the order in [CanonicalConversationSections]; kind does not
 * impose a global sort because Hermes may interleave narration, tools, and
 * inline diffs as a turn progresses.
 */
sealed interface HermesConversationSection {
    val metadata: HermesConversationSectionMetadata
    val kind: HermesConversationSectionKind

    val key: String
        get() = metadata.key
    val status: HermesConversationSectionStatus
        get() = metadata.status
    val revision: HermesConversationSectionRevision
        get() = metadata.revision

    data class UserPrompt(
        override val metadata: HermesConversationSectionMetadata,
        val prompt: ConversationPromptUiModel,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.USER_PROMPT
    }

    data class Todo(
        override val metadata: HermesConversationSectionMetadata,
        val items: List<HermesConversationTodoItem>,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.TODO
    }

    data class Thinking(
        override val metadata: HermesConversationSectionMetadata,
        val text: String,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.THINKING
    }

    data class ToolGroup(
        override val metadata: HermesConversationSectionMetadata,
        val tools: List<ConversationToolUiModel>,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.TOOL_GROUP
    }

    data class Subagents(
        override val metadata: HermesConversationSectionMetadata,
        val subagents: List<HermesConversationSubagent>,
        val moaReferences: List<HermesConversationMoaReference>,
        val moaProgress: HermesConversationMoaProgress? = null,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.SUBAGENTS
    }

    data class Activity(
        override val metadata: HermesConversationSectionMetadata,
        val text: String,
        val tone: HermesConversationActivityTone = HermesConversationActivityTone.INFO,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.ACTIVITY
    }

    data class ResponseBoundary(
        override val metadata: HermesConversationSectionMetadata,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.RESPONSE_BOUNDARY
    }

    data class AssistantResponse(
        override val metadata: HermesConversationSectionMetadata,
        val text: String,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.ASSISTANT_RESPONSE
    }

    data class Event(
        override val metadata: HermesConversationSectionMetadata,
        val text: String,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.EVENT
    }

    data class Diff(
        override val metadata: HermesConversationSectionMetadata,
        val text: String,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.DIFF
    }

    data class Error(
        override val metadata: HermesConversationSectionMetadata,
        val message: String,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.ERROR
    }

    data class TokenSummary(
        override val metadata: HermesConversationSectionMetadata,
        val summary: HermesConversationTokenSummary,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.TOKEN_SUMMARY
    }

    data class PendingInput(
        override val metadata: HermesConversationSectionMetadata,
        val input: HermesConversationPendingInput,
    ) : HermesConversationSection {
        override val kind = HermesConversationSectionKind.PENDING_INPUT
    }
}

enum class HermesConversationTodoStatus {
    PENDING,
    IN_PROGRESS,
    COMPLETED,
    CANCELLED,
}

data class HermesConversationTodoItem(
    val key: String,
    val content: String,
    val status: HermesConversationTodoStatus,
)

data class HermesConversationSubagent(
    val key: String,
    val goal: String,
    val status: HermesConversationSectionStatus,
    val parentKey: String? = null,
    val model: String? = null,
    val summary: String? = null,
    val durationSeconds: Double? = null,
    val taskIndex: Long? = null,
    val taskCount: Long? = null,
    val tokenSummary: HermesConversationTokenSummary? = null,
    val apiCalls: Long? = null,
)

data class HermesConversationMoaReference(
    val key: String,
    val label: String,
    val text: String,
)

data class HermesConversationMoaProgress(
    val phase: String? = null,
    val aggregator: String? = null,
    val refsDone: Int? = null,
    val refsTotal: Int? = null,
)

enum class HermesConversationActivityTone {
    INFO,
    WARNING,
    ERROR,
}

data class HermesConversationTokenSummary(
    val inputTokens: Long? = null,
    val outputTokens: Long? = null,
    val reasoningTokens: Long? = null,
    val toolTokens: Long? = null,
    val totalTokens: Long? = null,
)

enum class HermesConversationPendingInputKind {
    APPROVAL,
    CLARIFICATION,
    SUDO,
    SECRET,
    OTHER,
}

data class HermesConversationPendingInput(
    val kind: HermesConversationPendingInputKind,
    val prompt: String,
    val choices: List<String> = emptyList(),
)
