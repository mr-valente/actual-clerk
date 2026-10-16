package dev.actualclerk.companion

import org.unifiedpush.android.connector.FailedReason
import org.unifiedpush.android.connector.PushService
import org.unifiedpush.android.connector.data.PushEndpoint
import org.unifiedpush.android.connector.data.PushMessage

/**
 * Where the UnifiedPush distributor on the phone (the ntfy app, say) delivers
 * Clerk's nudges. The content of a nudge is never trusted for anything: any
 * message means "check Clerk now", and the review queue is read over the
 * app's own connection. A new endpoint is handed to Clerk by a worker, with
 * retries, since the phone may be away from home when the distributor
 * issues it.
 */
class ClerkPushService : PushService() {

    override fun onNewEndpoint(endpoint: PushEndpoint, instance: String) {
        val prefs = Prefs(this)
        val auth = endpoint.pubKeySet?.auth.orEmpty()
        val key = endpoint.pubKeySet?.pubKey.orEmpty()
        val changed = endpoint.url != prefs.pushEndpoint || auth != prefs.pushAuth || key != prefs.pushKey
        prefs.pushEndpoint = endpoint.url
        prefs.pushAuth = auth
        prefs.pushKey = key
        prefs.pushError = ""
        // The distributor repeats the same endpoint on every re-registration;
        // Clerk only needs to hear about a new one, or one it never got.
        if (changed || !prefs.deviceRegistered) {
            prefs.deviceRegistered = false
            RegisterDeviceWorker.enqueue(this)
        }
    }

    override fun onMessage(message: PushMessage, instance: String) {
        if (Prefs(this).reviewsEnabled) Reviews.checkNow(this)
    }

    override fun onRegistrationFailed(reason: FailedReason, instance: String) {
        val prefs = Prefs(this)
        prefs.pushEndpoint = ""
        prefs.pushAuth = ""
        prefs.pushKey = ""
        prefs.pushError = "The push app refused the registration ($reason)"
        prefs.deviceRegistered = false
        RegisterDeviceWorker.enqueue(this)
    }

    override fun onUnregistered(instance: String) {
        val prefs = Prefs(this)
        prefs.pushEndpoint = ""
        prefs.pushAuth = ""
        prefs.pushKey = ""
        prefs.pushError = "The push app dropped this registration"
        prefs.deviceRegistered = false
        if (prefs.reviewsEnabled) RegisterDeviceWorker.enqueue(this)
    }

    override fun onTempUnavailable(instance: String) {
        // The distributor's own server is down for the moment; the periodic
        // check covers the gap and the endpoint stays valid.
    }
}
