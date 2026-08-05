package app.hermesmobile.sessions

import app.hermesmobile.protocol.auth.ClientInstanceId
import kotlin.test.Test
import kotlin.test.assertEquals

class ClientInstanceIdStoreTest {
    @Test
    fun `stable client instance id is generated once and reused`() {
        val persistence = FakePersistence()
        val generated = ClientInstanceId("11111111-1111-4111-8111-111111111111")
        val store = PersistentClientInstanceIdStore(
            persistence = persistence,
            generate = { generated },
        )

        assertEquals(generated, store.loadOrCreate())
        assertEquals(generated.value, persistence.value)
        assertEquals(generated, store.loadOrCreate())
        assertEquals(1, persistence.saveCount)
    }

    @Test
    fun `invalid persisted client identity is replaced rather than used`() {
        val persistence = FakePersistence("not-a-canonical-uuid")
        val generated = ClientInstanceId("22222222-2222-4222-8222-222222222222")
        val store = PersistentClientInstanceIdStore(
            persistence = persistence,
            generate = { generated },
        )

        assertEquals(generated, store.loadOrCreate())
        assertEquals(generated.value, persistence.value)
        assertEquals(1, persistence.saveCount)
    }

    private class FakePersistence(initial: String? = null) : ClientInstanceIdPersistence {
        var value: String? = initial
        var saveCount = 0

        override fun load(): String? = value

        override fun save(value: String) {
            this.value = value
            saveCount += 1
        }
    }
}
