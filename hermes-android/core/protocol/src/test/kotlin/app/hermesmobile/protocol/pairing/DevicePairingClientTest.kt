package app.hermesmobile.protocol.pairing

import app.hermesmobile.protocol.GatewayEndpoint
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

class DevicePairingClientTest {
    private lateinit var server: MockWebServer
    private lateinit var endpoint: GatewayEndpoint
    private lateinit var client: DevicePairingClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        endpoint = GatewayEndpoint.parse(server.url("/hermes/").toString()).getOrThrow()
        client = DevicePairingClient(OkHttpClient())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `pairing code accepts eight Crockford characters and is always redacted`() {
        val code = PairingCode.fromUserInput("2ab3 c4d5")

        assertEquals("2AB3-C4D5", code.requestValue)
        assertFalse(code.toString().contains("2AB3"))
        assertFailsWith<IllegalArgumentException> {
            PairingCode.fromUserInput("2ABI-C4D5")
        }
    }

    @Test
    fun `claim uses owner bearer UUID idempotency and strict contract body`() = runTest {
        server.enqueue(successOwnerView())
        val secretCode = PairingCode.fromUserInput("2AB3-C4D5")

        val result = client.claim(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            idempotencyKey = IDEMPOTENCY_KEY,
            request = ClaimPairingRequest(
                pairingCode = secretCode,
                workspaceId = WORKSPACE_ID,
                agentId = AGENT_ID,
                deviceDisplayName = "Office Mac",
                scopes = setOf(PairingScope.SESSION_OBSERVE, PairingScope.SESSION_CONTROL_REQUEST),
            ),
        )

        val view = assertIs<PairingResult.Success<PairingOwnerView>>(result).value
        assertEquals("Hermes Connector", view.connector.displayName)
        assertEquals("macos", view.connector.platformFamily)
        assertEquals("1.0.0", view.connector.version)
        assertEquals("Ed25519", view.connector.keyAlgorithm)
        assertEquals(FINGERPRINT, view.credentialFingerprint)
        assertEquals(WORKSPACE_ID, view.binding.workspaceId)
        assertEquals(AGENT_ID, view.binding.agentId)
        assertEquals(
            setOf(PairingScope.SESSION_OBSERVE, PairingScope.SESSION_CONTROL_REQUEST),
            view.binding.scopes,
        )
        assertEquals(PairingSessionState.CLAIMED, view.state)
        assertEquals(PairingActivationState.WAITING_OWNER_CONFIRMATION, view.activationState)
        assertEquals(4, view.deviceRevision)
        assertEquals("2026-08-01T00:05:00Z", view.expiresAt)

        val recorded = server.takeRequest()
        assertEquals("/hermes/api/device-pairing/claims", recorded.path)
        assertEquals("Bearer owner-access-token", recorded.getHeader("Authorization"))
        assertEquals(IDEMPOTENCY_KEY.value, recorded.getHeader("Idempotency-Key"))
        assertEquals("application/json", recorded.getHeader("Accept"))
        assertEquals(
            JsonObject(
                mapOf(
                    "pairing_code" to JsonPrimitive("2AB3-C4D5"),
                    "workspace_id" to JsonPrimitive(WORKSPACE_ID),
                    "agent_id" to JsonPrimitive(AGENT_ID),
                    "device_display_name" to JsonPrimitive("Office Mac"),
                    "scopes" to JsonArray(
                        listOf(
                            JsonPrimitive("session.observe"),
                            JsonPrimitive("session.control.request"),
                        ),
                    ),
                    "expected_revision" to JsonPrimitive(1),
                ),
            ),
            Json.parseToJsonElement(recorded.body.readUtf8()).jsonObject,
        )
        assertFalse(result.toString().contains("2AB3-C4D5"))
        assertFalse(result.toString().contains("owner-access-token"))
    }

    @Test
    fun `owner view rejects unknown response fields instead of silently widening contract`() = runTest {
        server.enqueue(
            successOwnerView(
                bodySuffix = """,
                  "unexpected_authority": "tenant-self-assertion"
                """.trimIndent(),
            ),
        )

        val result = client.claim(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            idempotencyKey = IDEMPOTENCY_KEY,
            request = claimRequest(),
        )

        assertIs<PairingResult.InvalidResponse>(result)
    }

