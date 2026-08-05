package app.hermesmobile.sessions

import android.animation.ValueAnimator
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@Immutable
internal data class HermesTranscriptRailSegment(
    val startXSlots: Float,
    val startYFraction: Float,
    val endXSlots: Float,
    val endYFraction: Float,
)

@Immutable
internal data class HermesTranscriptRailPoint(
    val xSlots: Float,
    val yFraction: Float,
)

@Immutable
internal data class HermesTranscriptRailSpec(
    val widthSlots: Int,
    val segments: List<HermesTranscriptRailSegment>,
    val node: HermesTranscriptRailPoint,
) {
    companion object {
        fun project(
            ancestorContinuations: List<Boolean>,
            branchLast: Boolean,
        ): HermesTranscriptRailSpec {
            val branchX = ancestorContinuations.size + 0.5f
            val node = HermesTranscriptRailPoint(
                xSlots = ancestorContinuations.size + 1f,
                yFraction = 0.5f,
            )
            return HermesTranscriptRailSpec(
                widthSlots = ancestorContinuations.size + 1,
                segments = buildList {
                    ancestorContinuations.forEachIndexed { index, continues ->
                        if (continues) {
                            add(
                                HermesTranscriptRailSegment(
                                    startXSlots = index + 0.5f,
                                    startYFraction = 0f,
                                    endXSlots = index + 0.5f,
                                    endYFraction = 1f,
                                ),
                            )
                        }
                    }
                    add(
                        HermesTranscriptRailSegment(
                            startXSlots = branchX,
                            startYFraction = 0f,
                            endXSlots = branchX,
                            endYFraction = if (branchLast) 0.5f else 1f,
                        ),
                    )
                    add(
                        HermesTranscriptRailSegment(
                            startXSlots = branchX,
                            startYFraction = 0.5f,
                            endXSlots = node.xSlots,
                            endYFraction = node.yFraction,
                        ),
                    )
                },
                node = node,
            )
        }
    }
}

@Immutable
internal data class HermesTranscriptRailHorizontalGeometry(
    val slotWidthPx: Float,
    val nodeCenterXPx: Float,
)

internal fun projectHermesTranscriptRailHorizontalGeometry(
    spec: HermesTranscriptRailSpec,
    canvasWidthPx: Float,
    nodeDiameterPx: Float,
): HermesTranscriptRailHorizontalGeometry {
    val nodeRadiusPx = nodeDiameterPx / 2f
    val drawableWidthPx = (canvasWidthPx - nodeRadiusPx).coerceAtLeast(0f)
    val slotWidthPx = drawableWidthPx / spec.widthSlots
    return HermesTranscriptRailHorizontalGeometry(
        slotWidthPx = slotWidthPx,
        nodeCenterXPx = spec.node.xSlots * slotWidthPx,
    )
}

@Immutable
internal data class HermesStatusDotPresentation(
    val showCore: Boolean,
    val showRing: Boolean,
    val pulses: Boolean,
)

internal fun hermesStatusDotPresentation(
    status: HermesTranscriptStatus,
    motionEnabled: Boolean,
): HermesStatusDotPresentation = when (status) {
    HermesTranscriptStatus.Pending -> HermesStatusDotPresentation(
        showCore = false,
        showRing = true,
        pulses = false,
    )
    HermesTranscriptStatus.Running -> HermesStatusDotPresentation(
        showCore = true,
        showRing = true,
        pulses = motionEnabled,
    )
    HermesTranscriptStatus.Complete -> HermesStatusDotPresentation(
        showCore = true,
        showRing = false,
        pulses = false,
    )
    else -> HermesStatusDotPresentation(
        showCore = true,
        showRing = true,
        pulses = false,
    )
}

