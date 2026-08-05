package app.hermesmobile.sessions

object HermesMarkdownSanitizer {
    fun sanitize(text: String): String = HermesStreamingMarkdownSanitizer().advance(text)
}

/**
 * Incremental counterpart to [HermesMarkdownSanitizer]. It retains incomplete
 * terminal-control state so an append can be consumed without exposing payload
 * text that a later control-sequence terminator would hide.
 */
internal class HermesStreamingMarkdownSanitizer {
    private enum class State {
        TEXT,
        ESCAPE,
        CSI,
        OSC,
        OSC_ESCAPE,
        CONTROL_STRING,
        CONTROL_STRING_ESCAPE,
    }

    private val sanitized = StringBuilder()
    private var source = ""
    private var state = State.TEXT
    internal var processedSourceCharacterCount: Long = 0
        private set

    fun advance(text: String): String {
        if (!text.startsWith(source)) reset()

        return advanceVerifiedAppend(text)
    }

    internal fun advanceVerifiedAppend(text: String): String {
        require(text.length >= source.length)
        for (index in source.length until text.length) {
            processedSourceCharacterCount += 1
            consume(text[index])
        }
        source = text
        return sanitized.toString()
    }

    fun reset() {
        sanitized.clear()
        source = ""
        state = State.TEXT
        processedSourceCharacterCount = 0
    }

    private fun consume(character: Char) {
        when (state) {
            State.TEXT -> when {
                character == ESCAPE -> state = State.ESCAPE
                character == C1_CSI -> state = State.CSI
                character == C1_OSC -> state = State.OSC
                character in C1_CONTROL_STRING_INTRODUCERS -> state = State.CONTROL_STRING
                character == BELL || character == DELETE -> Unit
                character.isISOControl() && character !in ALLOWED_CONTROLS -> Unit
                else -> sanitized.append(character)
            }
            State.ESCAPE -> state = when (character) {
                ESCAPE -> State.ESCAPE
                '[' -> State.CSI
                ']' -> State.OSC
                'P', 'X', '^', '_' -> State.CONTROL_STRING
                C1_CSI -> State.CSI
                C1_OSC -> State.OSC
                in C1_CONTROL_STRING_INTRODUCERS -> State.CONTROL_STRING
                else -> State.TEXT
            }
            State.CSI -> state = when (character) {
                ESCAPE -> State.ESCAPE
                C1_CSI -> State.CSI
                C1_OSC -> State.OSC
                in C1_CONTROL_STRING_INTRODUCERS -> State.CONTROL_STRING
                in CSI_FINAL_RANGE -> State.TEXT
                else -> State.CSI
            }
            State.OSC -> state = when (character) {
                BELL, C1_STRING_TERMINATOR -> State.TEXT
                ESCAPE -> State.OSC_ESCAPE
                else -> State.OSC
            }
            State.OSC_ESCAPE -> state = when (character) {
                '\\', BELL, C1_STRING_TERMINATOR -> State.TEXT
                ESCAPE -> State.OSC_ESCAPE
                else -> State.OSC
            }
            State.CONTROL_STRING -> state = when (character) {
                C1_STRING_TERMINATOR -> State.TEXT
                ESCAPE -> State.CONTROL_STRING_ESCAPE
                else -> State.CONTROL_STRING
            }
            State.CONTROL_STRING_ESCAPE -> state = when (character) {
                '\\', C1_STRING_TERMINATOR -> State.TEXT
                ESCAPE -> State.CONTROL_STRING_ESCAPE
                else -> State.CONTROL_STRING
            }
        }
    }

    private companion object {
        const val ESCAPE = '\u001B'
        const val BELL = '\u0007'
        const val DELETE = '\u007F'
        const val C1_CSI = '\u009B'
        const val C1_OSC = '\u009D'
        const val C1_STRING_TERMINATOR = '\u009C'
        val C1_CONTROL_STRING_INTRODUCERS = setOf('\u0090', '\u0098', '\u009E', '\u009F')
        val ALLOWED_CONTROLS = setOf('\n', '\r', '\t')
        val CSI_FINAL_RANGE = '@'..'~'
    }
}
