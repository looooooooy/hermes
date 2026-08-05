package app.hermesmobile.sessions

import android.content.Context
import app.hermesmobile.protocol.auth.ClientInstanceId
import java.util.UUID

interface ClientInstanceIdStore {
    fun loadOrCreate(): ClientInstanceId
}

interface ClientInstanceIdPersistence {
    fun load(): String?

    fun save(value: String)
}

class PersistentClientInstanceIdStore(
    private val persistence: ClientInstanceIdPersistence,
    private val generate: () -> ClientInstanceId = {
        ClientInstanceId(UUID.randomUUID().toString())
    },
) : ClientInstanceIdStore {
    @Synchronized
    override fun loadOrCreate(): ClientInstanceId {
        val stored = persistence.load()
            ?.let { value -> runCatching { ClientInstanceId(value) }.getOrNull() }
        if (stored != null) return stored

        val created = generate()
        persistence.save(created.value)
        return created
    }
}

class SharedPreferencesClientInstanceIdStore(
    context: Context,
    preferenceName: String = DEFAULT_PREFERENCE_NAME,
) : ClientInstanceIdStore {
    private val preferences = context.applicationContext.getSharedPreferences(
        preferenceName,
        Context.MODE_PRIVATE,
    )
    private val delegate = PersistentClientInstanceIdStore(
        persistence = object : ClientInstanceIdPersistence {
            override fun load(): String? = preferences.getString(CLIENT_INSTANCE_ID_KEY, null)

            override fun save(value: String) {
                check(
                    preferences.edit()
                        .putString(CLIENT_INSTANCE_ID_KEY, value)
                        .commit(),
                ) {
                    "Client instance identity storage failed."
                }
            }
        },
    )

    override fun loadOrCreate(): ClientInstanceId = delegate.loadOrCreate()

    private companion object {
        const val DEFAULT_PREFERENCE_NAME = "hermes-control"
        const val CLIENT_INSTANCE_ID_KEY = "client-instance-id"
    }
}