/** A Compose Canvas rail: native lines and a native node, never Unicode indentation. */
@Composable
internal fun HermesTranscriptProcessRail(
    ancestorContinuations: List<Boolean>,
    branchLast: Boolean,
    modifier: Modifier = Modifier,
    showNode: Boolean = true,
    nodeStatus: HermesTranscriptStatus? = null,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    val spec = remember(ancestorContinuations, branchLast) {
        HermesTranscriptRailSpec.project(ancestorContinuations, branchLast)
    }
    val nodePresentation = nodeStatus?.let { status ->
        hermesStatusDotPresentation(
            status = status,
            motionEnabled = ValueAnimator.areAnimatorsEnabled(),
        )
    }
    val nodeColor = nodeStatus?.let(colors::statusColors)?.foreground ?: colors.accent
    val ringAlpha = rememberHermesStatusRingAlpha(
        pulses = nodePresentation?.pulses == true,
        transitionLabel = "Hermes process node pulse",
        valueLabel = "Hermes process node ring alpha",
    )
    Canvas(
        modifier = modifier
            .width(metrics.railSlotWidth * spec.widthSlots)
            .fillMaxHeight()
            .defaultMinSize(minHeight = metrics.railMinimumHeight),
    ) {
        val radius = metrics.railNodeDiameter.toPx() / 2f
        val horizontalGeometry = projectHermesTranscriptRailHorizontalGeometry(
            spec = spec,
            canvasWidthPx = size.width,
            nodeDiameterPx = if (showNode) metrics.railNodeDiameter.toPx() else 0f,
        )
        val slotWidth = horizontalGeometry.slotWidthPx
        spec.segments.forEach { segment ->
            drawLine(
                color = colors.borderRail,
                start = Offset(
                    x = segment.startXSlots * slotWidth,
                    y = segment.startYFraction * size.height,
                ),
                end = Offset(
                    x = segment.endXSlots * slotWidth,
                    y = segment.endYFraction * size.height,
                ),
                strokeWidth = metrics.railStrokeWidth.toPx(),
                cap = StrokeCap.Round,
            )
        }
        if (showNode) {
            val center = Offset(
                x = horizontalGeometry.nodeCenterXPx,
                y = spec.node.yFraction * size.height,
            )
            if (nodePresentation == null || nodePresentation.showCore) {
                drawCircle(
                    color = nodeColor,
                    radius = if (nodePresentation == null) radius else radius * 0.66f,
                    center = center,
                )
            }
            if (nodePresentation?.showRing == true) {
                drawCircle(
                    color = nodeColor.copy(alpha = ringAlpha),
                    radius = radius,
                    center = center,
                    style = androidx.compose.ui.graphics.drawscope.Stroke(width = 1.dp.toPx()),
                )
            }
        }
    }
}

@Composable
internal fun HermesTranscriptStatusDot(
    status: HermesTranscriptStatus,
    color: androidx.compose.ui.graphics.Color,
    modifier: Modifier = Modifier,
) {
    val presentation = hermesStatusDotPresentation(
        status = status,
        motionEnabled = ValueAnimator.areAnimatorsEnabled(),
    )
    val ringAlpha = rememberHermesStatusRingAlpha(
        pulses = presentation.pulses,
        transitionLabel = "Hermes standalone status pulse",
        valueLabel = "Hermes standalone status ring alpha",
    )
    Canvas(modifier = modifier.size(8.dp)) {
        if (presentation.showCore) {
            drawCircle(
                color = color,
                radius = size.minDimension / 3f,
                center = Offset(size.width / 2f, size.height / 2f),
            )
        }
        if (presentation.showRing) {
            drawCircle(
                color = color.copy(alpha = ringAlpha),
                radius = size.minDimension / 2f,
                center = Offset(size.width / 2f, size.height / 2f),
                style = androidx.compose.ui.graphics.drawscope.Stroke(width = 1.dp.toPx()),
            )
        }
    }
}

@Composable
private fun rememberHermesStatusRingAlpha(
    pulses: Boolean,
    transitionLabel: String,
    valueLabel: String,
): Float = if (pulses) {
        val transition = rememberInfiniteTransition(label = transitionLabel)
        val alpha by transition.animateFloat(
            initialValue = 0.35f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(
                animation = tween(durationMillis = 900),
                repeatMode = RepeatMode.Reverse,
            ),
            label = valueLabel,
        )
        alpha
    } else {
        1f
    }

/** Flat user input line with the Hermes prompt marker and selectable multiline text. */
@Composable
internal fun HermesTranscriptPrompt(
    text: String,
    modifier: Modifier = Modifier,
    marker: String = "❯",
    semanticLabel: String = "User prompt",
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    val typography = HermesTranscriptThemeTokens.typography
    Row(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = metrics.minimumTouchTarget)
            .semantics(mergeDescendants = true) {
                contentDescription = semanticLabel
            },
        verticalAlignment = Alignment.Top,
    ) {
        Box(
            modifier = Modifier
                .width(metrics.processGutter)
                .padding(top = 1.dp),
            contentAlignment = Alignment.TopStart,
        ) {
            Text(
                text = marker,
                style = typography.body,
                fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.Bold,
                color = colors.prompt,
            )
        }
        SelectionContainer(modifier = Modifier.weight(1f)) {
            Text(
                text = text,
                style = typography.body,
                color = colors.text,
            )
        }
    }
}

