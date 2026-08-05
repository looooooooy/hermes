package app.hermesmobile.network

import android.security.NetworkSecurityPolicy
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DebugNetworkSecurityPolicyTest {
    @Test
    fun cleartextTrafficIsLimitedToLocalDebugTunnelHosts() {
        val policy = NetworkSecurityPolicy.getInstance()

        assertTrue(policy.isCleartextTrafficPermitted("127.0.0.1"))
        assertTrue(policy.isCleartextTrafficPermitted("localhost"))
        assertTrue(policy.isCleartextTrafficPermitted("10.0.2.2"))
        assertFalse(policy.isCleartextTrafficPermitted("192.168.2.176"))
        assertFalse(policy.isCleartextTrafficPermitted("hermes.example.com"))
    }
}
