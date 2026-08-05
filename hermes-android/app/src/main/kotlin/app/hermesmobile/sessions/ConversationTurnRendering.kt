package app.hermesmobile.sessions

/**
 * Returns the authoritative caller-ordered sections for rendering.
 *
 * Compose must not reorder or reconstruct canonical projector output: stable
 * section identity drives disclosure, scrolling, and in-place streaming updates.
 */
internal fun ConversationTurnUiModel.renderSections(): List<HermesConversationSection> {
    if (sections.isNotEmpty()) return sections.toList()
    eventText?.takeIf(String::isNotBlank)?.let { text ->
        return listOf(
            HermesConversationSection.Event(
                metadata = renderMetadata("event", HermesConversationSectionStatus.COMPLETE),
                text = text,
            ),
        )
    }
    return buildList {
        userPrompt?.let { prompt ->
            add(
                HermesConversationSection.UserPrompt(
                    metadata = renderMetadata(
                        suffix = "user-prompt",
                        status = HermesConversationSectionStatus.COMPLETE,
                    ),
                    prompt = prompt,
                ),
            )
        }
        thinking.takeIf(String::isNotBlank)?.let { text ->
            add(
                HermesConversationSection.Thinking(
                    metadata = renderMetadata("thinking", status.toSectionStatus()),
                    text = text,
                ),
            )
        }
        if (tools.isNotEmpty()) {
            add(
                HermesConversationSection.ToolGroup(
                    metadata = renderMetadata("tools", tools.renderStatus()),
                    tools = tools,
                ),
            )
        }
        statusText.takeIf(String::isNotBlank)?.let { text ->
            add(
                HermesConversationSection.Activity(
                    metadata = renderMetadata("activity", status.toSectionStatus()),
                    text = text,
                ),
            )
        }
        val hasProcess = thinking.isNotBlank() || tools.isNotEmpty() || statusText.isNotBlank()
        if (
            hasProcess &&
            response.isNotBlank()
        ) {
            add(
                HermesConversationSection.ResponseBoundary(
                    metadata = renderMetadata(
                        suffix = "response-boundary",
                        status = status.toSectionStatus(),
                    ),
                ),
            )
        }
        response.takeIf(String::isNotBlank)?.let { text ->
            add(
                HermesConversationSection.AssistantResponse(
                    metadata = renderMetadata("response", status.toSectionStatus()),
                    text = text,
                ),
            )
        }
        error?.takeIf(String::isNotBlank)?.let { message ->
            add(
                HermesConversationSection.Error(
                    metadata = renderMetadata("error", HermesConversationSectionStatus.ERROR),
                    message = message,
                ),
            )
        }
    }
}

internal fun HermesConversationSection.renderIdentityKey(): String = key

private fun ConversationTurnUiModel.renderMetadata(
    suffix: String,
    status: HermesConversationSectionStatus,
) = HermesConversationSectionMetadata(
    key = "$key:$suffix",
    status = status,
)

private fun ConversationTurnStatus.toSectionStatus(): HermesConversationSectionStatus = when (this) {
    ConversationTurnStatus.INCOMPLETE -> HermesConversationSectionStatus.INTERRUPTED
    ConversationTurnStatus.STREAMING -> HermesConversationSectionStatus.STREAMING
    ConversationTurnStatus.COMPLETE -> HermesConversationSectionStatus.COMPLETE
    ConversationTurnStatus.ERROR -> HermesConversationSectionStatus.ERROR
}

private fun List<ConversationToolUiModel>.renderStatus(): HermesConversationSectionStatus = when {
    any { it.status == ConversationToolStatus.ERROR } -> HermesConversationSectionStatus.ERROR
    any {
        it.status == ConversationToolStatus.INTERRUPTED ||
            it.status == ConversationToolStatus.UNKNOWN
    } -> HermesConversationSectionStatus.INTERRUPTED
    any { it.status == ConversationToolStatus.RUNNING } -> HermesConversationSectionStatus.RUNNING
    else -> HermesConversationSectionStatus.COMPLETE
}
