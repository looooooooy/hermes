package app.hermesmobile.pairing

import app.hermesmobile.auth.TokenVault
import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.NativeAuthResult
import app.hermesmobile.protocol.auth.NativeTokens
import app.hermesmobile.protocol.pairing.CancelPairingRequest
import app.hermesmobile.protocol.pairing.ClaimPairingRequest
import app.hermesmobile.protocol.pairing.ConfirmPairingRequest
import app.hermesmobile.protocol.pairing.DeviceRevokeReason
import app.hermesmobile.protocol.pairing.PairingActivationState
import app.hermesmobile.protocol.pairing.PairingCancelReason
import app.hermesmobile.protocol.pairing.PairingCode
import app.hermesmobile.protocol.pairing.PairingConnectorReview
import app.hermesmobile.protocol.pairing.PairingDeviceId
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingIdempotencyKey
import app.hermesmobile.protocol.pairing.PairingOwnerApi
import app.hermesmobile.protocol.pairing.PairingOwnerBinding
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingResult
import app.hermesmobile.protocol.pairing.PairingScope
import app.hermesmobile.protocol.pairing.PairingSessionId
import app.hermesmobile.protocol.pairing.PairingSessionState
import app.hermesmobile.protocol.pairing.RevokePairingDeviceRequest
import app.hermesmobile.protocol.pairing.RevokedPairingDevice
import app.hermesmobile.sessions.TokenRefresh
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull

class AuthenticatedPairingOwnerActionsTest {
    private val endpoint = GatewayEndpoint.parse("https://gateway.example/hermes/").getOrThrow()

    @Test
    fun `missing owner token fails before pairing API call`() = runTest {
        val vault = FakeTokenVault()
        val api = FakePairingOwnerApi()
        val actions = actions(vault = vault, api = api)

        val result = actions.claim(claimRequest())

        val failed = assertIs<PairingActionResult.Failed>(result)
        assertEquals(PairingFailure.AuthenticationRequired, failed.failure)
        assertEquals(0, api.claimCalls)
    }

