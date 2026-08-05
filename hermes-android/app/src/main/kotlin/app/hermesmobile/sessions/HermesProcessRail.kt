package app.hermesmobile.sessions

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.unit.dp

internal data class HermesProcessRailSegment(
    val startXUnits: Float,
    val startYFraction: Float,
    val endXUnits: Float,
    val endYFraction: Float,
)

internal data class HermesProcessRailSpec(
    val widthUnits: Int,
    val segments: List<HermesProcessRailSegment>,
) {
    companion object {
        fun project(
            ancestorContinuations: List<Boolean>,
            branchLast: Boolean,
        ): HermesProcessRailSpec {
            val branchX = ancestorContinuations.size + 0.5f
            return HermesProcessRailSpec(
                widthUnits = ancestorContinuations.size + 1,
                segments = buildList {
                    ancestorContinuations.forEachIndexed { index, continues ->
                        if (continues) {
                            add(HermesProcessRailSegment(index + 0.5f, 0f, index + 0.5f, 1f))
                        }
                    }
                    add(
                        HermesProcessRailSegment(
                            branchX,
                            0f,
                            branchX,
                            if (branchLast) 0.5f else 1f,
                        ),
                    )
                    add(
                        HermesProcessRailSegment(
                            branchX,
                            0.5f,
                            ancestorContinuations.size + 1f,
                            0.5f,
                        ),
                    )
                },
            )
        }
    }
}

@Composable
internal fun HermesProcessRail(
    ancestorContinuations: List<Boolean>,
    branchLast: Boolean,
    modifier: Modifier = Modifier,
) {
    val spec = remember(ancestorContinuations, branchLast) {
        HermesProcessRailSpec.project(ancestorContinuations, branchLast)
    }
    val lineColor = MaterialTheme.colorScheme.outlineVariant
    Canvas(
        modifier = modifier
            .width((spec.widthUnits * RAIL_SLOT_WIDTH_DP).dp)
            .fillMaxHeight()
            .defaultMinSize(minHeight = RAIL_MIN_HEIGHT_DP.dp),
    ) {
        val unitWidth = size.width / spec.widthUnits
        spec.segments.forEach { segment ->
            drawLine(
                color = lineColor,
                start = Offset(
                    x = segment.startXUnits * unitWidth,
                    y = segment.startYFraction * size.height,
                ),
                end = Offset(
                    x = segment.endXUnits * unitWidth,
                    y = segment.endYFraction * size.height,
                ),
                strokeWidth = RAIL_STROKE_WIDTH_DP.dp.toPx(),
                cap = StrokeCap.Round,
            )
        }
    }
}

private const val RAIL_SLOT_WIDTH_DP = 12
private const val RAIL_MIN_HEIGHT_DP = 20
private const val RAIL_STROKE_WIDTH_DP = 1.5f
