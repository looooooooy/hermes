package app.hermesmobile.connection

import android.content.Context
import androidx.test.platform.app.InstrumentationRegistry
import java.util.UUID
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

class EndpointStoreTest {
    private lateinit var context: Context
    private lateinit var preferenceName: String
    private lateinit var store: SharedPreferencesEndpointStore

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        preferenceName = "endpoint-store-test-${UUID.randomUUID()}"
        store = SharedPreferencesEndpointStore(context, preferenceName)
    }

    @After
    fun tearDown() {
        context.getSharedPreferences(preferenceName, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
    }

    @Test
    fun canonicalEndpointRoundTripsAndCanBeCleared() {
        val endpoint = "https://api.seaotter.wiki/hermes/"

        store.save(endpoint)

        assertEquals(endpoint, store.load())
        store.clear()
        assertNull(store.load())
    }
}
