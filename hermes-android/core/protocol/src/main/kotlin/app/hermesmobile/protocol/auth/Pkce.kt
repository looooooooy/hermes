package app.hermesmobile.protocol.auth

import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64


data class PkceCredentials(
    val verifier: String,
    val challenge: String,
    val state: String,
)

object Pkce {
    private val verifierPattern = Regex("^[A-Za-z0-9._~-]{43,128}$")

    fun challengeFor(verifier: String): String {
        require(verifierPattern.matches(verifier)) {
            "PKCE verifier must be 43-128 RFC 7636 unreserved characters."
        }
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(verifier.toByteArray(Charsets.US_ASCII))
        return digest.base64UrlWithoutPadding()
    }
}

class PkceGenerator(
    private val randomBytes: (Int) -> ByteArray = { size ->
        ByteArray(size).also(SecureRandom()::nextBytes)
    },
) {
    fun generate(): PkceCredentials {
        val verifier = randomBytes(RANDOM_BYTE_COUNT)
            .also { require(it.size == RANDOM_BYTE_COUNT) }
            .base64UrlWithoutPadding()
        val state = randomBytes(RANDOM_BYTE_COUNT)
            .also { require(it.size == RANDOM_BYTE_COUNT) }
            .base64UrlWithoutPadding()
        return PkceCredentials(
            verifier = verifier,
            challenge = Pkce.challengeFor(verifier),
            state = state,
        )
    }

    private companion object {
        const val RANDOM_BYTE_COUNT = 32
    }
}

private fun ByteArray.base64UrlWithoutPadding(): String =
    Base64.getUrlEncoder().withoutPadding().encodeToString(this)
