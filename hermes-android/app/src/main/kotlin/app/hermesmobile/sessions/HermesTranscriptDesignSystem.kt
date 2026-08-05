package app.hermesmobile.sessions

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** Semantic colors for the flat Hermes transcript surface. */
@Immutable
internal data class HermesTranscriptColorTokens(
    val background: Color,
    val activeBackground: Color,
    val accent: Color,
    val borderRail: Color,
    val error: Color,
    val muted: Color,
    val prompt: Color,
    val statusBackground: Color,
    val statusForeground: Color,
    val text: Color,
    val tool: Color,
    val warning: Color,
    val success: Color,
    val critical: Color,
) {
    fun statusColors(status: HermesTranscriptStatus): HermesTranscriptStatusColors =
        HermesTranscriptStatusColors(
            foreground = when (status) {
                HermesTranscriptStatus.Idle,
                HermesTranscriptStatus.Unknown,
                -> statusForeground

                HermesTranscriptStatus.Pending -> muted
                HermesTranscriptStatus.Running -> accent
                HermesTranscriptStatus.Complete -> success
                HermesTranscriptStatus.Warning,
                HermesTranscriptStatus.Interrupted,
                -> warning

                HermesTranscriptStatus.Error -> error
                HermesTranscriptStatus.Critical -> critical
            },
            background = statusBackground,
        )
}

/** Lifecycle vocabulary deliberately independent from any projector/model enum. */
internal enum class HermesTranscriptStatus {
    Idle,
    Pending,
    Running,
    Complete,
    Warning,
    Interrupted,
    Error,
    Critical,
    Unknown,
}

@Immutable
internal data class HermesTranscriptStatusColors(
    val foreground: Color,
    val background: Color,
)

@Immutable
internal data class HermesTranscriptMetrics(
    val horizontalContentInset: Dp,
    val processGutter: Dp,
    val turnGap: Dp,
    val sectionGap: Dp,
    val containmentRadius: Dp,
    val minimumTouchTarget: Dp,
    val railSlotWidth: Dp,
    val railMinimumHeight: Dp,
    val railStrokeWidth: Dp,
    val railNodeDiameter: Dp,
)

@Immutable
internal data class HermesTranscriptTypography(
    val body: TextStyle,
    val process: TextStyle,
    val meta: TextStyle,
    val code: TextStyle,
)

/**
 * Compose-native source of truth for transcript color, density, and type.
 * It mirrors Hermes identity without importing terminal-cell rendering idioms.
 */
internal object HermesTranscriptDesignSystem {
    val shapes = Shapes(
        extraSmall = RoundedCornerShape(6.dp),
        small = RoundedCornerShape(8.dp),
        medium = RoundedCornerShape(10.dp),
        large = RoundedCornerShape(12.dp),
        extraLarge = RoundedCornerShape(14.dp),
    )

    val darkColors = HermesTranscriptColorTokens(
        background = Color(0xFF0A0C0F),
        activeBackground = Color(0xFF0E1115),
        accent = Color(0xFF65D2E2),
        borderRail = Color(0xFF272C33),
        error = Color(0xFFEB9898),
        muted = Color(0xFF8B929D),
        prompt = Color(0xFF65D2E2),
        statusBackground = Color(0xFF13161B),
        statusForeground = Color(0xFFC8CDD3),
        text = Color(0xFFF0F1F3),
        tool = Color(0xFFC9CDD3),
        warning = Color(0xFFE7B56D),
        success = Color(0xFF73D7A1),
        critical = Color(0xFFEB9898),
    )

    val lightColors = HermesTranscriptColorTokens(
        background = Color(0xFFF6F7F9),
        activeBackground = Color(0xFFEFF2F5),
        accent = Color(0xFF006F80),
        borderRail = Color(0xFF8A939F),
        error = Color(0xFFA33232),
        muted = Color(0xFF586575),
        prompt = Color(0xFF005F6E),
        statusBackground = Color(0xFFE9EDF1),
        statusForeground = Color(0xFF425466),
        text = Color(0xFF1B1F24),
        tool = Color(0xFF425466),
        warning = Color(0xFF83560C),
        success = Color(0xFF2D6E4B),
        critical = Color(0xFF8E2424),
    )

