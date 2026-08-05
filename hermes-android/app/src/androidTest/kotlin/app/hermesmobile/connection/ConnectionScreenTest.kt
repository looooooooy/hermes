package app.hermesmobile.connection

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.text.AnnotatedString
import org.junit.Rule
import org.junit.Test

class ConnectionScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun endpointIsRequiredBeforeConnectionCheck() {
        var state by mutableStateOf(ConnectionUiState())
        composeRule.setContent {
            ConnectionScreen(
                state = state,
                onEndpointChanged = { state = state.copy(endpointInput = it) },
                onConnect = {},
                onSignIn = {},
            )
        }

        composeRule.onNodeWithText("Hermes Mobile").assertIsDisplayed()
        composeRule.onNodeWithText("Check connection").assertIsNotEnabled()

        composeRule.onNodeWithTag("endpoint-input").performTextInput("hermes.example.com")

        composeRule.onNodeWithText("Check connection").assertIsEnabled()
    }

    @Test
    fun protectedReachableGatewayShowsNativeSignInAction() {
        var signInClicked = false
        composeRule.setContent {
            ConnectionScreen(
                state = ConnectionUiState(
                    endpointInput = "hermes.example.com",
                    phase = ConnectionPhase.AUTHENTICATION_REQUIRED,
                    canonicalEndpoint = "https://hermes.example.com/",
                    hermesVersion = "0.14.0",
                    gatewayRunning = true,
                    supportsNativePkce = true,
                ),
                onEndpointChanged = {},
                onConnect = {},
                onSignIn = { signInClicked = true },
            )
        }

        composeRule.onNodeWithText("Hermes 0.14.0 is reachable").assertIsDisplayed()
        composeRule.onNodeWithText("Gateway service").assertIsDisplayed()
        composeRule.onNodeWithText("Running").assertIsDisplayed()
        composeRule.onNodeWithText("Agent runtime").assertDoesNotExist()
        composeRule.onNodeWithText("Not running").assertDoesNotExist()
        composeRule.onNodeWithText("Sign in securely").assertIsDisplayed()
        composeRule.onNodeWithText("System browser + PKCE").assertIsDisplayed()
        composeRule.onNodeWithText("Continue in browser").performClick()
        composeRule.runOnIdle { check(signInClicked) }
    }

    @Test
    fun protectedGatewayWithBasicProviderShowsPasswordSignInForm() {
        var submittedUsername: String? = null
        var submittedPassword: String? = null
        composeRule.setContent {
            ConnectionScreen(
                state = ConnectionUiState(
                    endpointInput = "https://api.seaotter.wiki/hermes/",
                    phase = ConnectionPhase.AUTHENTICATION_REQUIRED,
                    canonicalEndpoint = "https://api.seaotter.wiki/hermes/",
                    hermesVersion = "0.19.0",
                    gatewayRunning = true,
                    supportsPassword = true,
                ),
                onEndpointChanged = {},
                onConnect = {},
                onSignIn = {},
                onPasswordSignIn = { username, password ->
                    submittedUsername = username
                    submittedPassword = password
                },
            )
        }

        composeRule.onNodeWithTag("password-sign-in").assertIsNotEnabled()
        composeRule.onNodeWithTag("username-input")
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.EditableText,
                    AnnotatedString("hermes-mobile"),
                ),
            )
            .performTextReplacement("mobile-user")
        composeRule.onNodeWithTag("password-input").performTextInput("temporary-password")
        composeRule.onNodeWithTag("password-input").assert(
            SemanticsMatcher.keyIsDefined(SemanticsProperties.Password),
        )
        composeRule.onNodeWithTag("password-sign-in").assertIsEnabled().performClick()
        composeRule.onNodeWithTag("password-input").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.EditableText,
                AnnotatedString(""),
            ),
        )

        composeRule.runOnIdle {
            check(submittedUsername == "mobile-user")
            check(submittedPassword == "temporary-password")
        }
    }

    @Test
    fun authenticationProgressIsVisibleWhileBrowserFlowRuns() {
        composeRule.setContent {
            ConnectionScreen(
                state = ConnectionUiState(
                    endpointInput = "hermes.example.com",
                    phase = ConnectionPhase.AUTHENTICATING,
                    canonicalEndpoint = "https://hermes.example.com/",
                    supportsNativePkce = true,
                ),
                onEndpointChanged = {},
                onConnect = {},
                onSignIn = {},
            )
        }

        composeRule.onNodeWithText("Waiting for browser sign-in…").assertIsDisplayed()
    }
}
