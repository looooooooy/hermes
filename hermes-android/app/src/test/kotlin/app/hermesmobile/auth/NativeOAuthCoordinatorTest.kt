package app.hermesmobile.auth

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthClient
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.test.runTest
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNotNull

class NativeOAuthCoordinatorTest {
    private lateinit var gateway: MockWebServer
    private lateinit var endpoint: GatewayEndpoint
    private lateinit var callbackExecutor: java.util.concurrent.ExecutorService

    @Before
    fun setUp() {
        gateway = MockWebServer()
        gateway.start()
        endpoint = GatewayEndpoint.parse(gateway.url("/ingress/").toString()).getOrThrow()
        callbackExecutor = Executors.newSingleThreadExecutor()
    }

    @After
    fun tearDown() {
        callbackExecutor.shutdownNow()
        callbackExecutor.awaitTermination(2, TimeUnit.SECONDS)
        gateway.shutdown()
    }

    @Test
    fun `system browser callback is state checked and exchanged`() = runTest {
        gateway.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"access_token":"access","refresh_token":"refresh","token_type":"Bearer","expires_at":1900000000,"provider":"nous","user_id":"user-1"}""",
                ),
        )
        var authorizationUrl: HttpUrl? = null
        val coordinator = NativeOAuthCoordinator(
            authClient = NativeAuthClient(OkHttpClient()),
            browserLauncher = BrowserLauncher { url ->
                authorizationUrl = url
                callbackExecutor.submit {
                    val redirect = url.queryParameter("redirect_uri")!!.toHttpUrl()
                    val callback = redirect.newBuilder()
                        .addQueryParameter("code", "one-time-code")
                        .addQueryParameter("state", url.queryParameter("state")!!)
                        .build()
                    (URL(callback.toString()).openConnection() as HttpURLConnection).run {
                        connectTimeout = 2_000
                        readTimeout = 2_000
                        inputStream.use { it.readBytes() }
                        disconnect()
                    }
                }
            },
            callbackTimeoutMillis = 5_000,
        )

        val result = coordinator.signIn(endpoint)

        val authenticated = assertIs<NativeSignInResult.Authenticated>(result)
        assertEquals("access", authenticated.tokens.accessToken)
        assertEquals("/ingress/auth/native/authorize", authorizationUrl?.encodedPath)
        val tokenRequest = gateway.takeRequest(2, TimeUnit.SECONDS)
        assertNotNull(tokenRequest)
        assertEquals("/ingress/auth/native/token", tokenRequest.path)
    }

    @Test
    fun `mismatched state is rejected without token exchange`() = runTest {
        val coordinator = NativeOAuthCoordinator(
            authClient = NativeAuthClient(OkHttpClient()),
            browserLauncher = BrowserLauncher { url ->
                callbackExecutor.submit {
                    val redirect = url.queryParameter("redirect_uri")!!.toHttpUrl()
                    val callback = redirect.newBuilder()
                        .addQueryParameter("code", "intercepted-code")
                        .addQueryParameter("state", "attacker-state")
                        .build()
                    runCatching { URL(callback.toString()).readText() }
                }
            },
            callbackTimeoutMillis = 5_000,
        )

        val result = coordinator.signIn(endpoint)

        assertIs<NativeSignInResult.Failed>(result)
        assertEquals(0, gateway.requestCount)
    }
}
