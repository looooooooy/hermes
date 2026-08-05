package app.hermesmobile.connection

import android.content.Context

interface EndpointStore {
    fun load(): String?

    fun save(canonicalEndpoint: String)

    fun clear()
}

class SharedPreferencesEndpointStore(
    context: Context,
    preferenceName: String = DEFAULT_PREFERENCE_NAME,
) : EndpointStore {
    private val preferences = context.applicationContext.getSharedPreferences(
        preferenceName,
        Context.MODE_PRIVATE,
    )

    override fun load(): String? = preferences.getString(ENDPOINT_KEY, null)

    override fun save(canonicalEndpoint: String) {
        require(canonicalEndpoint.isNotBlank()) { "Canonical endpoint is required." }
        check(preferences.edit().putString(ENDPOINT_KEY, canonicalEndpoint).commit()) {
            "Endpoint storage failed."
        }
    }

    override fun clear() {
        check(preferences.edit().remove(ENDPOINT_KEY).commit()) {
            "Endpoint removal failed."
        }
    }

    private companion object {
        const val DEFAULT_PREFERENCE_NAME = "hermes-connection"
        const val ENDPOINT_KEY = "last-successful-endpoint"
    }
}
