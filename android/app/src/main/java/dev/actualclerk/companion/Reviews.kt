package dev.actualclerk.companion

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.roundToInt

/** A category the budget offers, as Clerk's last snapshot lists it. */
data class Category(val id: String, val name: String, val groupName: String, val income: Boolean)

/** One bank row or phone charge waiting on the person. */
data class ReviewItem(
    val id: String,
    val groupKey: String,
    val payee: String,
    val amountCents: Long,
    val date: String,
    val account: String,
    val phone: Boolean,
    val phoneText: String,
    val categoryId: String,
    val categoryName: String,
    val proposed: String,
    val confidence: Double,
    val asking: Boolean,
    val reason: String,
)

/**
 * A merchant's worth of waiting items, grouped as the Review tab groups them:
 * one decision settles every row and charge from that merchant.
 */
data class ReviewGroup(
    val key: String,
    val label: String,
    val items: List<ReviewItem>,
    val suggestionId: String,
    val suggestionName: String,
    val proposed: String,
    val confidence: Double,
    val asking: Boolean,
) {
    val ids: List<String> get() = items.map { it.id }
    val totalCents: Long get() = items.sumOf { it.amountCents }
    val phoneCount: Int get() = items.count { it.phone }
    val reason: String get() = items.firstOrNull { it.reason.isNotBlank() }?.reason.orEmpty()
}

data class ReviewQueue(
    val groups: List<ReviewGroup>,
    val categories: List<Category>,
    val symbol: String,
    val reviewUrl: String,
) {
    val count: Int get() = groups.sumOf { it.items.size }
}

/**
 * The review queue on the phone: reading it, grouping it, and keeping the
 * phone's notifications in step with it. Run by a push from Clerk, by the
 * periodic check, and by the review screen itself.
 */
object Reviews {
    const val CHANNEL = "reviews"
    const val EXTRA_GROUP = "review_group"
    const val CHECK_MINUTES = 15L
    private const val PERIODIC_WORK = "review-check"
    private const val IMMEDIATE_WORK = "review-check-now"
    private const val SUMMARY_GROUP = "dev.actualclerk.reviews"

    fun parse(reply: JSONObject): ReviewQueue {
        val symbol = when (reply.optString("currency", "USD")) { "EUR" -> "€"; "GBP" -> "£"; else -> "$" }
        val categories = reply.optJSONArray("categories")?.let { array ->
            (0 until array.length()).mapNotNull { array.optJSONObject(it) }.map {
                Category(it.optString("id"), it.optString("name"), it.optString("group_name"), it.optBoolean("is_income"))
            }
        }.orEmpty()
        val items = reply.optJSONArray("reviews")?.let { array ->
            (0 until array.length()).mapNotNull { array.optJSONObject(it) }.map { json ->
                val phone = json.optJSONObject("phone")
                ReviewItem(
                    id = json.optString("id"),
                    groupKey = json.optString("group_key").ifBlank { json.optString("merchant_key") }.ifBlank { json.optString("id") },
                    payee = json.optString("payee_name").ifBlank { phone?.optString("merchant").orEmpty() }.ifBlank { json.optString("merchant_key") },
                    amountCents = json.optLong("amount_cents"),
                    date = json.optString("transaction_date"),
                    account = json.optString("account_name"),
                    phone = phone != null,
                    phoneText = phone?.optString("text").orEmpty(),
                    categoryId = json.optString("category_id"),
                    categoryName = json.optString("category_name"),
                    proposed = json.optString("proposed_category"),
                    confidence = json.optDouble("confidence", 0.0),
                    asking = json.optBoolean("asking"),
                    reason = json.optString("reason"),
                )
            }
        }.orEmpty()
        return ReviewQueue(group(items), categories, symbol, reply.optString("review_url"))
    }

    /** The bank's name for a shop reads better than a notification's shouting descriptor. */
    private fun group(items: List<ReviewItem>): List<ReviewGroup> =
        items.groupBy { it.groupKey }.map { (key, members) ->
            val named = members.firstOrNull { !it.phone && it.payee.isNotBlank() } ?: members.first()
            val suggested = members.firstOrNull { it.categoryId.isNotBlank() }
            ReviewGroup(
                key = key,
                label = named.payee.ifBlank { "Transaction" },
                items = members,
                suggestionId = suggested?.categoryId.orEmpty(),
                suggestionName = suggested?.categoryName.orEmpty(),
                proposed = members.firstOrNull { it.proposed.isNotBlank() }?.proposed.orEmpty(),
                confidence = members.minOf { it.confidence },
                asking = suggested == null && members.any { it.asking },
            )
        }.sortedWith(compareByDescending<ReviewGroup> { it.items.size }.thenByDescending { abs(it.totalCents) })

    fun fetch(prefs: Prefs): ReviewQueue = parse(ClerkApi(prefs).reviews())

    /** Read the queue and bring the phone's notifications in line with it. */
    fun sync(context: Context): ReviewQueue {
        val prefs = Prefs(context)
        val queue = fetch(prefs)
        reconcile(context, prefs, queue)
        return queue
    }

    fun reconcile(context: Context, prefs: Prefs, queue: ReviewQueue) {
        val manager = NotificationManagerCompat.from(context)
        val announced = prefs.announced.toMutableMap()
        val open = queue.groups.associateBy { it.key }
        for (key in announced.keys.toList()) {
            if (key !in open) {
                manager.cancel(notificationId(key))
                announced.remove(key)
            }
        }
        for (group in queue.groups) {
            // The model is still being asked: wait for the suggestion rather
            // than announce a question and then change it.
            if (group.asking) continue
            val previous = announced[group.key]
            if (previous != null && previous == group.suggestionId) continue
            notify(context, group, queue.symbol, quietly = previous != null)
            announced[group.key] = group.suggestionId
        }
        prefs.announced = announced
        prefs.reviewsWaiting = queue.count
        prefs.lastReviewCheck = System.currentTimeMillis()
    }

