package app.hermesmobile.protocol.auth

import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl

/** Process-local browser-style session cookies; values are never persisted or rendered. */
class HermesSessionCookieJar(
    private val clockEpochMillis: () -> Long = System::currentTimeMillis,
) : CookieJar {
    private data class CookieKey(
        val name: String,
        val domain: String,
        val path: String,
    )

    private val cookies = linkedMapOf<CookieKey, Cookie>()

    @Synchronized
    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        if (!url.isHttps) return
        val now = clockEpochMillis()
        cookies.forEach { cookie ->
            if (
                cookie.secure &&
                cookie.httpOnly &&
                cookie.name in SESSION_COOKIE_NAMES &&
                cookie.matches(url)
            ) {
                val key = CookieKey(cookie.name, cookie.domain, cookie.path)
                if (cookie.expiresAt <= now) {
                    this.cookies.remove(key)
                } else {
                    this.cookies[key] = cookie
                }
            }
        }
    }

    @Synchronized
    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        if (!url.isHttps) return emptyList()
        val now = clockEpochMillis()
        cookies.entries.removeAll { (_, cookie) -> cookie.expiresAt <= now }
        return cookies.values.filter { it.matches(url) }
    }

    @Synchronized
    fun clear() {
        cookies.clear()
    }

    override fun toString(): String = "HermesSessionCookieJar(cookieCount=${cookies.size})"

    private companion object {
        val SESSION_COOKIE_NAMES = setOf(
            "hermes_session_at",
            "hermes_session_rt",
            "hermes_session_provider",
        )
    }
}
