package dev.actualclerk.companion

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** One registered app on this phone, mirrored from Clerk. */
data class Source(
    val id: String,
    val packageName: String,
    val appLabel: String,
    val accountId: String,
    val accountName: String,
    val enabled: Boolean,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("package_name", packageName)
        .put("app_label", appLabel)
        .put("actual_account_id", accountId)
        .put("account_name", accountName)
        .put("enabled", enabled)

    companion object {
        fun fromJson(json: JSONObject) = Source(
            id = json.optString("id"),
            packageName = json.optString("package_name"),
            appLabel = json.optString("app_label"),
            accountId = json.optString("actual_account_id"),
            accountName = json.optString("account_name"),
            enabled = json.optBoolean("enabled", true),
        )
    }
}

/** A line in the phone's own log of what it forwarded and what Clerk made of it. */
data class LogEntry(val at: Long, val title: String, val detail: String, val ok: Boolean) {
    fun toJson(): JSONObject = JSONObject().put("at", at).put("title", title).put("detail", detail).put("ok", ok)

    companion object {
        fun fromJson(json: JSONObject) =
            LogEntry(json.optLong("at"), json.optString("title"), json.optString("detail"), json.optBoolean("ok"))
    }
}

/**
 * Everything the app remembers, in one SharedPreferences file. The set of
 * registered sources is what the listener consults on every notification, so
 * it is kept here rather than fetched: the listener must decide in
 * milliseconds and without a network.
 */
class Prefs(context: Context) {
    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences("clerk", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString("server_url", "") ?: ""
        set(value) = prefs.edit().putString("server_url", value.trim().trimEnd('/')).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value.trim()).apply()

    var deviceName: String
        get() = (prefs.getString("device_name", "") ?: "").ifBlank { defaultDeviceName() }
        set(value) = prefs.edit().putString("device_name", value.trim()).apply()

    /** Stable for the life of the install; Clerk keys sources by it. */
    val deviceId: String
        get() {
            val existing = prefs.getString("device_id", null)
            if (existing != null) return existing
            val fresh = UUID.randomUUID().toString()
            prefs.edit().putString("device_id", fresh).apply()
            return fresh
        }

    val configured: Boolean get() = serverUrl.isNotBlank()

    var sources: List<Source>
        get() = readArray("sources").map { Source.fromJson(it) }
        set(value) = prefs.edit().putString("sources", JSONArray(value.map { it.toJson() }).toString()).apply()

    fun sourceFor(packageName: String): Source? = sources.firstOrNull { it.packageName == packageName }

    /** The last budget summary Clerk returned, shown on the home screen. */
    var lastBudget: JSONObject?
        get() = prefs.getString("last_budget", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
        set(value) = prefs.edit().putString("last_budget", value?.toString()).apply()

    val log: List<LogEntry> get() = readArray("log").map { LogEntry.fromJson(it) }

    fun appendLog(entry: LogEntry) {
        val kept = (listOf(entry) + log).take(60)
        prefs.edit().putString("log", JSONArray(kept.map { it.toJson() }).toString()).apply()
    }

    /**
     * Remember a notification's content for a short while so an app that
     * re-posts the same notification (a badge update, a group refresh) does
     * not produce a second charge. Returns true if it was not seen recently.
     */
    @Synchronized
    fun rememberContent(hash: String, now: Long, windowMs: Long = 10 * 60 * 1000L): Boolean {
        val seen = prefs.getString("seen", null)?.let { runCatching { JSONObject(it) }.getOrNull() } ?: JSONObject()
        val last = seen.optLong(hash, 0L)
        if (last > 0 && now - last < windowMs) return false
        seen.put(hash, now)
        // Keep the map small: drop anything older than an hour.
        val pruned = JSONObject()
        for (key in seen.keys()) {
            val at = seen.optLong(key, 0L)
            if (now - at < 60 * 60 * 1000L) pruned.put(key, at)
        }
        prefs.edit().putString("seen", pruned.toString()).apply()
        return true
    }

    private fun readArray(key: String): List<JSONObject> {
        val raw = prefs.getString(key, null) ?: return emptyList()
        val array = runCatching { JSONArray(raw) }.getOrNull() ?: return emptyList()
        return (0 until array.length()).mapNotNull { array.optJSONObject(it) }
    }

    private fun defaultDeviceName(): String =
        listOf(Build.MANUFACTURER, Build.MODEL).filter { it.isNotBlank() }.joinToString(" ")
            .replaceFirstChar { it.uppercase() }
}
