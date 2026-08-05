package app.hermesmobile.sessions

internal enum class ConversationDisclosureSection {
    TODO,
    THINKING,
    TOOLS,
    SUBAGENTS,
    ACTIVITY,
}

internal enum class ConversationDisclosureMode {
    HIDDEN,
    COLLAPSED,
    EXPANDED,
}

internal class ConversationDisclosurePolicy {
    fun mode(section: ConversationDisclosureSection): ConversationDisclosureMode = when (section) {
        ConversationDisclosureSection.TODO,
        ConversationDisclosureSection.THINKING,
        ConversationDisclosureSection.TOOLS,
        ConversationDisclosureSection.SUBAGENTS,
        -> ConversationDisclosureMode.EXPANDED

        ConversationDisclosureSection.ACTIVITY -> ConversationDisclosureMode.HIDDEN
    }
}

internal data class ConversationDisclosureStateKey(
    val turnKey: String,
    val section: ConversationDisclosureSection,
)

internal data class ConversationDisclosureState(
    private val explicitModes: Map<ConversationDisclosureStateKey, ConversationDisclosureMode> = emptyMap(),
) {
    fun mode(
        key: ConversationDisclosureStateKey,
        policy: ConversationDisclosurePolicy,
    ): ConversationDisclosureMode = explicitModes[key] ?: policy.mode(key.section)

    fun toggled(
        key: ConversationDisclosureStateKey,
        policy: ConversationDisclosurePolicy,
    ): ConversationDisclosureState {
        val nextMode = when (mode(key, policy)) {
            ConversationDisclosureMode.EXPANDED -> ConversationDisclosureMode.COLLAPSED
            ConversationDisclosureMode.COLLAPSED -> ConversationDisclosureMode.EXPANDED
            ConversationDisclosureMode.HIDDEN -> return this
        }
        return copy(explicitModes = explicitModes + (key to nextMode))
    }

    fun withMode(
        key: ConversationDisclosureStateKey,
        mode: ConversationDisclosureMode,
    ): ConversationDisclosureState = copy(
        explicitModes = explicitModes + (key to mode),
    )
}

internal data class ConversationDisclosureRegistry(
    private val sessionStates: Map<String, ConversationDisclosureState> = emptyMap(),
) {
    fun state(sessionKey: String): ConversationDisclosureState =
        sessionStates[sessionKey] ?: ConversationDisclosureState()

    fun toggled(
        sessionKey: String,
        key: ConversationDisclosureStateKey,
        policy: ConversationDisclosurePolicy,
    ): ConversationDisclosureRegistry = copy(
        sessionStates = sessionStates + (sessionKey to state(sessionKey).toggled(key, policy)),
    )

    fun focused(
        sessionKey: String,
        key: ConversationDisclosureStateKey,
    ): ConversationDisclosureRegistry {
        val siblingSection = when (key.section) {
            ConversationDisclosureSection.TODO -> ConversationDisclosureSection.SUBAGENTS
            ConversationDisclosureSection.SUBAGENTS -> ConversationDisclosureSection.TODO
            else -> return this
        }
        val focusedState = state(sessionKey)
            .withMode(key, ConversationDisclosureMode.EXPANDED)
            .withMode(
                key.copy(section = siblingSection),
                ConversationDisclosureMode.COLLAPSED,
            )
        return copy(sessionStates = sessionStates + (sessionKey to focusedState))
    }
}