    @Test
    fun `owner view requires a positive device lifecycle revision`() = runTest {
        server.enqueue(successOwnerView(deviceRevision = null))
        server.enqueue(successOwnerView(deviceRevision = 0))

        repeat(2) {
            assertIs<PairingResult.InvalidResponse>(
                client.claim(
                    endpoint = endpoint,
                    accessToken = "owner-access-token",
                    idempotencyKey = IDEMPOTENCY_KEY,
                    request = claimRequest(),
                ),
            )
        }
    }

    @Test
    fun `claim rejects response binding that differs from owner selection`() = runTest {
        server.enqueue(successOwnerView(workspaceId = OTHER_WORKSPACE_ID))
        server.enqueue(successOwnerView(agentId = OTHER_AGENT_ID))
        server.enqueue(
            successOwnerView(
                bindingScopes = listOf(
                    "session.observe",
                    "session.control.request",
                ),
            ),
        )
        val request = claimRequest()

        repeat(3) {
            val result = client.claim(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )

            assertIs<PairingResult.InvalidResponse>(result)
        }
    }

    @Test
    fun `claim accepts only claimed state awaiting owner confirmation`() = runTest {
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "awaiting_proof",
                bindingScopes = listOf("session.observe"),
            ),
        )

        val result = client.claim(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            idempotencyKey = IDEMPOTENCY_KEY,
            request = claimRequest(),
        )

        assertIs<PairingResult.InvalidResponse>(result)
    }

    @Test
    fun `confirm echoes the reviewed fingerprint and current revision`() = runTest {
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "awaiting_proof",
                revision = 3,
            ),
        )

        val result = client.confirm(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            sessionId = PairingSessionId(SESSION_ID),
            expectedOwnerView = claimedOwnerView(),
            idempotencyKey = IDEMPOTENCY_KEY,
            request = ConfirmPairingRequest(
                credentialFingerprint = FINGERPRINT,
                expectedRevision = 2,
            ),
        )

        val view = assertIs<PairingResult.Success<PairingOwnerView>>(result).value
        assertEquals(PairingSessionState.CONFIRMED, view.state)
        assertEquals(PairingActivationState.AWAITING_PROOF, view.activationState)
        assertEquals(3, view.revision)
        val recorded = server.takeRequest()
        assertEquals("/hermes/api/device-pairing/sessions/$SESSION_ID/confirm", recorded.path)
        assertEquals(
            JsonObject(
                mapOf(
                    "credential_fingerprint" to JsonPrimitive(FINGERPRINT),
                    "expected_revision" to JsonPrimitive(2),
                ),
            ),
            Json.parseToJsonElement(recorded.body.readUtf8()).jsonObject,
        )
    }

    @Test
    fun `status uses owner bearer no-store GET and accepts active immutable snapshot`() = runTest {
        val awaitingProof = claimedOwnerView().copy(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
            deviceRevision = 4,
        )
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "active",
                revision = 4,
                deviceRevision = 5,
            ),
        )

        val result = client.status(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            sessionId = PairingSessionId(SESSION_ID),
            expectedOwnerView = awaitingProof,
        )

        val view = assertIs<PairingResult.Success<PairingOwnerView>>(result).value
        assertEquals(PairingActivationState.ACTIVE, view.activationState)
        assertEquals(4, view.revision)
        assertEquals(5, view.deviceRevision)
        val recorded = server.takeRequest()
        assertEquals("GET", recorded.method)
        assertEquals("/hermes/api/device-pairing/sessions/$SESSION_ID", recorded.path)
        assertEquals("Bearer owner-access-token", recorded.getHeader("Authorization"))
        assertEquals("no-store", recorded.getHeader("Cache-Control"))
        assertNull(recorded.getHeader("Idempotency-Key"))
        assertEquals(0L, recorded.bodySize)
    }

    @Test
    fun `status accepts confirmed blocked activation with advancing device revision`() = runTest {
        val awaitingProof = claimedOwnerView().copy(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
            deviceRevision = 4,
        )
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "blocked",
                revision = 4,
                deviceRevision = 5,
            ),
        )

        val result = client.status(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            sessionId = PairingSessionId(SESSION_ID),
            expectedOwnerView = awaitingProof,
        )

        val view = assertIs<PairingResult.Success<PairingOwnerView>>(result).value
        assertEquals(PairingSessionState.CONFIRMED, view.state)
        assertEquals(PairingActivationState.BLOCKED, view.activationState)
        assertEquals(4, view.revision)
        assertEquals(5, view.deviceRevision)
    }

    @Test
    fun `status rejects immutable drift revision regression and incoherent lifecycle`() = runTest {
        val awaitingProof = claimedOwnerView().copy(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
            deviceRevision = 4,
        )
        server.enqueue(
            successOwnerView(
                credentialFingerprint = OTHER_FINGERPRINT,
                state = "confirmed",
                activationState = "active",
                revision = 4,
            ),
        )
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "active",
                revision = 2,
            ),
        )
        server.enqueue(
            successOwnerView(
                state = "claimed",
                activationState = "active",
                revision = 4,
            ),
        )
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "active",
                revision = 4,
                deviceRevision = 3,
            ),
        )
        server.enqueue(
            successOwnerView(
                state = "claimed",
                activationState = "blocked",
                revision = 4,
                deviceRevision = 5,
            ),
        )

        repeat(5) {
            assertIs<PairingResult.InvalidResponse>(
                client.status(
                    endpoint = endpoint,
                    accessToken = "owner-access-token",
                    sessionId = PairingSessionId(SESSION_ID),
                    expectedOwnerView = awaitingProof,
                ),
            )
        }
    }

    @Test
    fun `status preserves retry after and exposes bounded retry server failures`() = runTest {
        val awaitingProof = claimedOwnerView().copy(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
            deviceRevision = 4,
        )
        server.enqueue(errorResponse(429, "RATE_LIMITED", retryAfter = "2"))
        server.enqueue(errorResponse(503, "UNKNOWN"))

        val rateLimited = assertIs<PairingResult.HttpFailure>(
            client.status(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = awaitingProof,
            ),
        )
        assertEquals(PairingErrorCode.RATE_LIMITED, rateLimited.code)
        assertEquals(2, rateLimited.retryAfterSeconds)
        val unavailable = assertIs<PairingResult.HttpFailure>(
            client.status(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = awaitingProof,
            ),
        )
        assertEquals(503, unavailable.statusCode)
        assertEquals(PairingErrorCode.UNKNOWN, unavailable.code)
    }

    @Test
    fun `confirm rejects illegal or mismatched expected snapshot before HTTP`() = runTest {
        repeat(4) {
            server.enqueue(
                successOwnerView(
                    state = "confirmed",
                    activationState = "awaiting_proof",
                    revision = 3,
                ),
            )
        }
        val request = ConfirmPairingRequest(
            credentialFingerprint = FINGERPRINT,
            expectedRevision = 2,
        )
        val invalidSnapshots = listOf(
            claimedOwnerView().copy(
                state = PairingSessionState.CONFIRMED,
                activationState = PairingActivationState.AWAITING_PROOF,
            ),
            claimedOwnerView().copy(
                pairingSessionId = PairingSessionId(OTHER_SESSION_ID),
            ),
            claimedOwnerView().copy(
                credentialFingerprint = OTHER_FINGERPRINT,
            ),
            claimedOwnerView().copy(revision = 3),
        )

        val results = invalidSnapshots.map { expected ->
            client.confirm(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = expected,
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )
        }

        assertTrue(results.all { it is PairingResult.InvalidResponse })
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `confirm rejects mismatched session fingerprint and cross-operation state`() = runTest {
        server.enqueue(
            successOwnerView(
                pairingSessionId = OTHER_SESSION_ID,
                state = "confirmed",
                activationState = "awaiting_proof",
                revision = 3,
            ),
        )
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "awaiting_proof",
                credentialFingerprint = OTHER_FINGERPRINT,
                revision = 3,
            ),
        )
        server.enqueue(successOwnerView(revision = 3))
        val request = ConfirmPairingRequest(
            credentialFingerprint = FINGERPRINT,
            expectedRevision = 2,
        )

        repeat(3) {
            val result = client.confirm(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = claimedOwnerView(),
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )

            assertIs<PairingResult.InvalidResponse>(result)
        }
    }

    @Test
    fun `confirm rejects any mutation of the claimed owner snapshot`() = runTest {
        listOf(
            successOwnerView(pairingOfferId = OTHER_OFFER_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(tenantId = OTHER_TENANT_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(userId = OTHER_USER_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(workspaceId = OTHER_WORKSPACE_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(agentId = OTHER_AGENT_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(deviceId = OTHER_DEVICE_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(credentialId = OTHER_CREDENTIAL_ID, state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(bindingScopes = listOf("session.observe"), state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(expiresAt = "2026-08-01T00:06:00Z", state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(displayName = "Changed Connector", state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(platformFamily = "linux", state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(connectorVersion = "2.0.0", state = "confirmed", activationState = "awaiting_proof", revision = 3),
            successOwnerView(keyAlgorithm = "Other", state = "confirmed", activationState = "awaiting_proof", revision = 3),
        ).forEach(server::enqueue)
        val request = ConfirmPairingRequest(
            credentialFingerprint = FINGERPRINT,
            expectedRevision = 2,
        )

        repeat(13) {
            val result = client.confirm(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = claimedOwnerView(),
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )

            assertIs<PairingResult.InvalidResponse>(result)
        }
    }

    @Test
    fun `cancel records exact owner reason and revision`() = runTest {
        server.enqueue(
            successOwnerView(
                state = "cancelled",
                activationState = "blocked",
                revision = 3,
            ),
        )

        val result = client.cancel(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            sessionId = PairingSessionId(SESSION_ID),
            expectedOwnerView = claimedOwnerView(),
            idempotencyKey = IDEMPOTENCY_KEY,
            request = CancelPairingRequest(
                reason = PairingCancelReason.FINGERPRINT_MISMATCH,
                expectedRevision = 2,
            ),
        )

        assertIs<PairingResult.Success<PairingOwnerView>>(result)
        val recorded = server.takeRequest()
        assertEquals("/hermes/api/device-pairing/sessions/$SESSION_ID/cancel", recorded.path)
        assertEquals(
            JsonObject(
                mapOf(
                    "reason" to JsonPrimitive("fingerprint_mismatch"),
                    "expected_revision" to JsonPrimitive(2),
                ),
            ),
            Json.parseToJsonElement(recorded.body.readUtf8()).jsonObject,
        )
    }

    @Test
    fun `cancel rejects illegal or mismatched expected snapshot before HTTP`() = runTest {
        repeat(4) {
            server.enqueue(
                successOwnerView(
                    state = "cancelled",
                    activationState = "blocked",
                    revision = 3,
                ),
            )
        }
        val request = CancelPairingRequest(
            reason = PairingCancelReason.OWNER_CANCELLED,
            expectedRevision = 2,
        )
        val invalidSnapshots = listOf(
            claimedOwnerView().copy(
                state = PairingSessionState.CONFIRMED,
                activationState = PairingActivationState.ACTIVE,
            ),
            claimedOwnerView().copy(
                pairingSessionId = PairingSessionId(OTHER_SESSION_ID),
            ),
            claimedOwnerView().copy(
                credentialFingerprint = "invalid-fingerprint",
            ),
            claimedOwnerView().copy(revision = 3),
        )

        val results = invalidSnapshots.map { expected ->
            client.cancel(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = expected,
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )
        }

        assertTrue(results.all { it is PairingResult.InvalidResponse })
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `cancel accepts confirmed session awaiting connector proof`() = runTest {
        server.enqueue(
            successOwnerView(
                state = "cancelled",
                activationState = "blocked",
                revision = 4,
            ),
        )
        val awaitingProof = claimedOwnerView().copy(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
        )

        val result = client.cancel(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            sessionId = PairingSessionId(SESSION_ID),
            expectedOwnerView = awaitingProof,
            idempotencyKey = IDEMPOTENCY_KEY,
            request = CancelPairingRequest(
                reason = PairingCancelReason.OWNER_CANCELLED,
                expectedRevision = 3,
            ),
        )

        assertIs<PairingResult.Success<PairingOwnerView>>(result)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `cancel rejects mismatched session and non-cancelled state`() = runTest {
        server.enqueue(
            successOwnerView(
                pairingSessionId = OTHER_SESSION_ID,
                state = "cancelled",
                activationState = "blocked",
                revision = 3,
            ),
        )
        server.enqueue(
            successOwnerView(
                state = "confirmed",
                activationState = "awaiting_proof",
                revision = 3,
            ),
        )
        val request = CancelPairingRequest(
            reason = PairingCancelReason.OWNER_CANCELLED,
            expectedRevision = 2,
        )

        repeat(2) {
            val result = client.cancel(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = claimedOwnerView(),
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )

            assertIs<PairingResult.InvalidResponse>(result)
        }
    }

    @Test
    fun `cancel rejects mutated binding or fixed expiry from claimed snapshot`() = runTest {
        server.enqueue(
            successOwnerView(
                deviceId = OTHER_DEVICE_ID,
                state = "cancelled",
                activationState = "blocked",
                revision = 3,
            ),
        )
        server.enqueue(
            successOwnerView(
                expiresAt = "2026-08-01T00:06:00Z",
                state = "cancelled",
                activationState = "blocked",
                revision = 3,
            ),
        )
        val request = CancelPairingRequest(
            reason = PairingCancelReason.OWNER_CANCELLED,
            expectedRevision = 2,
        )

        repeat(2) {
            val result = client.cancel(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                sessionId = PairingSessionId(SESSION_ID),
                expectedOwnerView = claimedOwnerView(),
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )

            assertIs<PairingResult.InvalidResponse>(result)
        }
    }

    @Test
    fun `revoke is owner authenticated and decodes only revoked response`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """
                    {
                      "device_id": "$DEVICE_ID",
                      "status": "revoked",
                      "revision": 4,
                      "revoked_at": "2026-08-01T00:30:00Z"
                    }
                    """.trimIndent(),
                ),
        )

        val result = client.revoke(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            deviceId = PairingDeviceId(DEVICE_ID),
            idempotencyKey = IDEMPOTENCY_KEY,
            request = RevokePairingDeviceRequest(
                reason = DeviceRevokeReason.USER_REQUESTED,
                expectedRevision = 3,
            ),
        )

        val revoked = assertIs<PairingResult.Success<RevokedPairingDevice>>(result).value
        assertEquals(DEVICE_ID, revoked.deviceId.value)
        assertEquals(4, revoked.revision)
        assertEquals("2026-08-01T00:30:00Z", revoked.revokedAt)
        val recorded = server.takeRequest()
        assertEquals("/hermes/api/devices/$DEVICE_ID/revoke", recorded.path)
        assertEquals("Bearer owner-access-token", recorded.getHeader("Authorization"))
        assertEquals(IDEMPOTENCY_KEY.value, recorded.getHeader("Idempotency-Key"))
    }

    @Test
    fun `revoke rejects mismatched device and unexpected revision`() = runTest {
        server.enqueue(successRevokedDevice(deviceId = OTHER_DEVICE_ID))
        server.enqueue(successRevokedDevice(revision = 6))
        val request = RevokePairingDeviceRequest(
            reason = DeviceRevokeReason.USER_REQUESTED,
            expectedRevision = 3,
        )

        repeat(2) {
            val result = client.revoke(
                endpoint = endpoint,
                accessToken = "owner-access-token",
                deviceId = PairingDeviceId(DEVICE_ID),
                idempotencyKey = IDEMPOTENCY_KEY,
                request = request,
            )

            assertIs<PairingResult.InvalidResponse>(result)
        }
    }

    @Test
    fun `all owner mutation successes require exact HTTP 200`() = runTest {
        listOf(201, 202, 204).forEach { status ->
            server.enqueue(successOwnerView().withSuccessStatus(status))
            assertIs<PairingResult.InvalidResponse>(
                client.claim(
                    endpoint = endpoint,
                    accessToken = "owner-access-token",
                    idempotencyKey = IDEMPOTENCY_KEY,
                    request = claimRequest(),
                ),
            )

            server.enqueue(
                successOwnerView(
                    state = "confirmed",
                    activationState = "awaiting_proof",
                    revision = 3,
                ).withSuccessStatus(status),
            )
            assertIs<PairingResult.InvalidResponse>(
                client.confirm(
                    endpoint = endpoint,
                    accessToken = "owner-access-token",
                    sessionId = PairingSessionId(SESSION_ID),
                    expectedOwnerView = claimedOwnerView(),
                    idempotencyKey = IDEMPOTENCY_KEY,
                    request = ConfirmPairingRequest(
                        credentialFingerprint = FINGERPRINT,
                        expectedRevision = 2,
                    ),
                ),
            )

            server.enqueue(
                successOwnerView(
                    state = "cancelled",
                    activationState = "blocked",
                    revision = 3,
                ).withSuccessStatus(status),
            )
            assertIs<PairingResult.InvalidResponse>(
                client.cancel(
                    endpoint = endpoint,
                    accessToken = "owner-access-token",
                    sessionId = PairingSessionId(SESSION_ID),
                    expectedOwnerView = claimedOwnerView(),
                    idempotencyKey = IDEMPOTENCY_KEY,
                    request = CancelPairingRequest(
                        reason = PairingCancelReason.OWNER_CANCELLED,
                        expectedRevision = 2,
                    ),
                ),
            )

            server.enqueue(successRevokedDevice().withSuccessStatus(status))
            assertIs<PairingResult.InvalidResponse>(
                client.revoke(
                    endpoint = endpoint,
                    accessToken = "owner-access-token",
                    deviceId = PairingDeviceId(DEVICE_ID),
                    idempotencyKey = IDEMPOTENCY_KEY,
                    request = RevokePairingDeviceRequest(
                        reason = DeviceRevokeReason.USER_REQUESTED,
                        expectedRevision = 3,
                    ),
                ),
            )
        }
    }

    @Test
    fun `claim throttling exposes bounded retry only and never server reason`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(429)
                .setHeader("Content-Type", "application/json")
                .setHeader("Retry-After", "120")
                .setBody(
                    """
                    {
                      "code": "PAIRING_CLAIM_RATE_LIMITED",
                      "reason": "pairing claims temporarily unavailable"
                    }
                    """.trimIndent(),
                ),
        )

        val result = client.claim(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            idempotencyKey = IDEMPOTENCY_KEY,
            request = claimRequest(),
        )

        val failure = assertIs<PairingResult.HttpFailure>(result)
        assertEquals(429, failure.statusCode)
        assertEquals(PairingErrorCode.PAIRING_CLAIM_RATE_LIMITED, failure.code)
        assertEquals(120, failure.retryAfterSeconds)
        assertFalse(failure.summary.contains("2AB3-C4D5"))
        assertFalse(failure.toString().contains("2AB3-C4D5"))
    }

    @Test
    fun `unavailable claim does not infer offer existence state or expiry`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(404)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """
                    {
                      "code": "PAIRING_CLAIM_UNAVAILABLE",
                      "reason": "pairing claim unavailable"
                    }
                    """.trimIndent(),
                ),
        )

        val result = client.claim(
            endpoint = endpoint,
            accessToken = "owner-access-token",
            idempotencyKey = IDEMPOTENCY_KEY,
            request = claimRequest(),
        )

        val failure = assertIs<PairingResult.HttpFailure>(result)
        assertEquals(PairingErrorCode.PAIRING_CLAIM_UNAVAILABLE, failure.code)
        assertNull(failure.retryAfterSeconds)
        assertFalse(failure.summary.contains("expired", ignoreCase = true))
        assertFalse(failure.summary.contains("cancelled", ignoreCase = true))
    }

    @Test
    fun `each owner operation rejects error codes from another operation`() = runTest {
        server.enqueue(errorResponse(404, "PAIRING_NOT_FOUND"))
        server.enqueue(errorResponse(404, "PAIRING_CLAIM_UNAVAILABLE"))
        server.enqueue(errorResponse(400, "PAIRING_INVALID_REQUEST"))
        server.enqueue(errorResponse(410, "PAIRING_EXPIRED"))
        server.enqueue(errorResponse(418, "FUTURE_PAIRING_ERROR"))

        assertIs<PairingResult.InvalidResponse>(
            client.claim(
                endpoint,
                "owner-access-token",
                IDEMPOTENCY_KEY,
                claimRequest(),
            ),
        )
        assertIs<PairingResult.InvalidResponse>(
            client.confirm(
                endpoint,
                "owner-access-token",
                PairingSessionId(SESSION_ID),
                claimedOwnerView(),
                IDEMPOTENCY_KEY,
                ConfirmPairingRequest(FINGERPRINT, expectedRevision = 2),
            ),
        )
        assertIs<PairingResult.InvalidResponse>(
            client.cancel(
                endpoint,
                "owner-access-token",
                PairingSessionId(SESSION_ID),
                claimedOwnerView(),
                IDEMPOTENCY_KEY,
                CancelPairingRequest(
                    PairingCancelReason.OWNER_CANCELLED,
                    expectedRevision = 2,
                ),
            ),
        )
        assertIs<PairingResult.InvalidResponse>(
            client.revoke(
                endpoint,
                "owner-access-token",
                PairingDeviceId(DEVICE_ID),
                IDEMPOTENCY_KEY,
                RevokePairingDeviceRequest(
                    DeviceRevokeReason.USER_REQUESTED,
                    expectedRevision = 3,
                ),
            ),
        )
        assertIs<PairingResult.InvalidResponse>(
            client.claim(
                endpoint,
                "owner-access-token",
                IDEMPOTENCY_KEY,
                claimRequest(),
            ),
        )
    }

    @Test
    fun `illegal Retry-After headers are rejected`() = runTest {
        server.enqueue(errorResponse(429, "PAIRING_CLAIM_RATE_LIMITED"))
        server.enqueue(errorResponse(429, "PAIRING_CLAIM_RATE_LIMITED", retryAfter = "0"))
        server.enqueue(errorResponse(409, "IDEMPOTENCY_CONFLICT", retryAfter = "10"))
        server.enqueue(errorResponse(429, "RATE_LIMITED", retryAfter = "10"))

        repeat(3) {
            assertIs<PairingResult.InvalidResponse>(
                client.claim(
                    endpoint,
                    "owner-access-token",
                    IDEMPOTENCY_KEY,
                    claimRequest(),
                ),
            )
        }
        assertIs<PairingResult.InvalidResponse>(
            client.confirm(
                endpoint,
                "owner-access-token",
                PairingSessionId(SESSION_ID),
                claimedOwnerView(),
                IDEMPOTENCY_KEY,
                ConfirmPairingRequest(FINGERPRINT, expectedRevision = 2),
            ),
        )
    }

    @Test
    fun `confirm cancel and revoke accept only their documented error pairs`() = runTest {
        server.enqueue(errorResponse(410, "PAIRING_EXPIRED"))
        server.enqueue(errorResponse(409, "PAIRING_STATE_CONFLICT"))
        server.enqueue(errorResponse(429, "RATE_LIMITED"))

        assertEquals(
            PairingErrorCode.PAIRING_EXPIRED,
            assertIs<PairingResult.HttpFailure>(
                client.confirm(
                    endpoint,
                    "owner-access-token",
                    PairingSessionId(SESSION_ID),
                    claimedOwnerView(),
                    IDEMPOTENCY_KEY,
                    ConfirmPairingRequest(FINGERPRINT, expectedRevision = 2),
                ),
            ).code,
        )
        assertEquals(
            PairingErrorCode.PAIRING_STATE_CONFLICT,
            assertIs<PairingResult.HttpFailure>(
                client.cancel(
                    endpoint,
                    "owner-access-token",
                    PairingSessionId(SESSION_ID),
                    claimedOwnerView(),
                    IDEMPOTENCY_KEY,
                    CancelPairingRequest(
                        PairingCancelReason.OWNER_CANCELLED,
                        expectedRevision = 2,
                    ),
                ),
            ).code,
        )
        assertEquals(
            PairingErrorCode.RATE_LIMITED,
            assertIs<PairingResult.HttpFailure>(
                client.revoke(
                    endpoint,
                    "owner-access-token",
                    PairingDeviceId(DEVICE_ID),
                    IDEMPOTENCY_KEY,
                    RevokePairingDeviceRequest(
                        DeviceRevokeReason.USER_REQUESTED,
                        expectedRevision = 3,
                    ),
                ),
            ).code,
        )
    }

    @Test
    fun `idempotency and path identifiers require canonical non-nil UUIDs`() {
        assertFailsWith<IllegalArgumentException> {
            PairingIdempotencyKey("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
        }
        assertFailsWith<IllegalArgumentException> {
            PairingIdempotencyKey("00000000-0000-0000-0000-000000000000")
        }
        assertFailsWith<IllegalArgumentException> {
            PairingSessionId("../sessions")
        }
        assertTrue(PairingIdempotencyKey.random().value.matches(CANONICAL_UUID))
    }

    @Test
    fun `authorization is mandatory and never omitted from owner mutations`() = runTest {
        assertFailsWith<IllegalArgumentException> {
            client.claim(
                endpoint = endpoint,
                accessToken = "",
                idempotencyKey = IDEMPOTENCY_KEY,
                request = claimRequest(),
            )
        }
        assertNull(server.takeRequest(20, java.util.concurrent.TimeUnit.MILLISECONDS))
    }

    private fun claimRequest() = ClaimPairingRequest(
        pairingCode = PairingCode.fromUserInput("2AB3-C4D5"),
        workspaceId = WORKSPACE_ID,
        agentId = AGENT_ID,
        deviceDisplayName = "Office Mac",
        scopes = setOf(PairingScope.SESSION_OBSERVE),
    )

    private fun successOwnerView(
        pairingOfferId: String = OFFER_ID,
        pairingSessionId: String = SESSION_ID,
        state: String = "claimed",
        activationState: String = "waiting_owner_confirmation",
        tenantId: String = TENANT_ID,
        userId: String = USER_ID,
        workspaceId: String = WORKSPACE_ID,
        agentId: String = AGENT_ID,
        deviceId: String = DEVICE_ID,
        credentialId: String = CREDENTIAL_ID,
        bindingScopes: List<String> = listOf(
            "session.observe",
            "session.control.request",
        ),
        displayName: String = "Hermes Connector",
        platformFamily: String = "macos",
        connectorVersion: String = "1.0.0",
        keyAlgorithm: String = "Ed25519",
        credentialFingerprint: String = FINGERPRINT,
        expiresAt: String = "2026-08-01T00:05:00Z",
        revision: Int = 2,
        deviceRevision: Int? = 4,
        bodySuffix: String = "",
    ): MockResponse = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(
            """
            {
              "pairing_offer_id": "$pairingOfferId",
              "pairing_session_id": "$pairingSessionId",
              "state": "$state",
              "activation_state": "$activationState",
              "display_name": "$displayName",
              "platform_family": "$platformFamily",
              "connector_version": "$connectorVersion",
              "key_algorithm": "$keyAlgorithm",
              "binding": {
                "tenant_id": "$tenantId",
                "user_id": "$userId",
                "workspace_id": "$workspaceId",
                "agent_id": "$agentId",
                "device_id": "$deviceId",
                "credential_id": "$credentialId",
                "scopes": [${bindingScopes.joinToString { "\"$it\"" }}]
              },
              "credential_fingerprint": "$credentialFingerprint",
              "expires_at": "$expiresAt",
              "revision": $revision
              ${deviceRevision?.let { """, "device_revision": $it""" }.orEmpty()}
              $bodySuffix
            }
            """.trimIndent(),
        )

    private fun claimedOwnerView() = PairingOwnerView(
        pairingOfferId = OFFER_ID,
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
            tenantId = TENANT_ID,
            userId = USER_ID,
            workspaceId = WORKSPACE_ID,
            agentId = AGENT_ID,
            deviceId = PairingDeviceId(DEVICE_ID),
            credentialId = CREDENTIAL_ID,
            scopes = setOf(
                PairingScope.SESSION_OBSERVE,
                PairingScope.SESSION_CONTROL_REQUEST,
            ),
        ),
        credentialFingerprint = FINGERPRINT,
        expiresAt = "2026-08-01T00:05:00Z",
        revision = 2,
        deviceRevision = 4,
    )

    private fun successRevokedDevice(
        deviceId: String = DEVICE_ID,
        revision: Int = 4,
    ): MockResponse = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(
            """
            {
              "device_id": "$deviceId",
              "status": "revoked",
              "revision": $revision,
              "revoked_at": "2026-08-01T00:30:00Z"
            }
            """.trimIndent(),
        )

    private fun errorResponse(
        status: Int,
        code: String,
        retryAfter: String? = null,
    ): MockResponse = MockResponse()
        .setResponseCode(status)
        .setHeader("Content-Type", "application/json")
        .apply {
            if (retryAfter != null) setHeader("Retry-After", retryAfter)
        }
        .setBody(
            """
            {
              "code": "$code",
              "reason": "safe pairing failure"
            }
            """.trimIndent(),
        )

    private fun MockResponse.withSuccessStatus(status: Int): MockResponse =
        setResponseCode(status).apply {
            if (status == 204) {
                setBody("")
            }
        }

    private companion object {
        const val OFFER_ID = "11111111-1111-4111-8111-111111111111"
        const val OTHER_OFFER_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        const val TENANT_ID = "33333333-3333-4333-8333-333333333333"
        const val OTHER_TENANT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        const val USER_ID = "44444444-4444-4444-8444-444444444444"
        const val OTHER_USER_ID = "12121212-1212-4212-8212-121212121212"
        const val WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
        const val OTHER_WORKSPACE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        const val AGENT_ID = "66666666-6666-4666-8666-666666666666"
        const val OTHER_AGENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        const val SESSION_ID = "22222222-2222-4222-8222-222222222222"
        const val OTHER_SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        const val DEVICE_ID = "77777777-7777-4777-8777-777777777777"
        const val OTHER_DEVICE_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        const val CREDENTIAL_ID = "88888888-8888-4888-8888-888888888888"
        const val OTHER_CREDENTIAL_ID = "13131313-1313-4313-8313-131313131313"
        const val FINGERPRINT = "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        const val OTHER_FINGERPRINT = "SHA256:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        val IDEMPOTENCY_KEY =
            PairingIdempotencyKey("99999999-9999-4999-8999-999999999999")
        val CANONICAL_UUID =
            Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    }
}
