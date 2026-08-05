package app.hermesmobile.sessions

internal enum class SessionVoiceInputPhase {
    IDLE,
    REQUESTING_PERMISSION,
    LISTENING,
    ERROR,
}

internal enum class SessionVoiceInputFailure {
    PERMISSION_DENIED,
    SERVICE_UNAVAILABLE,
    NO_MATCH,
    AUDIO,
    NETWORK,
    RECOGNIZER_BUSY,
    CLIENT,
    UNKNOWN,
}

internal enum class SessionVoiceInputCommand {
    NONE,
    REQUEST_PERMISSION,
    START_RECOGNIZER,
    CANCEL_RECOGNIZER,
}

internal data class SessionVoiceInputState(
    val phase: SessionVoiceInputPhase = SessionVoiceInputPhase.IDLE,
    val baseDraft: String = "",
    val partialTranscript: String = "",
    val failure: SessionVoiceInputFailure? = null,
)

internal sealed interface SessionVoiceInputEvent {
    data class StartRequested(
        val draft: String,
        val serviceAvailable: Boolean,
        val permissionGranted: Boolean,
    ) : SessionVoiceInputEvent

    data class PermissionResult(val granted: Boolean) : SessionVoiceInputEvent
    data class PartialResult(val transcript: String) : SessionVoiceInputEvent
    data class FinalResult(val transcript: String) : SessionVoiceInputEvent
    data class Failed(val failure: SessionVoiceInputFailure) : SessionVoiceInputEvent
    data object CancelRequested : SessionVoiceInputEvent
    data object DismissFailure : SessionVoiceInputEvent
}

internal data class SessionVoiceInputTransition(
    val state: SessionVoiceInputState,
    val command: SessionVoiceInputCommand = SessionVoiceInputCommand.NONE,
    val draftUpdate: String? = null,
)

internal class SessionVoiceInputReducer {
    fun reduce(
        state: SessionVoiceInputState,
        event: SessionVoiceInputEvent,
    ): SessionVoiceInputTransition = when (event) {
        is SessionVoiceInputEvent.StartRequested -> start(event)
        is SessionVoiceInputEvent.PermissionResult -> permissionResult(state, event.granted)
        is SessionVoiceInputEvent.PartialResult -> partialResult(state, event.transcript)
        is SessionVoiceInputEvent.FinalResult -> finalResult(state, event.transcript)
        is SessionVoiceInputEvent.Failed -> failure(state, event.failure)
        SessionVoiceInputEvent.CancelRequested -> cancel(state)
        SessionVoiceInputEvent.DismissFailure -> SessionVoiceInputTransition(SessionVoiceInputState())
    }

    private fun start(event: SessionVoiceInputEvent.StartRequested): SessionVoiceInputTransition {
        if (!event.serviceAvailable) {
            return SessionVoiceInputTransition(
                state = SessionVoiceInputState(
                    phase = SessionVoiceInputPhase.ERROR,
                    baseDraft = event.draft,
                    failure = SessionVoiceInputFailure.SERVICE_UNAVAILABLE,
                ),
            )
        }
        val state = SessionVoiceInputState(
            phase = if (event.permissionGranted) {
                SessionVoiceInputPhase.LISTENING
            } else {
                SessionVoiceInputPhase.REQUESTING_PERMISSION
            },
            baseDraft = event.draft,
        )
        return SessionVoiceInputTransition(
            state = state,
            command = if (event.permissionGranted) {
                SessionVoiceInputCommand.START_RECOGNIZER
            } else {
                SessionVoiceInputCommand.REQUEST_PERMISSION
            },
        )
    }

    private fun permissionResult(
        state: SessionVoiceInputState,
        granted: Boolean,
    ): SessionVoiceInputTransition {
        if (state.phase != SessionVoiceInputPhase.REQUESTING_PERMISSION) {
            return SessionVoiceInputTransition(state)
        }
        return if (granted) {
            SessionVoiceInputTransition(
                state = state.copy(
                    phase = SessionVoiceInputPhase.LISTENING,
                    failure = null,
                ),
                command = SessionVoiceInputCommand.START_RECOGNIZER,
            )
        } else {
            SessionVoiceInputTransition(
                state = state.copy(
                    phase = SessionVoiceInputPhase.ERROR,
                    partialTranscript = "",
                    failure = SessionVoiceInputFailure.PERMISSION_DENIED,
                ),
                draftUpdate = state.baseDraft,
            )
        }
    }

    private fun partialResult(
        state: SessionVoiceInputState,
        transcript: String,
    ): SessionVoiceInputTransition {
        if (state.phase != SessionVoiceInputPhase.LISTENING) {
            return SessionVoiceInputTransition(state)
        }
        val normalized = transcript.trim()
        return SessionVoiceInputTransition(
            state = state.copy(partialTranscript = normalized),
            draftUpdate = appendVoiceSegment(state.baseDraft, normalized),
        )
    }

    private fun finalResult(
        state: SessionVoiceInputState,
        transcript: String,
    ): SessionVoiceInputTransition {
        if (state.phase != SessionVoiceInputPhase.LISTENING) {
            return SessionVoiceInputTransition(state)
        }
        val normalized = transcript.trim()
        if (normalized.isEmpty()) {
            return failure(state, SessionVoiceInputFailure.NO_MATCH)
        }
        return SessionVoiceInputTransition(
            state = SessionVoiceInputState(),
            draftUpdate = appendVoiceSegment(state.baseDraft, normalized),
        )
    }

    private fun failure(
        state: SessionVoiceInputState,
        failure: SessionVoiceInputFailure,
    ): SessionVoiceInputTransition {
        if (
            state.phase != SessionVoiceInputPhase.LISTENING &&
            state.phase != SessionVoiceInputPhase.REQUESTING_PERMISSION
        ) {
            return SessionVoiceInputTransition(state)
        }
        return SessionVoiceInputTransition(
            state = state.copy(
                phase = SessionVoiceInputPhase.ERROR,
                partialTranscript = "",
                failure = failure,
            ),
            draftUpdate = state.baseDraft,
        )
    }

    private fun cancel(state: SessionVoiceInputState): SessionVoiceInputTransition {
        if (
            state.phase != SessionVoiceInputPhase.LISTENING &&
            state.phase != SessionVoiceInputPhase.REQUESTING_PERMISSION
        ) {
            return SessionVoiceInputTransition(state)
        }
        return SessionVoiceInputTransition(
            state = SessionVoiceInputState(),
            command = if (state.phase == SessionVoiceInputPhase.LISTENING) {
                SessionVoiceInputCommand.CANCEL_RECOGNIZER
            } else {
                SessionVoiceInputCommand.NONE
            },
            draftUpdate = state.baseDraft,
        )
    }
}

internal fun appendVoiceSegment(baseDraft: String, transcript: String): String {
    val normalized = transcript.trim()
    if (normalized.isEmpty()) return baseDraft
    if (baseDraft.isEmpty() || baseDraft.last().isWhitespace()) return baseDraft + normalized
    return "$baseDraft $normalized"
}
