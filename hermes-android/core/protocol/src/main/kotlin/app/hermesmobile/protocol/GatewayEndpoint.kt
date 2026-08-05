package app.hermesmobile.protocol

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Canonical root URL for one Hermes dashboard / gateway ingress. */
class GatewayEndpoint private constructor(
    val baseUrl: HttpUrl,
) {
    val statusUrl: HttpUrl = requireNotNull(baseUrl.resolve("api/status"))
    val capabilitiesUrl: HttpUrl = requireNotNull(baseUrl.resolve("v1/capabilities"))

    val webSocketUrl: String = requireNotNull(baseUrl.resolve("api/ws"))
        .toString()
        .replaceFirst(
            oldValue = if (baseUrl.isHttps) "https://" else "http://",
            newValue = if (baseUrl.isHttps) "wss://" else "ws://",
        )

    companion object {
        private val insecureDevelopmentHosts = setOf(
            "localhost",
            "127.0.0.1",
            "::1",
            "10.0.2.2",
        )

        fun parse(rawValue: String): Result<GatewayEndpoint> = runCatching {
            val trimmed = rawValue.trim()
            require(trimmed.isNotEmpty()) { "Enter a Hermes server address." }

            val withScheme = if ("://" in trimmed) trimmed else "https://$trimmed"
            val parsed = requireNotNull(withScheme.toHttpUrlOrNull()) {
                "Enter a valid HTTP or HTTPS address."
            }

            require(parsed.encodedUsername.isEmpty() && parsed.encodedPassword.isEmpty()) {
                "Credentials must not be embedded in the server address."
            }
            require(parsed.query == null && parsed.fragment == null) {
                "Query parameters and fragments are not allowed in the server address."
            }
            require(parsed.isHttps || parsed.host in insecureDevelopmentHosts) {
                "HTTPS is required for remote Hermes servers."
            }

            val pathWithoutTrailingSlash = parsed.encodedPath.trimEnd('/')
            val normalizedPath = if (pathWithoutTrailingSlash.isEmpty()) {
                "/"
            } else {
                "$pathWithoutTrailingSlash/"
            }
            val normalized = parsed.newBuilder()
                .encodedPath(normalizedPath)
                .build()

            GatewayEndpoint(normalized)
        }
    }
}
