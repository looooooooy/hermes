package app.hermesmobile.sessions

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

@Composable
internal fun ConversationTurnContent(
    turn: ConversationTurnUiModel,
    modifier: Modifier = Modifier,
    disclosureState: ConversationDisclosureState? = null,
    onDisclosureToggle: ((ConversationDisclosureStateKey) -> Unit)? = null,
) {
    HermesCanonicalConversationTurnContent(
        turn = turn,
        modifier = modifier,
        disclosureState = disclosureState,
        onDisclosureToggle = onDisclosureToggle,
    )
}