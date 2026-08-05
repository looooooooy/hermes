package app.hermesmobile.sessions

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.PointerInputScope
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.SemanticsPropertyKey
import androidx.compose.ui.semantics.SemanticsPropertyReceiver
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import app.hermesmobile.R

internal val TranscriptMinimapVisualMarkerCountKey =
    SemanticsPropertyKey<Int>("TranscriptMinimapVisualMarkerCount")
internal val TranscriptMinimapNavigationTargetCountKey =
    SemanticsPropertyKey<Int>("TranscriptMinimapNavigationTargetCount")
private var SemanticsPropertyReceiver.transcriptMinimapVisualMarkerCount by
    TranscriptMinimapVisualMarkerCountKey
private var SemanticsPropertyReceiver.transcriptMinimapNavigationTargetCount by
    TranscriptMinimapNavigationTargetCountKey

@Composable
internal fun TranscriptMinimap(
    markers: List<TranscriptMinimapMarker>,
    activeMarkerIndex: Int?,
    onMarkerSelected: (TranscriptMinimapMarker) -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = HermesTranscriptThemeTokens.colors
    val metrics = HermesTranscriptThemeTokens.metrics
    val maximumMarkerStroke = 3.dp
    val minimumMarkerGap = 2.dp
    val selectAtY: PointerInputScope.(Float) -> Unit = { pointerY ->
        val touchTarget = metrics.minimumTouchTarget.toPx()
        val trackStartY = touchTarget / 2f
        val trackHeight = (size.height - touchTarget).coerceAtLeast(0f)
        val visualLayout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = activeMarkerIndex,
            trackStartY = trackStartY,
            trackHeight = trackHeight,
            markerStrokeWidth = maximumMarkerStroke.toPx(),
            minimumGap = minimumMarkerGap.toPx(),
        )
        val visibleMarkerHitRadius = transcriptMinimapVisibleMarkerHitRadius(
            trackHeight = trackHeight,
            markerCount = markers.size,
            preferredHitRadius = (maximumMarkerStroke + minimumMarkerGap).toPx() / 2f,
        )
        transcriptMinimapPointerTargetMarkerIndex(
            pointerY = pointerY,
            trackStartY = trackStartY,
            trackHeight = trackHeight,
            markerCount = markers.size,
            visualLayout = visualLayout,
            visibleMarkerHitRadius = visibleMarkerHitRadius,
        )?.let { index -> onMarkerSelected(markers[index]) }
    }

    BoxWithConstraints(
        modifier = modifier
            .width(metrics.minimumTouchTarget)
            .fillMaxHeight()
            .clipToBounds()
            .pointerInput(markers, activeMarkerIndex) {
                detectTapGestures { position ->
                    selectAtY(position.y)
                }
            }
            .pointerInput(markers, activeMarkerIndex) {
                detectDragGestures(
                    onDragStart = { position ->
                        selectAtY(position.y)
                    },
                    onDrag = { change, _ ->
                        change.consume()
                        selectAtY(change.position.y)
                    },
                )
            }
            .semantics {
                transcriptMinimapNavigationTargetCount = markers.size
            }
            .testTag("transcript-minimap"),
    ) {
        val visualLayout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = activeMarkerIndex,
            trackStartY = metrics.minimumTouchTarget.value / 2f,
            trackHeight = (maxHeight - metrics.minimumTouchTarget)
                .coerceAtLeast(0.dp)
                .value,
            markerStrokeWidth = maximumMarkerStroke.value,
            minimumGap = minimumMarkerGap.value,
        )
        Box(
            modifier = Modifier
                .matchParentSize()
                .semantics {
                    transcriptMinimapVisualMarkerCount = visualLayout.size
                }
                .testTag("transcript-minimap-visual-layer"),
        )
        Canvas(Modifier.matchParentSize()) {
            visualLayout.forEach { visualMarker ->
                val index = visualMarker.markerIndex
                val marker = markers[index]
                val y = visualMarker.centerY.dp.toPx()
                val active = index == activeMarkerIndex
                val lineWidth = when (marker.kind) {
                    TranscriptMinimapMarkerKind.TURN -> 22.dp.toPx()
                    TranscriptMinimapMarkerKind.TODO -> 15.dp.toPx()
                    TranscriptMinimapMarkerKind.SUBAGENT -> 13.dp.toPx()
                }
                val depthInset = (marker.depth * 3).dp.toPx()
                val endX = size.width - 6.dp.toPx() - depthInset
                val color = when {
                    active -> colors.accent
                    marker.status == TranscriptMinimapStatus.ERROR -> colors.error
                    marker.status == TranscriptMinimapStatus.RUNNING -> colors.warning
                    marker.status == TranscriptMinimapStatus.COMPLETE -> colors.muted
                    else -> colors.borderRail
                }
                drawLine(
                    color = color,
                    start = Offset(endX - lineWidth, y),
                    end = Offset(endX, y),
                    strokeWidth = if (active) 3.dp.toPx() else 1.5.dp.toPx(),
                    cap = StrokeCap.Round,
                )
            }
        }

        visualLayout.forEach { visualMarker ->
            val marker = markers[visualMarker.markerIndex]
            val statusLabel = stringResource(marker.status.labelResource())
            val description = if (
                visualMarker.bucketStartIndex == visualMarker.bucketEndIndex
            ) {
                stringResource(
                    R.string.transcript_minimap_turn_description,
                    marker.turnOrdinal,
                    statusLabel,
                    marker.summary,
                )
            } else {
                stringResource(
                    R.string.transcript_minimap_range_description,
                    visualMarker.bucketStartIndex + 1,
                    visualMarker.bucketEndIndex + 1,
                    marker.turnOrdinal,
                    statusLabel,
                    marker.summary,
                )
            }
            val targetTop = (
                visualMarker.centerY.dp - metrics.minimumTouchTarget / 2
                ).coerceIn(
                minimumValue = 0.dp,
                maximumValue = (maxHeight - metrics.minimumTouchTarget)
                    .coerceAtLeast(0.dp),
            )
            Box(
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .offset(y = targetTop)
                    .width(metrics.minimumTouchTarget)
                    .height(metrics.minimumTouchTarget)
                    .then(
                        if (marker.kind == TranscriptMinimapMarkerKind.TURN) {
                            Modifier.testTag(
                                "transcript-minimap-marker:${marker.turnOrdinal}",
                            )
                        } else {
                            Modifier
                        },
                    )
                    .clearAndSetSemantics {
                        contentDescription = description
                        onClick(label = description) {
                            onMarkerSelected(marker)
                            true
                        }
                    },
            )
        }
    }
}

private fun TranscriptMinimapStatus.labelResource(): Int = when (this) {
    TranscriptMinimapStatus.PENDING -> R.string.long_running_waiting
    TranscriptMinimapStatus.RUNNING -> R.string.long_running_running
    TranscriptMinimapStatus.COMPLETE -> R.string.long_running_complete
    TranscriptMinimapStatus.INTERRUPTED -> R.string.long_running_interrupted
    TranscriptMinimapStatus.ERROR -> R.string.long_running_failed
}
