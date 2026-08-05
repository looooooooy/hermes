package app.hermesmobile.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

private val LightColors = lightColorScheme(
    primary = Color(0xFF006F80),
    onPrimary = Color.White,
    background = Color(0xFFF6F7F9),
    surface = Color(0xFFF6F7F9),
    surfaceVariant = Color(0xFFE9EDF1),
    onSurface = Color(0xFF1B1F24),
    onSurfaceVariant = Color(0xFF586575),
    outline = Color(0xFF8A939F),
    error = Color(0xFFA33232),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF65D2E2),
    onPrimary = Color(0xFF0A0C0F),
    background = Color(0xFF0A0C0F),
    surface = Color(0xFF0A0C0F),
    surfaceVariant = Color(0xFF13161B),
    onSurface = Color(0xFFF0F1F3),
    onSurfaceVariant = Color(0xFF8B929D),
    outline = Color(0xFF272C33),
    error = Color(0xFFEB9898),
)

private val HermesShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(10.dp),
    large = RoundedCornerShape(12.dp),
    extraLarge = RoundedCornerShape(14.dp),
)

@Composable
fun HermesMobileTheme(
    darkTheme: Boolean = true,
    content: @Composable () -> Unit,
) {
    val colors = if (darkTheme) DarkColors else LightColors

    MaterialTheme(
        colorScheme = colors,
        shapes = HermesShapes,
        content = content,
    )
}
