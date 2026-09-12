package dev.actualclerk.companion

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Delivers one notification to Clerk. WorkManager owns the retry: the phone
 * may be off Wi-Fi, asleep, or away from home when the card is charged, and
 * the charge must still arrive once it is back. Clerk deduplicates by the
 * notification key, so a retry after a half-delivered request is harmless.
 */
class ForwardWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val prefs = Prefs(applicationContext)
        val packageName = inputData.getString(KEY_PACKAGE) ?: return Result.failure()
        val key = inputData.getString(KEY_KEY) ?: return Result.failure()
        val postedAt = inputData.getLong(KEY_POSTED_AT, 0L)
        val title = inputData.getString(KEY_TITLE).orEmpty()
        val text = inputData.getString(KEY_TEXT).orEmpty()
        val api = ClerkApi(prefs)
        return try {
            val reply = api.forward(packageName, key, postedAt, title, text)
            val charge = reply.optJSONObject("charge")
            val budget = reply.optJSONObject("budget")
            if (budget != null) prefs.lastBudget = budget
            prefs.appendLog(describe(reply, charge, title, text))
            Result.success()
        } catch (error: ClerkApi.ApiException) {
            // A refused request will be refused again: the app is not
            // registered, the token is wrong, or the feature is off. Log it
            // once rather than retrying forever.
            if (error.status in 400..499) {
                prefs.appendLog(LogEntry(System.currentTimeMillis(), title.ifBlank { packageName }, "Clerk refused it: ${error.message}", false))
                return Result.failure()
            }
            retryOrGiveUp(prefs, title, packageName, error.message ?: "server error")
        } catch (error: IOException) {
            retryOrGiveUp(prefs, title, packageName, error.message ?: "network error")
        }
    }

    private fun retryOrGiveUp(prefs: Prefs, title: String, packageName: String, reason: String): Result {
        if (runAttemptCount >= MAX_ATTEMPTS) {
            prefs.appendLog(LogEntry(System.currentTimeMillis(), title.ifBlank { packageName }, "Gave up after $MAX_ATTEMPTS attempts: $reason", false))
            return Result.failure()
        }
        return Result.retry()
    }

    private fun describe(reply: org.json.JSONObject, charge: org.json.JSONObject?, title: String, text: String): LogEntry {
        val now = System.currentTimeMillis()
        if (!reply.optBoolean("accepted", false)) {
            return LogEntry(now, title, "Not accepted: ${reply.optString("reason", "source paused")}", false)
        }
        if (charge == null) return LogEntry(now, title, "Accepted", true)
        val merchant = charge.optString("merchant").ifBlank { title.ifBlank { text.take(40) } }
        val cents = charge.optLong("amount_cents", 0L)
        val amount = formatCents(cents)
        val status = charge.optString("status")
        val kind = charge.optString("kind")
        val detail = when {
            !reply.optBoolean("created", true) -> "$amount · already known"
            status == "open" && kind == "charge" -> "$amount · counted as spent until the bank posts it"
            status == "open" -> "$amount · a credit, recorded but not counted"
            status == "matched" -> "$amount · already in Actual"
            kind == "declined" -> "$amount · declined, not counted"
            kind == "unknown" -> "No amount could be read from this notification"
            else -> "$amount · $status"
        }
        return LogEntry(now, merchant, detail, true)
    }

    companion object {
        private const val KEY_PACKAGE = "package"
        private const val KEY_KEY = "key"
        private const val KEY_POSTED_AT = "posted_at"
        private const val KEY_TITLE = "title"
        private const val KEY_TEXT = "text"
        private const val MAX_ATTEMPTS = 12

        fun enqueue(context: Context, packageName: String, key: String, postedAt: Long, title: String, text: String) {
            val request = OneTimeWorkRequestBuilder<ForwardWorker>()
                .setInputData(
                    Data.Builder()
                        .putString(KEY_PACKAGE, packageName)
                        .putString(KEY_KEY, key)
                        .putLong(KEY_POSTED_AT, postedAt)
                        .putString(KEY_TITLE, title)
                        .putString(KEY_TEXT, text)
                        .build(),
                )
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(key, ExistingWorkPolicy.KEEP, request)
        }

        fun formatCents(cents: Long, currency: String = "$"): String {
            val sign = if (cents < 0) "-" else ""
            val abs = kotlin.math.abs(cents)
            return "$sign$currency${abs / 100}.${(abs % 100).toString().padStart(2, '0')}"
        }
    }
}
