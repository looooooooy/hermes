package app.hermesmobile.sessions

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.IconButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

internal enum class HermesPreformattedContentKind(val wireName: String) {
    CODE("code"),
    DIFF("diff"),
    TOOL_OUTPUT("tool-output"),
    COMMAND("command"),
}

internal val HermesPreformattedContentKind.defaultExpanded: Boolean
    get() = this == HermesPreformattedContentKind.COMMAND ||
        this == HermesPreformattedContentKind.TOOL_OUTPUT

@Immutable
internal data class HermesPreformattedContentPresentation(
    val source: String,
    val kind: HermesPreformattedContentKind,
    val explicitLineCount: Int,
    val softWrap: Boolean,
    val scrollsHorizontally: Boolean,
)

internal fun presentHermesPreformattedContent(
    source: String,
    kind: HermesPreformattedContentKind,
): HermesPreformattedContentPresentation = HermesPreformattedContentPresentation(
    source = source,
    kind = kind,
    explicitLineCount = if (source.isEmpty()) 0 else source.count { it == '\n' } + 1,
    softWrap = false,
    scrollsHorizontally = true,
)

/**
 * Shared professional renderer for code, diffs, commands, and tool output.
 * Explicit source line breaks are preserved; long lines never wrap and scroll only inside this surface.
 */
@Composable
internal fun HermesPreformattedContent(
    source: String,
    kind: HermesPreformattedContentKind,
    modifier: Modifier = Modifier,
    annotatedSource: AnnotatedString = AnnotatedString(source),
    copySource: String = source,
    label: String? = null,
    copyLabel: String? = null,
    copyButtonTag: String? = null,
    scrollTag: String = "preformatted-${kind.wireName}",
    expanded: Boolean = kind.defaultExpanded,
    onExpandedChange: ((Boolean) -> Unit)? = null,
    expandContentDescription: String = "Expand ${kind.wireName}",
    collapseContentDescription: String = "Collapse ${kind.wireName}",
    extraActionContentDescription: String? = null,
    extraActionTag: String? = null,
    onExtraAction: (() -> Unit)? = null,
    textStyle: TextStyle = HermesTranscriptThemeTokens.typography.code,
    textColor: Color = HermesTranscriptThemeTokens.colors.tool,
    containerColor: Color = HermesTranscriptThemeTokens.colors.statusBackground,
) {
    val presentation = remember(source, kind) {
        presentHermesPreformattedContent(source = source, kind = kind)
    }
    val clipboard = LocalClipboardManager.current
    val colors = HermesTranscriptThemeTokens.colors
    val horizontalScrollState = rememberScrollState()

    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = androidx.compose.material3.MaterialTheme.shapes.small,
        color = containerColor,
        border = BorderStroke(1.dp, colors.borderRail.copy(alpha = 0.68f)),
        tonalElevation = 0.dp,
    ) {
        Column {
            if (label != null || copyLabel != null || onExpandedChange != null) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 2.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    if (onExpandedChange != null) {
                        IconButton(
                            onClick = { onExpandedChange(!expanded) },
                            modifier = Modifier.semantics {
                                contentDescription = if (expanded) {
                                    collapseContentDescription
                                } else {
                                    expandContentDescription
                                }
                            },
                        ) {
                            HermesTranscriptChevron(
                                expanded = expanded,
                                color = colors.accent,
                            )
                        }
                    }
                    if (label != null) {
                        Text(
                            text = label,
                            modifier = Modifier.weight(1f),
                            style = HermesTranscriptThemeTokens.typography.meta,
                            color = colors.muted,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    } else {
                        Box(modifier = Modifier.weight(1f))
                    }
                    if (extraActionContentDescription != null && onExtraAction != null) {
                        IconButton(
                            onClick = onExtraAction,
                            modifier = (if (extraActionTag == null) {
                                Modifier
                            } else {
                                Modifier.testTag(extraActionTag)
                            }).semantics {
                                contentDescription = extraActionContentDescription
                            },
                        ) {
                            HermesFullOutputIcon(color = colors.muted)
                        }
                    }
                    if (copyLabel != null) {
                        IconButton(
                            onClick = { clipboard.setText(AnnotatedString(copySource)) },
                            modifier = if (copyButtonTag == null) {
                                Modifier.semantics { contentDescription = copyLabel }
                            } else {
                                Modifier
                                    .testTag(copyButtonTag)
                                    .semantics { contentDescription = copyLabel }
                            },
                        ) {
                            HermesCopyIcon(color = colors.muted)
                        }
                    }
                }
            }
            if (expanded) {
                SelectionContainer {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .horizontalScroll(horizontalScrollState)
                            .testTag(scrollTag),
                    ) {
                        Text(
                            text = annotatedSource,
                            modifier = Modifier.padding(
                                start = 12.dp,
                                end = 12.dp,
                                top = if (label == null && copyLabel == null) 10.dp else 4.dp,
                                bottom = 10.dp,
                            ),
                            style = textStyle,
                            color = textColor,
                            softWrap = presentation.softWrap,
                            overflow = TextOverflow.Clip,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun HermesCopyIcon(color: Color) {
    Canvas(modifier = Modifier.size(18.dp)) {
        val stroke = 1.4.dp.toPx()
        drawRect(
            color = color,
            topLeft = Offset(size.width * 0.18f, size.height * 0.28f),
            size = androidx.compose.ui.geometry.Size(size.width * 0.54f, size.height * 0.54f),
            style = androidx.compose.ui.graphics.drawscope.Stroke(width = stroke),
        )
        drawRect(
            color = color,
            topLeft = Offset(size.width * 0.32f, size.height * 0.14f),
            size = androidx.compose.ui.geometry.Size(size.width * 0.54f, size.height * 0.54f),
            style = androidx.compose.ui.graphics.drawscope.Stroke(width = stroke),
        )
    }
}

@Composable
private fun HermesFullOutputIcon(color: Color) {
    Canvas(modifier = Modifier.size(18.dp)) {
        val stroke = 1.4.dp.toPx()
        val centerX = size.width / 2f
        drawLine(color, Offset(centerX, size.height * 0.18f), Offset(centerX, size.height * 0.82f), stroke)
        drawLine(color, Offset(centerX, size.height * 0.18f), Offset(size.width * 0.30f, size.height * 0.38f), stroke)
        drawLine(color, Offset(centerX, size.height * 0.18f), Offset(size.width * 0.70f, size.height * 0.38f), stroke)
        drawLine(color, Offset(centerX, size.height * 0.82f), Offset(size.width * 0.30f, size.height * 0.62f), stroke)
        drawLine(color, Offset(centerX, size.height * 0.82f), Offset(size.width * 0.70f, size.height * 0.62f), stroke)
    }
}
