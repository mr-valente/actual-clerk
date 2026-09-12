package dev.actualclerk.companion

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * The handful of calls the phone makes, over the platform's own HTTP client.
 * No third-party networking library: the payloads are small JSON objects and
 * the endpoints are Clerk's own, so nothing more is needed.
 */
class ClerkApi(private val prefs: Prefs) {

    class ApiException(message: String, val status: Int = 0) : IOException(message)

    fun hello(): JSONObject =
        request("GET", "/api/anticipated/device/hello?device_id=${encode(prefs.deviceId)}")

    fun registerSource(
        packageName: String,
        appLabel: String,
        accountId: String,
        sampleTitle: String,
        sampleText: String,
        samplePostedAtMs: Long,
    ): JSONObject = request(
        "POST",
        "/api/anticipated/device/sources",
        JSONObject()
            .put("device_id", prefs.deviceId)
            .put("device_name", prefs.deviceName)
            .put("package_name", packageName)
            .put("app_label", appLabel)
            .put("actual_account_id", accountId)
            .put("sample_title", sampleTitle.take(400))
            .put("sample_text", sampleText.take(2000))
            .put("sample_posted_at_ms", samplePostedAtMs),
    )

    fun unregisterSource(sourceId: String): JSONObject = request(
        "DELETE",
        "/api/anticipated/device/sources/${encode(sourceId)}?device_id=${encode(prefs.deviceId)}",
    )

    fun forward(packageName: String, key: String, postedAtMs: Long, title: String, text: String): JSONObject =
        request(
            "POST",
            "/api/anticipated/device/notifications",
            JSONObject()
                .put("device_id", prefs.deviceId)
                .put("package_name", packageName)
                .put("notification_key", key)
                .put("posted_at_ms", postedAtMs)
                .put("title", title.take(400))
                .put("text", text.take(4000)),
        )

    fun charges(limit: Int = 60): JSONObject =
        request("GET", "/api/anticipated/device/charges?device_id=${encode(prefs.deviceId)}&limit=$limit")

    private fun request(method: String, path: String, body: JSONObject? = null): JSONObject {
        val base = prefs.serverUrl.ifBlank { throw ApiException("No Clerk server URL is set") }
        val connection = URL(base + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = method
            connection.connectTimeout = 15_000
            connection.readTimeout = 30_000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("User-Agent", "ActualClerkCompanion/${BuildConfig.VERSION_NAME}")
            val token = prefs.token
            if (token.isNotBlank()) connection.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val raw = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            val json = runCatching { JSONObject(raw.ifBlank { "{}" }) }.getOrElse { JSONObject() }
            if (status !in 200..299) {
                val detail = json.optString("detail").ifBlank { json.optString("error") }.ifBlank { "HTTP $status" }
                throw ApiException(detail, status)
            }
            return json
        } finally {
            connection.disconnect()
        }
    }

    private fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")
}
