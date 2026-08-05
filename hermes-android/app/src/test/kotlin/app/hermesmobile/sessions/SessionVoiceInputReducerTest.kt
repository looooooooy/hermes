package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class SessionVoiceInputReducerTest {
    private val reducer = SessionVoiceInputReducer()

    @Test
    fun `permission is requested before recognition without changing the draft`() {
        val transition = reducer.reduce(
            SessionVoiceInputState(),
            SessionVoiceInputEvent.StartRequested(
                draft = "Inspect the logs",
                serviceAvailable = true,
                permissionGranted = false,
            ),
        )

        assertEquals(SessionVoiceInputPhase.REQUESTING_PERMISSION, transition.state.phase)
        assertEquals("Inspect the logs", transition.state.baseDraft)
        assertEquals(SessionVoiceInputCommand.REQUEST_PERMISSION, transition.command)
        assertNull(transition.draftUpdate)
    }

    @Test
    fun `permission grant starts recognition and denial restores the original draft`() {
        val awaiting = SessionVoiceInputState(
            phase = SessionVoiceInputPhase.REQUESTING_PERMISSION,
            baseDraft = "Keep this",
        )

        val granted = reducer.reduce(awaiting, SessionVoiceInputEvent.PermissionResult(granted = true))
        assertEquals(SessionVoiceInputPhase.LISTENING, granted.state.phase)
        assertEquals(SessionVoiceInputCommand.START_RECOGNIZER, granted.command)
        assertNull(granted.draftUpdate)

        val denied = reducer.reduce(awaiting, SessionVoiceInputEvent.PermissionResult(granted = false))
        assertEquals(SessionVoiceInputPhase.ERROR, denied.state.phase)
        assertEquals(SessionVoiceInputFailure.PERMISSION_DENIED, denied.state.failure)
        assertEquals("Keep this", denied.draftUpdate)
    }

    @Test
    fun `partial recognition replaces only the voice segment after the preserved draft`() {
        val listening = SessionVoiceInputState(
            phase = SessionVoiceInputPhase.LISTENING,
            baseDraft = "Inspect the logs",
        )

        val first = reducer.reduce(
            listening,
            SessionVoiceInputEvent.PartialResult("and summarize"),
        )
        assertEquals("Inspect the logs and summarize", first.draftUpdate)

        val replacement = reducer.reduce(
            first.state,
            SessionVoiceInputEvent.PartialResult("and summarize failures"),
        )
        assertEquals("Inspect the logs and summarize failures", replacement.draftUpdate)
        assertEquals("Inspect the logs", replacement.state.baseDraft)
    }

    @Test
    fun `final recognition commits the appended transcript and returns idle`() {
        val transition = reducer.reduce(
            SessionVoiceInputState(
                phase = SessionVoiceInputPhase.LISTENING,
                baseDraft = "Inspect the logs ",
                partialTranscript = "and summarize",
            ),
            SessionVoiceInputEvent.FinalResult("and summarize failures"),
        )

        assertEquals(SessionVoiceInputPhase.IDLE, transition.state.phase)
        assertEquals("Inspect the logs and summarize failures", transition.draftUpdate)
        assertEquals(SessionVoiceInputCommand.NONE, transition.command)
    }

    @Test
    fun `cancel stops recognition and restores the original draft`() {
        val transition = reducer.reduce(
            SessionVoiceInputState(
                phase = SessionVoiceInputPhase.LISTENING,
                baseDraft = "Keep this",
                partialTranscript = "temporary words",
            ),
            SessionVoiceInputEvent.CancelRequested,
        )

        assertEquals(SessionVoiceInputPhase.IDLE, transition.state.phase)
        assertEquals(SessionVoiceInputCommand.CANCEL_RECOGNIZER, transition.command)
        assertEquals("Keep this", transition.draftUpdate)
    }

    @Test
    fun `recognizer error restores the original draft and exposes a bounded failure`() {
        val transition = reducer.reduce(
            SessionVoiceInputState(
                phase = SessionVoiceInputPhase.LISTENING,
                baseDraft = "Keep this",
                partialTranscript = "temporary words",
            ),
            SessionVoiceInputEvent.Failed(SessionVoiceInputFailure.NETWORK),
        )

        assertEquals(SessionVoiceInputPhase.ERROR, transition.state.phase)
        assertEquals(SessionVoiceInputFailure.NETWORK, transition.state.failure)
        assertEquals("Keep this", transition.draftUpdate)
        assertEquals(SessionVoiceInputCommand.NONE, transition.command)
    }

    @Test
    fun `unavailable recognizer fails closed without requesting microphone permission`() {
        val transition = reducer.reduce(
            SessionVoiceInputState(),
            SessionVoiceInputEvent.StartRequested(
                draft = "Existing",
                serviceAvailable = false,
                permissionGranted = false,
            ),
        )

        assertEquals(SessionVoiceInputPhase.ERROR, transition.state.phase)
        assertEquals(SessionVoiceInputFailure.SERVICE_UNAVAILABLE, transition.state.failure)
        assertEquals(SessionVoiceInputCommand.NONE, transition.command)
        assertNull(transition.draftUpdate)
    }

    @Test
    fun `blank final result is treated as no match and restores the original draft`() {
        val transition = reducer.reduce(
            SessionVoiceInputState(
                phase = SessionVoiceInputPhase.LISTENING,
                baseDraft = "Existing",
            ),
            SessionVoiceInputEvent.FinalResult("   "),
        )

        assertEquals(SessionVoiceInputPhase.ERROR, transition.state.phase)
        assertEquals(SessionVoiceInputFailure.NO_MATCH, transition.state.failure)
        assertEquals("Existing", transition.draftUpdate)
    }
}