    fun clear(context: Context, prefs: Prefs, key: String) {
        NotificationManagerCompat.from(context).cancel(notificationId(key))
        prefs.announced = prefs.announced - key
    }

    fun clearAll(context: Context, prefs: Prefs) {
        val manager = NotificationManagerCompat.from(context)
        for (key in prefs.announced.keys) manager.cancel(notificationId(key))
        prefs.announced = emptyMap()
        prefs.reviewsWaiting = 0
    }

    fun canNotify(context: Context): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL) != null) return
        val channel = NotificationChannel(CHANNEL, "Category questions", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "A charge Clerk could not place, with its suggestion"
        }
        manager.createNotificationChannel(channel)
    }

    private fun notificationId(key: String): Int = 1000 + (key.hashCode() and 0x7fffffff) % 1_000_000

    private fun notify(context: Context, group: ReviewGroup, symbol: String, quietly: Boolean) {
        if (!canNotify(context)) return
        ensureChannel(context)
        val id = notificationId(group.key)
        val open = Intent(context, MainActivity::class.java)
            .putExtra(EXTRA_GROUP, group.key)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val tap = PendingIntent.getActivity(context, id, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val amount = ForwardWorker.formatCents(abs(group.totalCents), symbol)
        val what = when {
            group.items.size == 1 && group.phoneCount == 1 -> "$amount seen by your phone"
            group.items.size == 1 -> amount
            else -> "$amount across ${group.items.size} transactions"
        }
        val hint = when {
            group.suggestionName.isNotBlank() -> "Clerk suggests ${group.suggestionName} (${(group.confidence * 100).roundToInt()}% sure)"
            group.proposed.isNotBlank() -> "Clerk suggests a new category, ${group.proposed}"
            else -> "Clerk has no suggestion"
        }
        val detail = listOfNotNull(
            group.items.firstOrNull { it.phoneText.isNotBlank() }?.phoneText?.take(160)?.let { "“$it”" },
            group.reason.takeIf { it.isNotBlank() }?.take(200),
        ).joinToString("\n")
        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Needs a category: ${group.label}")
            .setContentText("$what · $hint")
            .setStyle(NotificationCompat.BigTextStyle().bigText(listOf("$what · $hint", detail).filter { it.isNotBlank() }.joinToString("\n")))
            .setContentIntent(tap)
            .setAutoCancel(true)
            .setOnlyAlertOnce(quietly)
            .setGroup(SUMMARY_GROUP)
            .setCategory(Notification.CATEGORY_REMINDER)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
        NotificationManagerCompat.from(context).notify(id, notification)
    }

    private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    /** Look every quarter hour, which is also what catches a push that never arrived. */
    fun schedule(context: Context) {
        val request = PeriodicWorkRequestBuilder<ReviewCheckWorker>(CHECK_MINUTES, TimeUnit.MINUTES)
            .setConstraints(online)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC_WORK, ExistingPeriodicWorkPolicy.UPDATE, request)
    }

    fun unschedule(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(PERIODIC_WORK)
    }

    /** Look now: a push arrived, or something changed. */
    fun checkNow(context: Context) {
        val request = OneTimeWorkRequestBuilder<ReviewCheckWorker>()
            .setConstraints(online)
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(IMMEDIATE_WORK, ExistingWorkPolicy.REPLACE, request)
    }
}

/** Reads the queue and keeps the notifications in step with it. */
class ReviewCheckWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val prefs = Prefs(applicationContext)
        if (!prefs.reviewsEnabled || !prefs.configured) return Result.success()
        return try {
            Reviews.sync(applicationContext)
            Result.success()
        } catch (error: ClerkApi.ApiException) {
            if (error.status in 400..499 || runAttemptCount >= 3) Result.failure() else Result.retry()
        } catch (error: IOException) {
            if (runAttemptCount >= 3) Result.failure() else Result.retry()
        }
    }

    // Only consulted on Android 11 and earlier, where expedited work runs as
    // a short foreground service and must show something.
    override fun getForegroundInfo(): ForegroundInfo {
        Reviews.ensureChannel(applicationContext)
        val notification = NotificationCompat.Builder(applicationContext, Reviews.CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Checking Clerk")
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setSilent(true)
            .build()
        return ForegroundInfo(1, notification)
    }
}

/**
 * Tells Clerk how to reach this phone: the push endpoint and keys when the
 * distributor issued some, or just the wish to be asked, which Clerk honours
 * by leaving the question to this phone's periodic check. With reviews
 * turned off, withdraws the registration so the question goes back to ntfy.
 */
class RegisterDeviceWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val prefs = Prefs(applicationContext)
        if (!prefs.configured) return Result.success()
        val api = ClerkApi(prefs)
        return try {
            if (prefs.reviewsEnabled) {
                api.registerDevice(true, prefs.pushEndpoint, prefs.pushAuth, prefs.pushKey)
                prefs.deviceRegistered = true
                Reviews.sync(applicationContext)
            } else {
                api.unregisterDevice()
                prefs.deviceRegistered = false
            }
            Result.success()
        } catch (error: ClerkApi.ApiException) {
            if (error.status in 400..499) {
                prefs.pushError = "Clerk refused the registration: ${error.message}"
                Result.failure()
            } else if (runAttemptCount >= 8) Result.failure() else Result.retry()
        } catch (error: IOException) {
            if (runAttemptCount >= 8) Result.failure() else Result.retry()
        }
    }

    companion object {
        private const val WORK = "register-device"

        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<RegisterDeviceWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(WORK, ExistingWorkPolicy.REPLACE, request)
        }
    }
}
