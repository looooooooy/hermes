package app.hermesmobile.sessions

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.hermesmobile.R
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class HermesTranscriptDesignSystemTest {
    @Test
    fun `dark tokens match the approved neutral cyan mobile transcript direction`() {
        val colors = HermesTranscriptDesignSystem.darkColors

        assertEquals(Color(0xFF0A0C0F), colors.background)
        assertEquals(Color(0xFF65D2E2), colors.accent)
        assertEquals(Color(0xFF272C33), colors.borderRail)
        assertEquals(Color(0xFFF0F1F3), colors.text)
        assertEquals(Color(0xFF8B929D), colors.muted)
        assertEquals(Color(0xFF65D2E2), colors.prompt)
        assertEquals(Color(0xFFC9CDD3), colors.tool)
        assertEquals(Color(0xFF0E1115), colors.activeBackground)
        assertEquals(Color(0xFF13161B), colors.statusBackground)
        assertEquals(Color(0xFFE7B56D), colors.warning)
        assertEquals(Color(0xFF73D7A1), colors.success)
        assertEquals(Color(0xFFEB9898), colors.error)
    }

    @Test
    fun `light tokens preserve the same neutral cyan hierarchy`() {
        val colors = HermesTranscriptDesignSystem.lightColors

        assertEquals(Color(0xFFF6F7F9), colors.background)
        assertEquals(Color(0xFF006F80), colors.accent)
        assertEquals(Color(0xFF8A939F), colors.borderRail)
        assertEquals(Color(0xFF1B1F24), colors.text)
        assertEquals(Color(0xFF005F6E), colors.prompt)
        assertEquals(Color(0xFF425466), colors.tool)
    }

    @Test
    fun `light normal text accent tool and muted combinations meet WCAG AA contrast`() {
        val colors = HermesTranscriptDesignSystem.lightColors
        val foregrounds = mapOf(
            "accent" to colors.accent,
            "tool" to colors.tool,
            "muted" to colors.muted,
        )
        val backgrounds = mapOf(
            "background" to colors.background,
            "statusBackground" to colors.statusBackground,
        )

        foregrounds.forEach { (foregroundName, foreground) ->
            backgrounds.forEach { (backgroundName, background) ->
                val ratio = contrastRatio(foreground, background)
                assertTrue(
                    ratio >= 4.5f,
                    "$foregroundName on $backgroundName contrast was $ratio; expected at least 4.5",
                )
            }
        }
    }

    @Test
    fun `density and typography match the mobile transcript contract`() {
        val metrics = HermesTranscriptDesignSystem.metrics
        val type = HermesTranscriptDesignSystem.typography

        assertEquals(16.dp, metrics.horizontalContentInset)
        assertEquals(24.dp, metrics.processGutter)
        assertEquals(18.dp, metrics.turnGap)
        assertEquals(6.dp, metrics.sectionGap)
        assertEquals(8.dp, metrics.containmentRadius)
        assertEquals(48.dp, metrics.minimumTouchTarget)

        assertEquals(16.sp, type.body.fontSize)
        assertEquals(24.sp, type.body.lineHeight)
        assertEquals(14.sp, type.process.fontSize)
        assertEquals(20.sp, type.process.lineHeight)
        assertEquals(FontFamily.Monospace, type.process.fontFamily)
        assertEquals(13.sp, type.meta.fontSize)
        assertEquals(19.sp, type.meta.lineHeight)
        assertEquals(FontFamily.Monospace, type.meta.fontFamily)
        assertEquals(13.5.sp, type.code.fontSize)
        assertEquals(20.sp, type.code.lineHeight)
        assertEquals(FontFamily.Monospace, type.code.fontFamily)
    }

    @Test
    fun `status mapping reserves orange for uncertainty and red for failure`() {
        val colors = HermesTranscriptDesignSystem.darkColors

        assertEquals(colors.statusForeground, colors.statusColors(HermesTranscriptStatus.Idle).foreground)
        assertEquals(colors.accent, colors.statusColors(HermesTranscriptStatus.Running).foreground)
        assertEquals(colors.success, colors.statusColors(HermesTranscriptStatus.Complete).foreground)
        assertEquals(colors.warning, colors.statusColors(HermesTranscriptStatus.Interrupted).foreground)
        assertEquals(colors.warning, colors.statusColors(HermesTranscriptStatus.Warning).foreground)
        assertEquals(colors.error, colors.statusColors(HermesTranscriptStatus.Error).foreground)
        assertEquals(colors.critical, colors.statusColors(HermesTranscriptStatus.Critical).foreground)
    }

    @Test
    fun `tool lifecycle accessibility maps every status to localized copy and transcript tone`() {
        val expected = mapOf(
            ConversationToolStatus.RUNNING to HermesToolAccessibility(
                status = HermesTranscriptStatus.Running,
                stateDescriptionResource = R.string.tool_running,
            ),
            ConversationToolStatus.COMPLETE to HermesToolAccessibility(
                status = HermesTranscriptStatus.Complete,
                stateDescriptionResource = R.string.tool_complete,
            ),
            ConversationToolStatus.ERROR to HermesToolAccessibility(
                status = HermesTranscriptStatus.Error,
                stateDescriptionResource = R.string.tool_failed,
            ),
            ConversationToolStatus.INTERRUPTED to HermesToolAccessibility(
                status = HermesTranscriptStatus.Interrupted,
                stateDescriptionResource = R.string.tool_interrupted,
            ),
        )

        expected.forEach { (status, accessibility) ->
            assertEquals(accessibility, status.toTranscriptToolAccessibility())
        }
    }

    @Test
    fun `unknown tool lifecycle uses the neutral uncertainty status`() {
        val unknown = assertNotNull(
            ConversationToolStatus.entries.firstOrNull { it.name == "UNKNOWN" },
        )

        assertEquals(
            HermesTranscriptStatus.Unknown,
            unknown.toTranscriptToolAccessibility().status,
        )
    }

    @Test
    fun `native rail geometry projects continuations elbow and terminal node`() {
        val spec = HermesTranscriptRailSpec.project(
            ancestorContinuations = listOf(true, false),
            branchLast = true,
        )

        assertEquals(3, spec.widthSlots)
        assertEquals(
            listOf(
                HermesTranscriptRailSegment(0.5f, 0f, 0.5f, 1f),
                HermesTranscriptRailSegment(2.5f, 0f, 2.5f, 0.5f),
                HermesTranscriptRailSegment(2.5f, 0.5f, 3f, 0.5f),
            ),
            spec.segments,
        )
        assertEquals(HermesTranscriptRailPoint(3f, 0.5f), spec.node)
    }

    @Test
    fun `rail canvas reserves the node radius without disconnecting the elbow`() {
        val spec = HermesTranscriptRailSpec.project(
            ancestorContinuations = emptyList(),
            branchLast = true,
        )

        val geometry = projectHermesTranscriptRailHorizontalGeometry(
            spec = spec,
            canvasWidthPx = 24f,
            nodeDiameterPx = 6f,
        )

        assertEquals(21f, geometry.nodeCenterXPx)
        assertEquals(
            geometry.nodeCenterXPx,
            spec.segments.last().endXSlots * geometry.slotWidthPx,
        )
    }

    @Test
    fun `status dots distinguish waiting running and complete while honoring reduced motion`() {
        assertEquals(
            HermesStatusDotPresentation(showCore = false, showRing = true, pulses = false),
            hermesStatusDotPresentation(HermesTranscriptStatus.Pending, motionEnabled = true),
        )
        assertEquals(
            HermesStatusDotPresentation(showCore = true, showRing = true, pulses = true),
            hermesStatusDotPresentation(HermesTranscriptStatus.Running, motionEnabled = true),
        )
        assertEquals(
            HermesStatusDotPresentation(showCore = true, showRing = true, pulses = false),
            hermesStatusDotPresentation(HermesTranscriptStatus.Running, motionEnabled = false),
        )
        assertEquals(
            HermesStatusDotPresentation(showCore = true, showRing = false, pulses = false),
            hermesStatusDotPresentation(HermesTranscriptStatus.Complete, motionEnabled = true),
        )
    }

    @Test
    fun `long running todo and subagent states have explicit visible and accessibility copy`() {
        assertEquals(
            HermesLongRunningItemPresentation(
                status = HermesTranscriptStatus.Pending,
                labelResource = R.string.long_running_waiting,
            ),
            HermesConversationTodoStatus.PENDING.toLongRunningItemPresentation(),
        )
        assertEquals(
            HermesLongRunningItemPresentation(
                status = HermesTranscriptStatus.Running,
                labelResource = R.string.long_running_running,
            ),
            HermesConversationTodoStatus.IN_PROGRESS.toLongRunningItemPresentation(),
        )
        assertEquals(
            HermesLongRunningItemPresentation(
                status = HermesTranscriptStatus.Running,
                labelResource = R.string.long_running_running,
            ),
            HermesConversationSectionStatus.STREAMING.toLongRunningItemPresentation(),
        )
        assertEquals(
            HermesLongRunningItemPresentation(
                status = HermesTranscriptStatus.Error,
                labelResource = R.string.long_running_failed,
            ),
            HermesConversationSectionStatus.ERROR.toLongRunningItemPresentation(),
        )
    }

    private fun contrastRatio(foreground: Color, background: Color): Float {
        val lighter = maxOf(foreground.luminance(), background.luminance())
        val darker = minOf(foreground.luminance(), background.luminance())
        return (lighter + 0.05f) / (darker + 0.05f)
    }
}
