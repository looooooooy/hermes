package app.hermesmobile.auth

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthClient
import app.hermesmobile.protocol.auth.NativeAuthResult

fun interface PasswordSignIn {
    suspend fun signIn(
        endpoint: GatewayEndpoint,
        username: String,
        password: String,
    ): NativeSignInResult
}

class GatewayPasswordSignIn(
    private val authClient: NativeAuthClient,
) : PasswordSignIn {
    override suspend fun signIn(
        endpoint: GatewayEndpoint,
        username: String,
        password: String,
    ): NativeSignInResult = when (
        val result = authClient.passwordLogin(
            endpoint = endpoint,
            username = username,
            password = password,
        )
    ) {
        is NativeAuthResult.Success -> NativeSignInResult.Authenticated(result.value)
        is NativeAuthResult.HttpFailure -> NativeSignInResult.Failed(
            if (result.statusCode == 401) {
                "Hermes rejected the username or password."
            } else {
                "Hermes rejected password sign-in (${result.statusCode})."
            },
        )
        is NativeAuthResult.InvalidResponse -> NativeSignInResult.Failed(result.summary)
        is NativeAuthResult.NetworkFailure -> NativeSignInResult.Failed(result.summary)
    }
}