    val metrics = HermesTranscriptMetrics(
        horizontalContentInset = 16.dp,
        processGutter = 24.dp,
        turnGap = 18.dp,
        sectionGap = 6.dp,
        containmentRadius = 8.dp,
        minimumTouchTarget = 48.dp,
        railSlotWidth = 24.dp,
        railMinimumHeight = 20.dp,
        railStrokeWidth = 1.5.dp,
        railNodeDiameter = 6.dp,
    )

    val typography = HermesTranscriptTypography(
        body = transcriptTextStyle(fontSize = 16.sp, lineHeight = 24.sp),
        process = transcriptTextStyle(
            fontSize = 14.sp,
            lineHeight = 20.sp,
            fontFamily = FontFamily.Monospace,
        ),
        meta = transcriptTextStyle(
            fontSize = 13.sp,
            lineHeight = 19.sp,
            fontFamily = FontFamily.Monospace,
        ),
        code = transcriptTextStyle(
            fontSize = 13.5.sp,
            lineHeight = 20.sp,
            fontFamily = FontFamily.Monospace,
        ),
    )
}

private fun transcriptTextStyle(
    fontSize: TextUnit,
    lineHeight: TextUnit,
    fontFamily: FontFamily? = null,
): TextStyle = TextStyle(
    fontSize = fontSize,
    lineHeight = lineHeight,
    fontFamily = fontFamily,
)

internal val LocalHermesTranscriptColors = staticCompositionLocalOf {
    HermesTranscriptDesignSystem.darkColors
}

internal val LocalHermesTranscriptMetrics = staticCompositionLocalOf {
    HermesTranscriptDesignSystem.metrics
}

internal val LocalHermesTranscriptTypography = staticCompositionLocalOf {
    HermesTranscriptDesignSystem.typography
}

internal object HermesTranscriptThemeTokens {
    val colors: HermesTranscriptColorTokens
        @Composable
        @ReadOnlyComposable
        get() = LocalHermesTranscriptColors.current

    val metrics: HermesTranscriptMetrics
        @Composable
        @ReadOnlyComposable
        get() = LocalHermesTranscriptMetrics.current

    val typography: HermesTranscriptTypography
        @Composable
        @ReadOnlyComposable
        get() = LocalHermesTranscriptTypography.current
}

/** Installs the complete Hermes console vocabulary, including its compact geometry. */
@Composable
internal fun ProvideHermesTranscriptDesignSystem(
    darkTheme: Boolean? = null,
    content: @Composable () -> Unit,
) {
    val resolvedDarkTheme = darkTheme ?: (MaterialTheme.colorScheme.background.luminance() < 0.5f)
    val colors = if (resolvedDarkTheme) {
        HermesTranscriptDesignSystem.darkColors
    } else {
        HermesTranscriptDesignSystem.lightColors
    }
    val baseScheme = MaterialTheme.colorScheme
    val materialScheme = baseScheme.copy(
        background = colors.background,
        onBackground = colors.text,
        surface = colors.background,
        onSurface = colors.text,
        surfaceVariant = colors.statusBackground,
        onSurfaceVariant = colors.muted,
        primary = colors.accent,
        outline = colors.borderRail,
        outlineVariant = colors.borderRail,
        error = colors.error,
    )

    CompositionLocalProvider(
        LocalHermesTranscriptColors provides colors,
        LocalHermesTranscriptMetrics provides HermesTranscriptDesignSystem.metrics,
        LocalHermesTranscriptTypography provides HermesTranscriptDesignSystem.typography,
    ) {
        MaterialTheme(
            colorScheme = materialScheme,
            typography = MaterialTheme.typography,
            shapes = HermesTranscriptDesignSystem.shapes,
            content = content,
        )
    }
}
