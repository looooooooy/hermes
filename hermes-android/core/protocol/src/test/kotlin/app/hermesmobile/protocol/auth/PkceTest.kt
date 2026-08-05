package app.hermesmobile.protocol.auth

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class PkceTest {
    @Test
    fun `RFC 7636 verifier produces the specified SHA-256 challenge`() {
        val verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

        val challenge = Pkce.challengeFor(verifier)

        assertEquals("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", challenge)
    }

    @Test
    fun `generated credentials are URL safe and contain no padding`() {
        var seed = 0
        val generator = PkceGenerator { size ->
            ByteArray(size) { (seed++ and 0xff).toByte() }
        }

        val credentials = generator.generate()

        assertTrue(credentials.verifier.length in 43..128)
        assertTrue(credentials.state.length >= 43)
        assertTrue(credentials.verifier.matches(URL_SAFE_VALUE))
        assertTrue(credentials.challenge.matches(URL_SAFE_VALUE))
        assertTrue(credentials.state.matches(URL_SAFE_VALUE))
        assertFalse(credentials.verifier.contains('='))
        assertEquals(Pkce.challengeFor(credentials.verifier), credentials.challenge)
    }

    @Test
    fun `verifier outside RFC length bounds is rejected`() {
        listOf("a".repeat(42), "a".repeat(129)).forEach { verifier ->
            val result = runCatching { Pkce.challengeFor(verifier) }
            assertTrue(result.isFailure)
        }
    }

    private companion object {
        val URL_SAFE_VALUE = Regex("^[A-Za-z0-9_-]+$")
    }
}
