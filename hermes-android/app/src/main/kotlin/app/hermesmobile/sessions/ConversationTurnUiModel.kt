package app.hermesmobile.sessions

data class ConversationTurnUiModel(
    val key: String,
    val userPrompt: ConversationPromptUiModel?,
    val thinking: String,
    val statusText: String,
    val tools: List<ConversationToolUiModel>,
    val response: String,
    val status: ConversationTurnStatus,
    val error: String? = null,
    val eventText: String? = null,
    val sections: CanonicalConversationSections = CanonicalConversationSections.Empty,
)

data class ConversationPromptUiModel(
    val key: String,
    val text: String,
)

internal fun shouldShowResponseSeparator(
    turn: ConversationTurnUiModel,
    processVisible: Boolean,
): Boolean = processVisible &&
    turn.response.isNotBlank()

data class ConversationToolDetailUiModel(
    val label: String,
    val value: String,
)

data class ConversationToolUiModel(
    val key: String,
    val toolId: String,
    val name: String?,
    val callLabel: String? = null,
    val context: String? = null,
    val arguments: String? = null,
    val argumentDetails: List<ConversationToolDetailUiModel> = emptyList(),
    val output: String = "",
    val result: String? = null,
    val summary: String? = null,
    val diff: String? = null,
    val resultDetails: List<ConversationToolDetailUiModel> = emptyList(),
    val durationSeconds: Double? = null,
    val status: ConversationToolStatus,
    val error: String? = null,
)

enum class ConversationTurnStatus {
    INCOMPLETE,
    STREAMING,
    COMPLETE,
    ERROR,
}

enum class ConversationToolStatus {
    RUNNING,
    COMPLETE,
    ERROR,
    INTERRUPTED,
    UNKNOWN,
}
