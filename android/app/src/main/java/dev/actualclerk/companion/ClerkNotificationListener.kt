package dev.actualclerk.companion

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import java.security.MessageDigest

/** A notification currently on the shade, as the registration screen lists them. */
data class ActiveNotification(
    val packageName: String,
    val appLabel: String,
    val title: String,
    val text: String,
    val postedAt: Long,
)

/**
 * The only reason this app exists. The system binds it once the user grants
 * notification access; from then on every posted notification passes through
 * [onNotificationPosted], and the ones from a registered app are handed to a
 * WorkManager job that delivers them to Clerk with retries.
 */
class ClerkNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val prefs = Prefs(this)
        val source = prefs.sourceFor(sbn.packageName) ?: return
        if (!source.enabled) return
        val notification = sbn.notification ?: return
        // A group summary repeats its children; the children are what carry the charge.
        if (notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        val (title, text) = extractText(notification)
        if (title.isBlank() && text.isBlank()) return
        val hash = sha256("${sbn.packageName}\n$title\n$text")
        if (!prefs.rememberContent(hash, System.currentTimeMillis())) return
        val key = "${sbn.packageName}:${sbn.postTime}:${hash.take(16)}"
        ForwardWorker.enqueue(this, sbn.packageName, key, sbn.postTime, title, text)
    }

    /** What is on the shade right now, newest first. */
    fun snapshotActive(): List<ActiveNotification> {
        val manager = packageManager
        return (activeNotifications ?: emptyArray())
            .filter { it.notification.flags and Notification.FLAG_GROUP_SUMMARY == 0 }
            .mapNotNull { sbn ->
                val (title, text) = extractText(sbn.notification)
                if (title.isBlank() && text.isBlank()) return@mapNotNull null
                val label = runCatching {
                    manager.getApplicationLabel(manager.getApplicationInfo(sbn.packageName, 0)).toString()
                }.getOrDefault(sbn.packageName)
                ActiveNotification(sbn.packageName, label, title, text, sbn.postTime)
            }
            .sortedByDescending { it.postedAt }
    }

    companion object {
        @Volatile
        var instance: ClerkNotificationListener? = null

        fun isEnabled(context: Context): Boolean {
            val enabled = Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners") ?: return false
            val self = ComponentName(context, ClerkNotificationListener::class.java)
            return enabled.split(":").any { entry ->
                ComponentName.unflattenFromString(entry) == self || entry == self.flattenToString()
            }
        }

        /**
         * Title and the fullest body the notification carries. Big text
         * supersedes the collapsed line when present; inbox-style lines are
         * joined so nothing an issuer puts in a second line is lost.
         */
        fun extractText(notification: Notification): Pair<String, String> {
            val extras = notification.extras
            val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim().orEmpty()
            val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.trim().orEmpty()
            val big = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.trim().orEmpty()
            val lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
                ?.joinToString(" ") { it.toString().trim() }.orEmpty()
            val body = listOf(big, text, lines).maxByOrNull { it.length }.orEmpty()
            return title to body
        }

        private fun sha256(value: String): String =
            MessageDigest.getInstance("SHA-256").digest(value.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }
    }
}
