package app.hermesmobile.auth

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import app.hermesmobile.protocol.auth.NativeTokens
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

interface TokenVault {
    fun save(endpointId: String, tokens: NativeTokens)

    fun load(endpointId: String): NativeTokens?

    fun clear(endpointId: String)
}

class EncryptedTokenVault(
    context: Context,
    preferenceName: String = DEFAULT_PREFERENCE_NAME,
    private val keyAlias: String = DEFAULT_KEY_ALIAS,
) : TokenVault {
    private val preferences = context.applicationContext.getSharedPreferences(
        preferenceName,
        Context.MODE_PRIVATE,
    )

    @Synchronized
    override fun save(endpointId: String, tokens: NativeTokens) {
        require(endpointId.isNotBlank()) { "Endpoint identity is required." }
        val plaintext = encode(tokens)
        val cipher = Cipher.getInstance(TRANSFORMATION).apply {
            init(Cipher.ENCRYPT_MODE, getOrCreateKey())
            updateAAD(endpointId.toByteArray(Charsets.UTF_8))
        }
        val ciphertext = cipher.doFinal(plaintext)
        val envelope = listOf(
            ENVELOPE_VERSION,
            cipher.iv.base64Url(),
            ciphertext.base64Url(),
        ).joinToString(ENVELOPE_SEPARATOR)
        check(preferences.edit().putString(preferenceKey(endpointId), envelope).commit()) {
            "Encrypted token storage failed."
        }
    }

    @Synchronized
    override fun load(endpointId: String): NativeTokens? {
        require(endpointId.isNotBlank()) { "Endpoint identity is required." }
        val recordKey = preferenceKey(endpointId)
        val envelope = preferences.getString(recordKey, null) ?: return null
        return runCatching {
            val parts = envelope.split(ENVELOPE_SEPARATOR)
            require(parts.size == 3 && parts[0] == ENVELOPE_VERSION)
            val iv = parts[1].base64UrlBytes()
            val ciphertext = parts[2].base64UrlBytes()
            require(iv.size in 12..16 && ciphertext.isNotEmpty())
            val cipher = Cipher.getInstance(TRANSFORMATION).apply {
                init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
                updateAAD(endpointId.toByteArray(Charsets.UTF_8))
            }
            decode(cipher.doFinal(ciphertext))
        }.getOrElse {
            preferences.edit().remove(recordKey).commit()
            null
        }
    }

    @Synchronized
    override fun clear(endpointId: String) {
        require(endpointId.isNotBlank()) { "Endpoint identity is required." }
        check(preferences.edit().remove(preferenceKey(endpointId)).commit()) {
            "Encrypted token removal failed."
        }
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEY_STORE).apply { load(null) }
        (keyStore.getKey(keyAlias, null) as? SecretKey)?.let { return it }

        val specBuilder = KeyGenParameterSpec.Builder(
            keyAlias,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(AES_KEY_BITS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            specBuilder.setUnlockedDeviceRequired(true)
        }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEY_STORE)
            .apply { init(specBuilder.build()) }
            .generateKey()
    }

    private fun preferenceKey(endpointId: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(endpointId.toByteArray(Charsets.UTF_8))
        return "tokens-${digest.base64Url()}"
    }

    private fun encode(tokens: NativeTokens): ByteArray =
        ByteArrayOutputStream().use { bytes ->
            DataOutputStream(bytes).use { output ->
                output.writeByte(PAYLOAD_VERSION)
                output.writeUTF(tokens.accessToken)
                output.writeUTF(tokens.refreshToken)
                output.writeUTF(tokens.tokenType)
                output.writeLong(tokens.expiresAtEpochSeconds)
                output.writeUTF(tokens.provider)
                output.writeUTF(tokens.userId)
            }
            bytes.toByteArray()
        }

    private fun decode(plaintext: ByteArray): NativeTokens =
        DataInputStream(ByteArrayInputStream(plaintext)).use { input ->
            require(input.readUnsignedByte() == PAYLOAD_VERSION)
            val tokens = NativeTokens(
                accessToken = input.readUTF(),
                refreshToken = input.readUTF(),
                tokenType = input.readUTF(),
                expiresAtEpochSeconds = input.readLong(),
                provider = input.readUTF(),
                userId = input.readUTF(),
            )
            require(tokens.accessToken.isNotBlank() && tokens.refreshToken.isNotBlank())
            require(input.read() == -1)
            tokens
        }

    private fun ByteArray.base64Url(): String = Base64.encodeToString(
        this,
        Base64.NO_WRAP or Base64.NO_PADDING or Base64.URL_SAFE,
    )

    private fun String.base64UrlBytes(): ByteArray = Base64.decode(
        this,
        Base64.NO_WRAP or Base64.NO_PADDING or Base64.URL_SAFE,
    )

    private companion object {
        const val DEFAULT_PREFERENCE_NAME = "hermes-auth-vault"
        const val DEFAULT_KEY_ALIAS = "hermes-mobile-native-auth-v1"
        const val ANDROID_KEY_STORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val AES_KEY_BITS = 256
        const val GCM_TAG_BITS = 128
        const val PAYLOAD_VERSION = 1
        const val ENVELOPE_VERSION = "v1"
        const val ENVELOPE_SEPARATOR = ":"
    }
}
