package app.hermesmobile.auth

import android.content.Context
import androidx.test.platform.app.InstrumentationRegistry
import app.hermesmobile.protocol.auth.NativeTokens
import java.security.KeyStore
import java.util.UUID
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

class EncryptedTokenVaultTest {
    private lateinit var context: Context
    private lateinit var preferenceName: String
    private lateinit var keyAlias: String
    private lateinit var vault: EncryptedTokenVault

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        val suffix = UUID.randomUUID().toString()
        preferenceName = "token-vault-test-$suffix"
        keyAlias = "token-vault-test-$suffix"
        vault = EncryptedTokenVault(context, preferenceName, keyAlias)
    }

    @After
    fun tearDown() {
        context.getSharedPreferences(preferenceName, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (keyStore.containsAlias(keyAlias)) keyStore.deleteEntry(keyAlias)
    }

    @Test
    fun tokensRoundTripAndCanBeCleared() {
        val endpoint = "https://hermes.example.com/"
        val tokens = NativeTokens(
            accessToken = "access-secret",
            refreshToken = "refresh-secret",
            tokenType = "Bearer",
            expiresAtEpochSeconds = 1_900_000_000,
            provider = "nous",
            userId = "user-1",
        )

        vault.save(endpoint, tokens)
        val restored = vault.load(endpoint)

        assertEquals("access-secret", restored?.accessToken)
        assertEquals("refresh-secret", restored?.refreshToken)
        assertEquals("nous", restored?.provider)

        vault.clear(endpoint)
        assertNull(vault.load(endpoint))
    }

    @Test
    fun preferencesContainCiphertextRatherThanTokenPlaintext() {
        val endpoint = "https://hermes.example.com/"
        vault.save(
            endpoint,
            NativeTokens(
                accessToken = "access-secret",
                refreshToken = "refresh-secret",
                tokenType = "Bearer",
                expiresAtEpochSeconds = 1_900_000_000,
                provider = "nous",
                userId = "user-1",
            ),
        )

        val storedValues = context
            .getSharedPreferences(preferenceName, Context.MODE_PRIVATE)
            .all
            .values
            .joinToString()

        assertFalse(storedValues.contains("access-secret"))
        assertFalse(storedValues.contains("refresh-secret"))
        assertFalse(storedValues.contains(endpoint))
    }

    @Test
    fun ciphertextIsBoundToItsEndpointAsAdditionalAuthenticatedData() {
        val firstEndpoint = "https://first.example.com/"
        val secondEndpoint = "https://second.example.com/"
        vault.save(
            firstEndpoint,
            NativeTokens(
                accessToken = "access-secret",
                refreshToken = "refresh-secret",
                tokenType = "Bearer",
                expiresAtEpochSeconds = 1_900_000_000,
                provider = "nous",
                userId = "user-1",
            ),
        )

        assertNull(vault.load(secondEndpoint))
        assertEquals("access-secret", vault.load(firstEndpoint)?.accessToken)
    }
}
