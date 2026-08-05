package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals

class HermesProcessRailTest {
    @Test
    fun `last branch projects ancestor continuation and elbow as drawable segments`() {
        val spec = HermesProcessRailSpec.project(
            ancestorContinuations = listOf(true, false),
            branchLast = true,
        )

        assertEquals(3, spec.widthUnits)
        assertEquals(
            listOf(
                HermesProcessRailSegment(0.5f, 0f, 0.5f, 1f),
                HermesProcessRailSegment(2.5f, 0f, 2.5f, 0.5f),
                HermesProcessRailSegment(2.5f, 0.5f, 3f, 0.5f),
            ),
            spec.segments,
        )
    }
}
