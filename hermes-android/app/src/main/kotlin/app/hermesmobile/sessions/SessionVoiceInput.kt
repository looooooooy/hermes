package app.hermesmobile.sessions

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat

internal data class SessionVoiceInputHandle(
    val state: SessionVoiceInputState,
    val onAction: () -> Unit,
)

@Composable
internal fun rememberSessionVoiceInput(
    sessionKey: String,
    draft: String,
    enabled: Boolean,
    onDraftChanged: (String) -> Unit,
): SessionVoiceInputHandle {
    val context = LocalContext.current
    val reducer = remember { SessionVoiceInputReducer() }
    val controller = remember(context, sessionKey) {
        AndroidSessionSpeechRecognizerController(context)
    }
    val stateHolder = remember(sessionKey) { mutableStateOf(SessionVoiceInputState()) }
    var pendingCommand by remember(sessionKey) {
        mutableStateOf(SessionVoiceInputCommand.NONE)
    }
    val currentDraft by rememberUpdatedState(draft)
    val currentOnDraftChanged by rememberUpdatedState(onDraftChanged)

    fun dispatch(event: SessionVoiceInputEvent) {
        val transition = reducer.reduce(stateHolder.value, event)
        stateHolder.value = transition.state
        transition.draftUpdate?.let(currentOnDraftChanged)
        if (transition.command != SessionVoiceInputCommand.NONE) {
            pendingCommand = transition.command
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        dispatch(SessionVoiceInputEvent.PermissionResult(granted))
    }

    LaunchedEffect(pendingCommand, controller) {
        val command = pendingCommand
        pendingCommand = SessionVoiceInputCommand.NONE
        when (command) {
            SessionVoiceInputCommand.NONE -> Unit
            SessionVoiceInputCommand.REQUEST_PERMISSION -> {
                permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
            }
            SessionVoiceInputCommand.START_RECOGNIZER -> controller.start(
                onPartialResult = { transcript ->
                    dispatch(SessionVoiceInputEvent.PartialResult(transcript))
                },
                onFinalResult = { transcript ->
                    dispatch(SessionVoiceInputEvent.FinalResult(transcript))
                },
                onFailure = { failure ->
                    dispatch(SessionVoiceInputEvent.Failed(failure))
                },
            )
            SessionVoiceInputCommand.CANCEL_RECOGNIZER -> controller.cancel()
        }
    }

    LaunchedEffect(enabled) {
        if (
            !enabled &&
            (
                stateHolder.value.phase == SessionVoiceInputPhase.LISTENING ||
                    stateHolder.value.phase == SessionVoiceInputPhase.REQUESTING_PERMISSION
                )
        ) {
            dispatch(SessionVoiceInputEvent.CancelRequested)
        }
    }

    DisposableEffect(controller) {
        onDispose {
            val state = stateHolder.value
            controller.cancel()
            controller.destroy()
            if (
                state.phase == SessionVoiceInputPhase.LISTENING ||
                state.phase == SessionVoiceInputPhase.REQUESTING_PERMISSION
            ) {
                currentOnDraftChanged(state.baseDraft)
            }
        }
    }

    val action = {
        when (stateHolder.value.phase) {
            SessionVoiceInputPhase.LISTENING,
            SessionVoiceInputPhase.REQUESTING_PERMISSION,
            -> dispatch(SessionVoiceInputEvent.CancelRequested)

            SessionVoiceInputPhase.IDLE,
            SessionVoiceInputPhase.ERROR,
            -> if (enabled) {
                dispatch(
                    SessionVoiceInputEvent.StartRequested(
                        draft = currentDraft,
                        serviceAvailable = controller.isAvailable,
                        permissionGranted = ContextCompat.checkSelfPermission(
                            context,
                            Manifest.permission.RECORD_AUDIO,
                        ) == PackageManager.PERMISSION_GRANTED,
                    ),
                )
            }
        }
    }
    return SessionVoiceInputHandle(
        state = stateHolder.value,
        onAction = action,
    )
}

private class AndroidSessionSpeechRecognizerController(
    context: Context,
) {
    private val applicationContext = context.applicationContext
    private var recognizer: SpeechRecognizer? = null

    val isAvailable: Boolean
        get() = SpeechRecognizer.isRecognitionAvailable(applicationContext)

    fun start(
        onPartialResult: (String) -> Unit,
        onFinalResult: (String) -> Unit,
        onFailure: (SessionVoiceInputFailure) -> Unit,
    ) {
        if (!isAvailable) {
            onFailure(SessionVoiceInputFailure.SERVICE_UNAVAILABLE)
            return
        }
        runCatching {
            val activeRecognizer = recognizer ?: SpeechRecognizer
                .createSpeechRecognizer(applicationContext)
                .also { recognizer = it }
            activeRecognizer.setRecognitionListener(
                SessionRecognitionListener(
                    onPartialResult = onPartialResult,
                    onFinalResult = onFinalResult,
                    onFailure = onFailure,
                ),
            )
            activeRecognizer.startListening(
                Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                    putExtra(
                        RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                        RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
                    )
                    putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                    putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                },
            )
        }.onFailure {
            onFailure(SessionVoiceInputFailure.CLIENT)
        }
    }

    fun cancel() {
        runCatching { recognizer?.cancel() }
    }

    fun destroy() {
        runCatching { recognizer?.destroy() }
        recognizer = null
    }
}

private class SessionRecognitionListener(
    private val onPartialResult: (String) -> Unit,
    private val onFinalResult: (String) -> Unit,
    private val onFailure: (SessionVoiceInputFailure) -> Unit,
) : RecognitionListener {
    override fun onReadyForSpeech(params: Bundle?) = Unit
    override fun onBeginningOfSpeech() = Unit
    override fun onRmsChanged(rmsdB: Float) = Unit
    override fun onBufferReceived(buffer: ByteArray?) = Unit
    override fun onEndOfSpeech() = Unit

    override fun onError(error: Int) {
        onFailure(error.toSessionVoiceInputFailure())
    }

    override fun onResults(results: Bundle?) {
        onFinalResult(results.firstRecognitionResult().orEmpty())
    }

    override fun onPartialResults(partialResults: Bundle?) {
        onPartialResult(partialResults.firstRecognitionResult().orEmpty())
    }

    override fun onEvent(eventType: Int, params: Bundle?) = Unit
}

private fun Bundle?.firstRecognitionResult(): String? = this
    ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
    ?.firstOrNull()

internal fun Int.toSessionVoiceInputFailure(): SessionVoiceInputFailure = when (this) {
    SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> SessionVoiceInputFailure.PERMISSION_DENIED
    SpeechRecognizer.ERROR_NO_MATCH,
    SpeechRecognizer.ERROR_SPEECH_TIMEOUT,
    -> SessionVoiceInputFailure.NO_MATCH
    SpeechRecognizer.ERROR_AUDIO -> SessionVoiceInputFailure.AUDIO
    SpeechRecognizer.ERROR_NETWORK,
    SpeechRecognizer.ERROR_NETWORK_TIMEOUT,
    SpeechRecognizer.ERROR_SERVER,
    SpeechRecognizer.ERROR_SERVER_DISCONNECTED,
    -> SessionVoiceInputFailure.NETWORK
    SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> SessionVoiceInputFailure.RECOGNIZER_BUSY
    SpeechRecognizer.ERROR_CLIENT -> SessionVoiceInputFailure.CLIENT
    else -> SessionVoiceInputFailure.UNKNOWN
}