/** Explicit boundary between process output and the assistant response hierarchy. */
@Composable
internal fun HermesTranscriptResponseBoundary(
    modifier: Modifier = Modifier,
    label: String = "Response",
    semanticLabel: String = "Response boundary",
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    val typography = HermesTranscriptThemeTokens.typography
    Row(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = metrics.railMinimumHeight)
            .height(IntrinsicSize.Min)
            .semantics(mergeDescendants = true) {
                contentDescription = semanticLabel
            },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        HermesTranscriptProcessRail(
            ancestorContinuations = emptyList(),
            branchLast = true,
        )
        Text(
            text = label.uppercase(),
            style = typography.meta,
            fontWeight = FontWeight.Bold,
            letterSpacing = 0.8.sp,
            color = colors.accent,
        )
    }
}

/** Native response spine used beside assistant Markdown; no terminal glyphs. */
@Composable
internal fun HermesTranscriptResponseSpine(
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    Canvas(
        modifier = modifier
            .width(metrics.processGutter)
            .fillMaxHeight()
            .defaultMinSize(minHeight = metrics.railMinimumHeight),
    ) {
        val x = size.width / 2f
        drawLine(
            color = colors.accent,
            start = Offset(x, 0f),
            end = Offset(x, size.height),
            strokeWidth = metrics.railStrokeWidth.toPx(),
            cap = StrokeCap.Round,
        )
    }
}

/** Accessible disclosure control with a native chevron and a full 48dp target. */
@Composable
internal fun HermesTranscriptDisclosureHeader(
    title: String,
    expanded: Boolean,
    onExpandedChange: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
    status: HermesTranscriptStatus = HermesTranscriptStatus.Idle,
    ancestorContinuations: List<Boolean> = emptyList(),
    branchLast: Boolean = true,
    expandedStateDescription: String = "Expanded",
    collapsedStateDescription: String = "Collapsed",
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    val typography = HermesTranscriptThemeTokens.typography
    val tone = colors.statusColors(status)
    Row(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = metrics.minimumTouchTarget)
            .height(IntrinsicSize.Min)
            .toggleable(
                value = expanded,
                role = Role.Button,
                onValueChange = onExpandedChange,
            )
            .semantics(mergeDescendants = true) {
                contentDescription = title
                stateDescription = if (expanded) {
                    expandedStateDescription
                } else {
                    collapsedStateDescription
                }
                role = Role.Button
            },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        HermesTranscriptProcessRail(
            ancestorContinuations = ancestorContinuations,
            branchLast = branchLast,
            nodeStatus = status,
        )
        HermesTranscriptChevron(
            expanded = expanded,
            color = colors.accent,
        )
        Text(
            text = title,
            modifier = Modifier
                .weight(1f)
                .padding(start = metrics.sectionGap),
            style = typography.process,
            fontWeight = if (status == HermesTranscriptStatus.Running) {
                FontWeight.Bold
            } else {
                FontWeight.Normal
            },
            color = tone.foreground,
        )
    }
}

@Composable
internal fun HermesTranscriptChevron(
    expanded: Boolean,
    color: androidx.compose.ui.graphics.Color,
) {
    val metrics = HermesTranscriptThemeTokens.metrics
    Canvas(modifier = Modifier.size(16.dp)) {
        val strokeWidth = metrics.railStrokeWidth.toPx()
        if (expanded) {
            drawLine(
                color = color,
                start = Offset(size.width * 0.2f, size.height * 0.35f),
                end = Offset(size.width * 0.5f, size.height * 0.65f),
                strokeWidth = strokeWidth,
                cap = StrokeCap.Round,
            )
            drawLine(
                color = color,
                start = Offset(size.width * 0.5f, size.height * 0.65f),
                end = Offset(size.width * 0.8f, size.height * 0.35f),
                strokeWidth = strokeWidth,
                cap = StrokeCap.Round,
            )
        } else {
            drawLine(
                color = color,
                start = Offset(size.width * 0.35f, size.height * 0.2f),
                end = Offset(size.width * 0.65f, size.height * 0.5f),
                strokeWidth = strokeWidth,
                cap = StrokeCap.Round,
            )
            drawLine(
                color = color,
                start = Offset(size.width * 0.65f, size.height * 0.5f),
                end = Offset(size.width * 0.35f, size.height * 0.8f),
                strokeWidth = strokeWidth,
                cap = StrokeCap.Round,
            )
        }
    }
}
