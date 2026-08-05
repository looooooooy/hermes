package app.hermesmobile.auth

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthClient
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.auth.PkceGenerator
import java.io.BufferedInputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

fun interface BrowserLauncher {
    fun launch(url: HttpUrl)
}

fun interface NativeSignIn {
    suspend fun signIn(endpoint: GatewayEndpoint): NativeSignInResult
}

sealed interface NativeSignInResult {
    data class Authenticated(val tokens: NativeTokens) : NativeSignInResult

    data class Failed(
        val summary: String = "Secure sign-in did not complete.",
    ) : NativeSignInResult
}

class NativeOAuthCoordinator(
    private val authClient: NativeAuthClient,
    private val browserLauncher: BrowserLauncher,
    private val pkceGenerator: PkceGenerator = PkceGenerator(),
    private val callbackTimeoutMillis: Int = DEFAULT_CALLBACK_TIMEOUT_MILLIS,
) : NativeSignIn {
    override suspend fun signIn(endpoint: GatewayEndpoint): NativeSignInResult {
        val credentials = pkceGenerator.generate()
        val callback = try {
            awaitAuthorizationCallback(endpoint, credentials)
        } catch (_: SocketTimeoutException) {
            return NativeSignInResult.Failed("Secure sign-in timed out. Try again.")
        } catch (_: Exception) {
            return NativeSignInResult.Failed("Secure sign-in could not be started.")
        }

        if (!constantTimeEquals(callback.state, credentials.state)) {
            return NativeSignInResult.Failed("Secure sign-in response could not be verified.")
        }
        if (callback.error != null) {
            return NativeSignInResult.Failed("Secure sign-in was denied (${callback.error}).")
        }
        val code = callback.code
            ?: return NativeSignInResult.Failed("Secure sign-in returned no authorization code.")

        return when (val result = authClient.exchangeCode(endpoint, code, credentials.verifier)) {
            is NativeAuthResult.Success -> NativeSignInResult.Authenticated(result.value)
            is NativeAuthResult.HttpFailure -> NativeSignInResult.Failed(
                "Hermes rejected the secure sign-in (${result.statusCode}).",
            )
            is NativeAuthResult.InvalidResponse -> NativeSignInResult.Failed(result.summary)
            is NativeAuthResult.NetworkFailure -> NativeSignInResult.Failed(result.summary)
        }
    }

    private suspend fun awaitAuthorizationCallback(
        endpoint: GatewayEndpoint,
        credentials: app.hermesmobile.protocol.auth.PkceCredentials,
    ): AuthorizationCallback = withContext(Dispatchers.IO) {
        ServerSocket(
            0,
            CALLBACK_BACKLOG,
            InetAddress.getByName(LOOPBACK_HOST),
        ).use { server ->
            server.soTimeout = callbackTimeoutMillis
            val redirectUri = "http://$LOOPBACK_HOST:${server.localPort}$CALLBACK_PATH"
            val authorizationUrl = authClient.authorizationUrl(
                endpoint = endpoint,
                redirectUri = redirectUri,
                credentials = credentials,
            )
            browserLauncher.launch(authorizationUrl)
            server.accept().use { socket ->
                socket.soTimeout = REQUEST_TIMEOUT_MILLIS
                val callback = socket.readCallback(server.localPort)
                socket.writeBrowserResponse(callback.error == null)
                callback
            }
        }
    }

    private fun Socket.readCallback(loopbackPort: Int): AuthorizationCallback {
        val input = BufferedInputStream(getInputStream())
        val requestLine = input.readAsciiLine(MAX_REQUEST_LINE_BYTES)
            ?: throw IllegalArgumentException("Missing callback request line.")
        for (index in 0 until MAX_HEADER_LINES) {
            val line = input.readAsciiLine(MAX_HEADER_LINE_BYTES) ?: break
            if (line.isEmpty()) break
        }
        val parts = requestLine.split(' ', limit = 3)
        require(parts.size == 3 && parts[0] == "GET")
        val target = parts[1]
        require(target.startsWith("/") && !target.startsWith("//"))
        val url = "http://$LOOPBACK_HOST:$loopbackPort$target".toHttpUrl()
        require(url.encodedPath == CALLBACK_PATH)
        return AuthorizationCallback(
            code = url.queryParameter("code"),
            state = url.queryParameter("state").orEmpty(),
            error = url.queryParameter("error"),
        )
    }

    private fun Socket.writeBrowserResponse(success: Boolean) {
        val title = if (success) "Sign-in received" else "Sign-in could not be verified"
        val body = (
            "<!doctype html><meta charset=\"utf-8\"><title>$title</title>" +
                "<p>$title. You can return to Hermes Mobile.</p>"
            ).toByteArray(StandardCharsets.UTF_8)
        getOutputStream().buffered().use { output ->
            output.write("HTTP/1.1 200 OK\r\n".toByteArray(StandardCharsets.US_ASCII))
            output.write("Content-Type: text/html; charset=utf-8\r\n".toByteArray(StandardCharsets.US_ASCII))
            output.write("Cache-Control: no-store\r\n".toByteArray(StandardCharsets.US_ASCII))
            output.write("Connection: close\r\n".toByteArray(StandardCharsets.US_ASCII))
            output.write("Content-Length: ${body.size}\r\n\r\n".toByteArray(StandardCharsets.US_ASCII))
            output.write(body)
        }
    }

    private fun BufferedInputStream.readAsciiLine(maxBytes: Int): String? {
        val bytes = ArrayList<Byte>()
        while (bytes.size <= maxBytes) {
            val next = read()
            if (next == -1) return if (bytes.isEmpty()) null else bytes.toByteArray().toString(StandardCharsets.US_ASCII)
            if (next == '\n'.code) {
                if (bytes.lastOrNull() == '\r'.code.toByte()) bytes.removeAt(bytes.lastIndex)
                return bytes.toByteArray().toString(StandardCharsets.US_ASCII)
            }
            bytes.add(next.toByte())
        }
        throw IllegalArgumentException("Loopback callback line was too long.")
    }

    private fun constantTimeEquals(first: String, second: String): Boolean =
        MessageDigest.isEqual(
            first.toByteArray(StandardCharsets.UTF_8),
            second.toByteArray(StandardCharsets.UTF_8),
        )

    private data class AuthorizationCallback(
        val code: String?,
        val state: String,
        val error: String?,
    )

    private companion object {
        const val LOOPBACK_HOST = "127.0.0.1"
        const val CALLBACK_PATH = "/oauth/callback"
        const val CALLBACK_BACKLOG = 1
        const val DEFAULT_CALLBACK_TIMEOUT_MILLIS = 10 * 60 * 1_000
        const val REQUEST_TIMEOUT_MILLIS = 5_000
        const val MAX_REQUEST_LINE_BYTES = 4_096
        const val MAX_HEADER_LINE_BYTES = 8_192
        const val MAX_HEADER_LINES = 64
    }
}
