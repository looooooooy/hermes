package app.hermesmobile.sessions

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import app.hermesmobile.ui.theme.HermesMobileTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class TranscriptMinimapDensityTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun denseSmallTrackDrawsSeparatedLinesWhileEveryMarkerRemainsNavigable() {
        val markers = (0 until 100).map { index ->
            TranscriptMinimapMarker(
                key = "marker-$index",
                turnKey = "turn-$index",
                turnOrdinal = index + 1,
                kind = TranscriptMinimapMarkerKind.TURN,
                depth = 0,
                status = when (index) {
                    1 -> TranscriptMinimapStatus.ERROR
                    98 -> TranscriptMinimapStatus.RUNNING
                    else -> TranscriptMinimapStatus.COMPLETE
                },
                summary = "Marker $index",
            )
        }
        var selectedMarkerIndex: Int? = null
        composeRule.setContent {
            HermesMobileTheme(darkTheme = false) {
                Box(
                    modifier = Modifier
                        .size(width = 48.dp, height = 120.dp)
                        .background(Color.White),
                ) {
                    TranscriptMinimap(
                        markers = markers,
                        activeMarkerIndex = 50,
                        onMarkerSelected = { marker ->
                            selectedMarkerIndex = markers.indexOf(marker)
                        },
                        modifier = Modifier.fillMaxSize(),
                    )
                }
            }
        }

        composeRule.onNodeWithTag("transcript-minimap")
            .assert(
                SemanticsMatcher.expectValue(
                    TranscriptMinimapNavigationTargetCountKey,
                    100,
                ),
            )
        composeRule.onNodeWithTag(
            "transcript-minimap-visual-layer",
            useUnmergedTree = true,
        ).assert(
            SemanticsMatcher.expectValue(
                TranscriptMinimapVisualMarkerCountKey,
                15,
            ),
        )
        composeRule.onNodeWithTag("transcript-minimap-marker:2")
            .performClick()
        composeRule.runOnIdle {
            assertEquals(1, selectedMarkerIndex)
        }
        composeRule.onNodeWithTag("transcript-minimap-marker:99")
            .performClick()
        composeRule.runOnIdle {
            assertEquals(98, selectedMarkerIndex)
        }

        val pixels = composeRule.onNodeWithTag("transcript-minimap")
            .captureToImage()
            .toPixelMap()
        val inkRows = (0 until pixels.height).filter { y ->
            (pixels.width / 4 until pixels.width).any { x ->
                val color = pixels[x, y]
                color.alpha > 0.5f &&
                    (
                        color.red < 0.93f ||
                            color.green < 0.93f ||
                            color.blue < 0.93f
                        )
            }
        }
        val runs = inkRows.fold(mutableListOf<IntRange>()) { ranges, row ->
            val last = ranges.lastOrNull()
            if (last == null || row > last.last + 1) {
                ranges += row..row
            } else {
                ranges[ranges.lastIndex] = last.first..row
            }
            ranges
        }

        assertEquals(15, runs.size)
        runs.zipWithNext { first, second ->
            assertTrue(second.first - first.last > 1)
        }
    }
}
