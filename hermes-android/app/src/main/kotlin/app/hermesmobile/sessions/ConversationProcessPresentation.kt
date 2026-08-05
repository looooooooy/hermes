package app.hermesmobile.sessions

internal data class ConversationProcessPresentation(
    val sections: List<ConversationProcessSectionPresentation>,
    val visibleErrorFallback: ConversationProcessVisibleErrorPresentation?,
) {
    fun section(
        section: ConversationDisclosureSection,
    ): ConversationProcessSectionPresentation? = sections.firstOrNull { it.section == section }
}

internal data class ConversationProcessSectionPresentation(
    val key: ConversationDisclosureStateKey,
    val items: List<ConversationProcessItemPresentation>,
) {
    val section: ConversationDisclosureSection
        get() = key.section
}

internal data class ConversationProcessItemKey(
    val sectionKey: ConversationDisclosureStateKey,
    val sourceKey: String,
)

internal sealed interface ConversationProcessItemPresentation {
    val key: ConversationProcessItemKey

    data class Thinking(
        override val key: ConversationProcessItemKey,
        val text: String,
    ) : ConversationProcessItemPresentation

    data class Tool(
        override val key: ConversationProcessItemKey,
        val status: ConversationToolStatus,
        val details: List<ConversationProcessToolDetailPresentation>,
    ) : ConversationProcessItemPresentation

    data class Activity(
        override val key: ConversationProcessItemKey,
        val text: String,
    ) : ConversationProcessItemPresentation
}

internal enum class ConversationProcessToolDetailKind {
    ARGUMENTS,
    RESULT,
    ERROR,
}

internal data class ConversationProcessToolDetailKey(
    val toolKey: ConversationProcessItemKey,
    val kind: ConversationProcessToolDetailKind,
)

internal data class ConversationProcessToolDetailPresentation(
    val key: ConversationProcessToolDetailKey,
    val body: String,
) {
    val kind: ConversationProcessToolDetailKind
        get() = key.kind
}

internal data class ConversationProcessVisibleErrorPresentation(
    val key: ConversationProcessItemKey,
    val text: String,
)

internal object ConversationProcessPresentationProjector {
    fun project(turn: ConversationTurnUiModel): ConversationProcessPresentation {
        val sections = buildList {
            turn.thinking.takeIf(String::isNotBlank)?.let { thinking ->
                val sectionKey = turn.sectionKey(ConversationDisclosureSection.THINKING)
                add(
                    ConversationProcessSectionPresentation(
                        key = sectionKey,
                        items = listOf(
                            ConversationProcessItemPresentation.Thinking(
                                key = sectionKey.itemKey("thinking"),
                                text = thinking,
                            ),
                        ),
                    ),
                )
            }
            if (turn.tools.isNotEmpty()) {
                val sectionKey = turn.sectionKey(ConversationDisclosureSection.TOOLS)
                add(
                    ConversationProcessSectionPresentation(
                        key = sectionKey,
                        items = turn.tools.map { tool ->
                            val itemKey = sectionKey.itemKey(tool.key)
                            ConversationProcessItemPresentation.Tool(
                                key = itemKey,
                                status = tool.status,
                                details = tool.details(itemKey),
                            )
                        },
                    ),
                )
            }
            turn.statusText.takeIf(String::isNotBlank)?.let { activity ->
                val sectionKey = turn.sectionKey(ConversationDisclosureSection.ACTIVITY)
                add(
                    ConversationProcessSectionPresentation(
                        key = sectionKey,
                        items = listOf(
                            ConversationProcessItemPresentation.Activity(
                                key = sectionKey.itemKey("activity"),
                                text = activity,
                            ),
                        ),
                    ),
                )
            }
        }
        val visibleError = sections
            .firstOrNull { section -> section.section == ConversationDisclosureSection.TOOLS }
            ?.items
            ?.asReversed()
            ?.filterIsInstance<ConversationProcessItemPresentation.Tool>()
            ?.firstOrNull { tool -> tool.status == ConversationToolStatus.ERROR }
            ?.let { tool ->
                ConversationProcessVisibleErrorPresentation(
                    key = tool.key,
                    text = tool.details
                        .first { detail -> detail.kind == ConversationProcessToolDetailKind.ERROR }
                        .body,
                )
            }
        return ConversationProcessPresentation(
            sections = sections,
            visibleErrorFallback = visibleError,
        )
    }
}

private fun ConversationTurnUiModel.sectionKey(
    section: ConversationDisclosureSection,
): ConversationDisclosureStateKey = ConversationDisclosureStateKey(
    turnKey = key,
    section = section,
)

private fun ConversationDisclosureStateKey.itemKey(sourceKey: String) = ConversationProcessItemKey(
    sectionKey = this,
    sourceKey = sourceKey,
)

private fun ConversationToolUiModel.details(
    itemKey: ConversationProcessItemKey,
): List<ConversationProcessToolDetailPresentation> = buildList {
    val call = HermesMessagePresentation.toolCall(name, arguments, context)
    val argumentPayload = HermesMessagePresentation.payloadText(arguments)
    val readableArguments = argumentDetails
        .ifEmpty { call.details }
        .ifEmpty { argumentPayload.details }
        .joinToString("\n") { detail -> "${detail.label}: ${detail.value}" }
        .ifBlank { argumentPayload.text.trimEnd() }
    readableArguments.takeIf(String::isNotBlank)?.let { body ->
        add(
            ConversationProcessToolDetailPresentation(
                key = ConversationProcessToolDetailKey(
                    toolKey = itemKey,
                    kind = ConversationProcessToolDetailKind.ARGUMENTS,
                ),
                body = body,
            ),
        )
    }

    val readableResult = HermesMessagePresentation.payloadText(output)
        .visibleText()
        .trimEnd()
    if (status == ConversationToolStatus.ERROR) {
        val readableError = HermesMessagePresentation.payloadText(error)
            .visibleText()
            .trimEnd()
        val body = listOf(readableResult, readableError)
            .filter(String::isNotBlank)
            .distinct()
            .joinToString("\n")
            .ifBlank { TOOL_ERROR_FALLBACK }
        add(
            ConversationProcessToolDetailPresentation(
                key = ConversationProcessToolDetailKey(
                    toolKey = itemKey,
                    kind = ConversationProcessToolDetailKind.ERROR,
                ),
                body = body,
            ),
        )
    } else {
        readableResult.takeIf(String::isNotBlank)?.let { body ->
            add(
                ConversationProcessToolDetailPresentation(
                    key = ConversationProcessToolDetailKey(
                        toolKey = itemKey,
                        kind = ConversationProcessToolDetailKind.RESULT,
                    ),
                    body = body,
                ),
            )
        }
    }
}

private const val TOOL_ERROR_FALLBACK = "No error details were provided."