    @Test
    fun `first authentication block freezes claim identity before any API call`() = runTest {
        val vault = FakeTokenVault()
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.NetworkFailure(),
                    PairingResult.HttpFailure(410, PairingErrorCode.PAIRING_EXPIRED),
                ),
            ),
        )
        val actions = actions(vault = vault, api = api)
        val frozenRequest = claimRequest()

        assertEquals(
            PairingFailure.AuthenticationRequired,
            assertIs<PairingActionResult.Failed>(
                actions.claim(frozenRequest),
            ).failure,
        )
        assertEquals(0, api.claimCalls)

        vault.tokens = tokens("authenticated", "refresh", expiresAt = 1_000)
        assertEquals(
            PairingFailure.InvalidResponse,
            assertIs<PairingActionResult.Failed>(
                actions.claim(claimRequest(deviceDisplayName = "Different Mac")),
            ).failure,
        )
        assertEquals(0, api.claimCalls)

        actions.claim(frozenRequest)
        actions.claim(frozenRequest)

        assertEquals(2, api.claimCalls)
        assertEquals(1, api.claimIdempotencyKeys.distinct().size)
    }

    @Test
    fun `first refresh outage freezes claim identity before any API call`() = runTest {
        val vault = FakeTokenVault(tokens("expired", "refresh", expiresAt = 100))
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.HttpFailure(410, PairingErrorCode.PAIRING_EXPIRED),
                ),
            ),
        )
        val actions = actions(
            vault = vault,
            api = api,
            refresh = TokenRefresh { _, _ -> NativeAuthResult.NetworkFailure() },
        )
        val frozenRequest = claimRequest()

        assertEquals(
            PairingFailure.Unavailable,
            assertIs<PairingActionResult.Failed>(
                actions.claim(frozenRequest),
            ).failure,
        )
        assertEquals(0, api.claimCalls)

        vault.tokens = tokens("authenticated", "refresh-2", expiresAt = 1_000)
        assertEquals(
            PairingFailure.InvalidResponse,
            assertIs<PairingActionResult.Failed>(
                actions.claim(claimRequest(deviceDisplayName = "Different Mac")),
            ).failure,
        )
        assertEquals(0, api.claimCalls)

        actions.claim(frozenRequest)

        assertEquals(1, api.claimCalls)
    }

    @Test
    fun `HTTP unauthorized refreshes once and retries with same idempotency key`() = runTest {
        val old = tokens("old-access", "refresh-1", expiresAt = 1_000)
        val rotated = tokens("new-access", "refresh-2", expiresAt = 2_000)
        val vault = FakeTokenVault(old)
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.HttpFailure(401, PairingErrorCode.UNAUTHORIZED),
                    PairingResult.HttpFailure(410, PairingErrorCode.PAIRING_EXPIRED),
                ),
            ),
        )
        var refreshCount = 0
        val actions = actions(
            vault = vault,
            api = api,
            refresh = TokenRefresh { _, current ->
                refreshCount += 1
                assertEquals(old, current)
                NativeAuthResult.Success(rotated)
            },
        )

        val result = actions.claim(claimRequest())

        val failed = assertIs<PairingActionResult.Failed>(result)
        assertEquals(
            PairingFailure.Contract(PairingErrorCode.PAIRING_EXPIRED),
            failed.failure,
        )
        assertEquals(1, refreshCount)
        assertEquals(listOf("old-access", "new-access"), api.accessTokens)
        assertEquals(2, api.claimIdempotencyKeys.size)
        assertEquals(
            api.claimIdempotencyKeys.first(),
            api.claimIdempotencyKeys.last(),
        )
        assertEquals(rotated, vault.tokens)
    }

    @Test
    fun `second unauthorized clears rejected rotated credentials`() = runTest {
        val vault = FakeTokenVault(tokens("old-access", "refresh-1", expiresAt = 1_000))
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.HttpFailure(401, PairingErrorCode.UNAUTHORIZED),
                    PairingResult.HttpFailure(401, PairingErrorCode.UNAUTHORIZED),
                ),
            ),
        )
        val actions = actions(
            vault = vault,
            api = api,
            refresh = TokenRefresh { _, _ ->
                NativeAuthResult.Success(tokens("new-access", "refresh-2", expiresAt = 2_000))
            },
        )

        val result = actions.claim(claimRequest())

        val failed = assertIs<PairingActionResult.Failed>(result)
        assertEquals(PairingFailure.AuthenticationRequired, failed.failure)
        assertNull(vault.tokens)
        assertEquals(1, vault.clearCount)
    }

    @Test
    fun `status read returns refreshed owner view without mutation identity`() = runTest {
        val active = claimedOwnerView().copy(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.ACTIVE,
            revision = 4,
        )
        val api = FakePairingOwnerApi(
            statusResults = ArrayDeque(listOf(PairingResult.Success(active))),
        )
        val actions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = api,
        )

        val result = actions.status(
            PairingSessionId(SESSION_ID),
            claimedOwnerView().copy(
                state = PairingSessionState.CONFIRMED,
                activationState = PairingActivationState.AWAITING_PROOF,
                revision = 3,
            ),
        )

        assertEquals(active, assertIs<PairingActionResult.Data<PairingOwnerView>>(result).value)
        assertEquals(1, api.statusCalls)
        assertEquals(listOf("access"), api.statusAccessTokens)
    }

    @Test
    fun `status read maps missing unauthorized and transport without delivery ambiguity`() =
        runTest {
            val awaiting = claimedOwnerView().copy(
                state = PairingSessionState.CONFIRMED,
                activationState = PairingActivationState.AWAITING_PROOF,
                revision = 3,
            )
            val missing = actions(
                vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
                api = FakePairingOwnerApi(
                    statusResults = ArrayDeque(
                        listOf(
                            PairingResult.HttpFailure(
                                404,
                                PairingErrorCode.PAIRING_NOT_FOUND,
                            ),
                        ),
                    ),
                ),
            )
            val unavailable = actions(
                vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
                api = FakePairingOwnerApi(
                    statusResults = ArrayDeque(listOf(PairingResult.NetworkFailure())),
                ),
            )

            assertEquals(
                PairingFailure.Contract(PairingErrorCode.PAIRING_NOT_FOUND),
                assertIs<PairingActionResult.Failed>(
                    missing.status(PairingSessionId(SESSION_ID), awaiting),
                ).failure,
            )
            assertEquals(
                PairingFailure.Unavailable,
                assertIs<PairingActionResult.Failed>(
                    unavailable.status(PairingSessionId(SESSION_ID), awaiting),
                ).failure,
            )
        }

    @Test
    fun `network and invalid response both preserve delivery identity`() = runTest {
        val vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000))
        val networkActions = actions(
            vault = vault,
            api = FakePairingOwnerApi(
                claimResults = ArrayDeque(listOf(PairingResult.NetworkFailure())),
            ),
        )
        val invalidActions = actions(
            vault = vault,
            api = FakePairingOwnerApi(
                claimResults = ArrayDeque(listOf(PairingResult.InvalidResponse())),
            ),
        )

        assertEquals(
            PairingFailure.DeliveryUnknown,
            assertIs<PairingActionResult.Failed>(networkActions.claim(claimRequest())).failure,
        )
        assertEquals(
            PairingFailure.DeliveryUnknown,
            assertIs<PairingActionResult.Failed>(invalidActions.claim(claimRequest())).failure,
        )
    }

    @Test
    fun `network delivery unknown replays equivalent claim with same idempotency key`() = runTest {
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.NetworkFailure(),
                    PairingResult.HttpFailure(410, PairingErrorCode.PAIRING_EXPIRED),
                ),
            ),
        )
        val actions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = api,
        )

        val first = actions.claim(claimRequest())
        actions.claim(claimRequest())

        assertEquals(
            PairingFailure.DeliveryUnknown,
            assertIs<PairingActionResult.Failed>(first).failure,
        )
        assertEquals(2, api.claimCalls)
        assertEquals(api.claimIdempotencyKeys.first(), api.claimIdempotencyKeys.last())
    }

    @Test
    fun `delivery unknown blocks a changed claim payload`() = runTest {
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.NetworkFailure(),
                    PairingResult.NetworkFailure(),
                ),
            ),
        )
        val actions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = api,
        )

        actions.claim(claimRequest())
        actions.claim(claimRequest(deviceDisplayName = "Different Mac"))

        assertEquals(1, api.claimCalls)
    }

    @Test
    fun `confirm cancel and revoke replay each pending payload with its original key`() = runTest {
        val confirmApi = FakePairingOwnerApi(
            confirmResults = lostThenDefinitiveOwnerResult(),
        )
        val confirmActions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = confirmApi,
        )
        val confirmRequest = ConfirmPairingRequest(FINGERPRINT, expectedRevision = 2)
        repeat(2) {
            confirmActions.confirm(
                PairingSessionId(SESSION_ID),
                claimedOwnerView(),
                confirmRequest,
            )
        }
        assertEquals(
            confirmApi.confirmIdempotencyKeys.first(),
            confirmApi.confirmIdempotencyKeys.last(),
        )

        val cancelApi = FakePairingOwnerApi(
            cancelResults = lostThenDefinitiveOwnerResult(),
        )
        val cancelActions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = cancelApi,
        )
        val cancelRequest = CancelPairingRequest(
            PairingCancelReason.OWNER_CANCELLED,
            expectedRevision = 2,
        )
        repeat(2) {
            cancelActions.cancel(
                PairingSessionId(SESSION_ID),
                claimedOwnerView(),
                cancelRequest,
            )
        }
        assertEquals(
            cancelApi.cancelIdempotencyKeys.first(),
            cancelApi.cancelIdempotencyKeys.last(),
        )

        val revokeApi = FakePairingOwnerApi(
            revokeResults = ArrayDeque(
                listOf(
                    PairingResult.NetworkFailure(),
                    PairingResult.HttpFailure(409, PairingErrorCode.PAIRING_STATE_CONFLICT),
                ),
            ),
        )
        val revokeActions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = revokeApi,
        )
        val revokeRequest = RevokePairingDeviceRequest(
            DeviceRevokeReason.USER_REQUESTED,
            expectedRevision = 3,
        )
        repeat(2) {
            revokeActions.revoke(PairingDeviceId(DEVICE_ID), revokeRequest)
        }
        assertEquals(
            revokeApi.revokeIdempotencyKeys.first(),
            revokeApi.revokeIdempotencyKeys.last(),
        )
    }

    @Test
    fun `pending claim survives authentication blocking and reuses frozen key afterward`() = runTest {
        val vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000))
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.NetworkFailure(),
                    PairingResult.HttpFailure(410, PairingErrorCode.PAIRING_EXPIRED),
                ),
            ),
        )
        val actions = actions(vault = vault, api = api)
        val frozenRequest = claimRequest()

        assertEquals(
            PairingFailure.DeliveryUnknown,
            assertIs<PairingActionResult.Failed>(
                actions.claim(frozenRequest),
            ).failure,
        )
        vault.tokens = null
        assertEquals(
            PairingFailure.AuthenticationRequired,
            assertIs<PairingActionResult.Failed>(
                actions.claim(frozenRequest),
            ).failure,
        )
        assertEquals(1, api.claimCalls)

        vault.tokens = tokens("reauthenticated", "refresh-2", expiresAt = 2_000)
        actions.claim(frozenRequest)

        assertEquals(2, api.claimCalls)
        assertEquals(
            api.claimIdempotencyKeys.first(),
            api.claimIdempotencyKeys.last(),
        )
    }

    @Test
    fun `invalid replay response keeps pending key until a definitive result`() = runTest {
        val api = FakePairingOwnerApi(
            claimResults = ArrayDeque(
                listOf(
                    PairingResult.NetworkFailure(),
                    PairingResult.InvalidResponse(),
                    PairingResult.HttpFailure(410, PairingErrorCode.PAIRING_EXPIRED),
                ),
            ),
        )
        val actions = actions(
            vault = FakeTokenVault(tokens("access", "refresh", expiresAt = 1_000)),
            api = api,
        )
        val frozenRequest = claimRequest()

        actions.claim(frozenRequest)
        assertEquals(
            PairingFailure.DeliveryUnknown,
            assertIs<PairingActionResult.Failed>(
                actions.claim(frozenRequest),
            ).failure,
        )
        actions.claim(frozenRequest)

        assertEquals(3, api.claimCalls)
        assertEquals(1, api.claimIdempotencyKeys.distinct().size)
    }

    private fun actions(
        vault: FakeTokenVault,
        api: FakePairingOwnerApi,
        refresh: TokenRefresh = TokenRefresh { _, _ -> error("Unexpected refresh") },
    ) = AuthenticatedPairingOwnerActions(
        endpoint = endpoint,
        tokenVault = vault,
        pairingApi = api,
        tokenRefresh = refresh,
        clockEpochSeconds = { 100 },
    )

    private fun claimRequest(
        deviceDisplayName: String = "Office Mac",
    ) = ClaimPairingRequest(
        pairingCode = PairingCode.fromUserInput("2AB3-C4D5"),
        workspaceId = "55555555-5555-4555-8555-555555555555",
        agentId = "66666666-6666-4666-8666-666666666666",
        deviceDisplayName = deviceDisplayName,
        scopes = setOf(PairingScope.SESSION_OBSERVE),
    )

    private fun lostThenDefinitiveOwnerResult(): ArrayDeque<PairingResult<PairingOwnerView>> =
        ArrayDeque(
            listOf(
                PairingResult.NetworkFailure(),
                PairingResult.HttpFailure(409, PairingErrorCode.PAIRING_STATE_CONFLICT),
            ),
        )

    private fun claimedOwnerView() = PairingOwnerView(
        pairingOfferId = "11111111-1111-4111-8111-111111111111",
        pairingSessionId = PairingSessionId(SESSION_ID),
        state = PairingSessionState.CLAIMED,
        activationState = PairingActivationState.WAITING_OWNER_CONFIRMATION,
        connector = PairingConnectorReview(
            displayName = "Hermes Connector",
            platformFamily = "macos",
            version = "1.0.0",
            keyAlgorithm = "Ed25519",
        ),
        binding = PairingOwnerBinding(
            tenantId = "33333333-3333-4333-8333-333333333333",
            userId = "44444444-4444-4444-8444-444444444444",
            workspaceId = "55555555-5555-4555-8555-555555555555",
            agentId = "66666666-6666-4666-8666-666666666666",
            deviceId = PairingDeviceId(DEVICE_ID),
            credentialId = "88888888-8888-4888-8888-888888888888",
            scopes = setOf(PairingScope.SESSION_OBSERVE),
        ),
        credentialFingerprint = FINGERPRINT,
        expiresAt = "2026-08-01T00:05:00Z",
        revision = 2,
        deviceRevision = 4,
    )

    private fun tokens(access: String, refresh: String, expiresAt: Long) = NativeTokens(
        accessToken = access,
        refreshToken = refresh,
        tokenType = "Bearer",
        expiresAtEpochSeconds = expiresAt,
        provider = "basic",
        userId = "owner",
    )

    private class FakeTokenVault(
        var tokens: NativeTokens? = null,
    ) : TokenVault {
        var clearCount = 0

        override fun save(endpointId: String, tokens: NativeTokens) {
            this.tokens = tokens
        }

        override fun load(endpointId: String): NativeTokens? = tokens

        override fun clear(endpointId: String) {
            tokens = null
            clearCount += 1
        }
    }

    private class FakePairingOwnerApi(
        private val claimResults: ArrayDeque<PairingResult<PairingOwnerView>> = ArrayDeque(),
        private val confirmResults: ArrayDeque<PairingResult<PairingOwnerView>> = ArrayDeque(),
        private val statusResults: ArrayDeque<PairingResult<PairingOwnerView>> = ArrayDeque(),
        private val cancelResults: ArrayDeque<PairingResult<PairingOwnerView>> = ArrayDeque(),
        private val revokeResults: ArrayDeque<PairingResult<RevokedPairingDevice>> = ArrayDeque(),
    ) : PairingOwnerApi {
        var claimCalls = 0
        var statusCalls = 0
        val accessTokens = mutableListOf<String>()
        val statusAccessTokens = mutableListOf<String>()
        val claimIdempotencyKeys = mutableListOf<PairingIdempotencyKey>()
        val confirmIdempotencyKeys = mutableListOf<PairingIdempotencyKey>()
        val cancelIdempotencyKeys = mutableListOf<PairingIdempotencyKey>()
        val revokeIdempotencyKeys = mutableListOf<PairingIdempotencyKey>()

        override suspend fun claim(
            endpoint: GatewayEndpoint,
            accessToken: String,
            idempotencyKey: PairingIdempotencyKey,
            request: ClaimPairingRequest,
        ): PairingResult<PairingOwnerView> {
            claimCalls += 1
            accessTokens += accessToken
            claimIdempotencyKeys += idempotencyKey
            return claimResults.removeFirstOrNull()
                ?: PairingResult.NetworkFailure()
        }

        override suspend fun confirm(
            endpoint: GatewayEndpoint,
            accessToken: String,
            sessionId: PairingSessionId,
            expectedOwnerView: PairingOwnerView,
            idempotencyKey: PairingIdempotencyKey,
            request: ConfirmPairingRequest,
        ): PairingResult<PairingOwnerView> {
            confirmIdempotencyKeys += idempotencyKey
            return confirmResults.removeFirstOrNull()
                ?: PairingResult.NetworkFailure()
        }

        override suspend fun status(
            endpoint: GatewayEndpoint,
            accessToken: String,
            sessionId: PairingSessionId,
            expectedOwnerView: PairingOwnerView,
        ): PairingResult<PairingOwnerView> {
            statusCalls += 1
            statusAccessTokens += accessToken
            return statusResults.removeFirstOrNull()
                ?: PairingResult.NetworkFailure()
        }

        override suspend fun cancel(
            endpoint: GatewayEndpoint,
            accessToken: String,
            sessionId: PairingSessionId,
            expectedOwnerView: PairingOwnerView,
            idempotencyKey: PairingIdempotencyKey,
            request: CancelPairingRequest,
        ): PairingResult<PairingOwnerView> {
            cancelIdempotencyKeys += idempotencyKey
            return cancelResults.removeFirstOrNull()
                ?: PairingResult.NetworkFailure()
        }

        override suspend fun revoke(
            endpoint: GatewayEndpoint,
            accessToken: String,
            deviceId: PairingDeviceId,
            idempotencyKey: PairingIdempotencyKey,
            request: RevokePairingDeviceRequest,
        ): PairingResult<RevokedPairingDevice> {
            revokeIdempotencyKeys += idempotencyKey
            return revokeResults.removeFirstOrNull()
                ?: PairingResult.NetworkFailure()
        }
    }

    private companion object {
        const val SESSION_ID = "22222222-2222-4222-8222-222222222222"
        const val DEVICE_ID = "77777777-7777-4777-8777-777777777777"
        const val FINGERPRINT = "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    }
}
