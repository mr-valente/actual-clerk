package dev.actualclerk.companion

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.unifiedpush.android.connector.UnifiedPush
import java.text.DateFormat
import java.util.Date
import kotlin.math.abs
import kotlin.math.roundToInt

/** An Actual account Clerk offered as a target, from the hello call. */
data class Account(val id: String, val name: String, val offBudget: Boolean)

private enum class Screen { Setup, Home, Register, Log, Review }

class MainActivity : ComponentActivity() {
    /** The review group a tapped notification asks for; consumed by the review screen. */
    private val focus = mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        focus.value = intent?.getStringExtra(Reviews.EXTRA_GROUP)
        setContent {
            val dark = isSystemInDarkTheme()
            MaterialTheme(colorScheme = if (dark) darkColorScheme() else lightColorScheme()) {
                App(focus)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.getStringExtra(Reviews.EXTRA_GROUP)?.let { focus.value = it }
    }
}

// ------------------------------------------------------------------ helpers

private fun parseAccounts(hello: JSONObject): List<Account> {
    val array = hello.optJSONArray("accounts") ?: return emptyList()
    return (0 until array.length()).mapNotNull { array.optJSONObject(it) }.map {
        Account(it.optString("id"), it.optString("name"), it.optBoolean("off_budget"))
    }
}

private fun parseSources(hello: JSONObject): List<Source> {
    val array = hello.optJSONArray("sources") ?: return emptyList()
    return (0 until array.length()).mapNotNull { array.optJSONObject(it) }.map { Source.fromJson(it) }
}

private fun budgetLine(budget: JSONObject?): String {
    if (budget == null || !budget.optBoolean("configured", false)) return "Budget not read yet"
    val symbol = when (budget.optString("currency", "USD")) { "EUR" -> "€"; "GBP" -> "£"; else -> "$" }
    val remaining = ForwardWorker.formatCents(budget.optLong("remaining_cents"), symbol)
    val anticipated = budget.optLong("anticipated_cents")
    val count = budget.optInt("anticipated_count")
    val pending = if (anticipated > 0) " · ${ForwardWorker.formatCents(anticipated, symbol)} from $count anticipated" else ""
    return "$remaining free money left$pending"
}

private fun timeLabel(millis: Long): String =
    DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(millis))

private fun openNotificationAccess(context: android.content.Context) {
    context.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

@Composable
private fun SectionTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
}

@Composable
private fun Note(text: String, error: Boolean = false) {
    Text(
        text,
        style = MaterialTheme.typography.bodySmall,
        color = if (error) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Bar(title: String, onBack: (() -> Unit)? = null) {
    TopAppBar(
        title = { Text(title) },
        navigationIcon = {
            if (onBack != null) IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
        },
    )
}

// ---------------------------------------------------------------------- app

@Composable
private fun App(focus: MutableState<String?>) {
    val context = LocalContext.current
    val prefs = remember { Prefs(context) }
    var screen by remember { mutableStateOf(if (prefs.configured) Screen.Home else Screen.Setup) }
    var accounts by remember { mutableStateOf<List<Account>>(emptyList()) }

    // A tapped notification lands on the review screen, whatever was open.
    LaunchedEffect(focus.value) {
        if (focus.value != null && prefs.configured) screen = Screen.Review
    }

    when (screen) {
        Screen.Setup -> SetupScreen(
            prefs,
            canCancel = prefs.configured,
            onDone = { screen = Screen.Home },
        )
        Screen.Home -> HomeScreen(
            prefs,
            onAccounts = { accounts = it },
            onRegister = { screen = Screen.Register },
            onLog = { screen = Screen.Log },
            onReview = { screen = Screen.Review },
            onSettings = { screen = Screen.Setup },
        )
        Screen.Register -> RegisterScreen(prefs, accounts, onBack = { screen = Screen.Home })
        Screen.Log -> LogScreen(prefs, onBack = { screen = Screen.Home })
        Screen.Review -> ReviewScreen(
            prefs,
            focusKey = focus.value,
            onBack = { focus.value = null; screen = Screen.Home },
        )
    }
}

// -------------------------------------------------------------------- setup

@Composable
private fun SetupScreen(prefs: Prefs, canCancel: Boolean, onDone: () -> Unit) {
    var url by remember { mutableStateOf(prefs.serverUrl) }
    var token by remember { mutableStateOf(prefs.token) }
    var name by remember { mutableStateOf(prefs.deviceName) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    Scaffold(topBar = { Bar("Connect to Clerk", onBack = if (canCancel) onDone else null) }) { padding ->
        Column(
            modifier = Modifier.padding(padding).padding(20.dp).fillMaxSize(),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text(
                "This app forwards a card app's notifications to Actual Clerk, so a charge counts against your budget the moment it is made. Nothing is written into Actual Budget.",
                style = MaterialTheme.typography.bodyMedium,
            )
            OutlinedTextField(
                value = url, onValueChange = { url = it }, singleLine = true,
                label = { Text("Clerk server URL") }, placeholder = { Text("http://192.168.1.10:8080") },
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = token, onValueChange = { token = it }, singleLine = true,
                label = { Text("Device token (Clerk: Settings → Phone app)") },
                visualTransformation = PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = name, onValueChange = { name = it }, singleLine = true,
                label = { Text("This phone's name") }, modifier = Modifier.fillMaxWidth(),
            )
            if (error.isNotBlank()) Note(error, error = true)
            Button(
                enabled = !busy && url.isNotBlank(),
                onClick = {
                    busy = true
                    error = ""
                    prefs.serverUrl = url
                    prefs.token = token
                    prefs.deviceName = name
                    scope.launch {
                        val result = withContext(Dispatchers.IO) { runCatching { ClerkApi(prefs).hello() } }
                        busy = false
                        result.onSuccess { hello ->
                            prefs.sources = parseSources(hello)
                            hello.optJSONObject("budget")?.let { prefs.lastBudget = it }
                            onDone()
                        }.onFailure { error = "Could not reach Clerk: ${it.message}" }
                    }
                },
            ) { Text(if (busy) "Connecting…" else "Connect") }
            Note("Use the address you open Clerk at from this phone, on your home network or VPN. A plain http:// address is fine there.")
        }
    }
}

// --------------------------------------------------------------------- home

@Composable
private fun HomeScreen(
    prefs: Prefs,
    onAccounts: (List<Account>) -> Unit,
    onRegister: () -> Unit,
    onLog: () -> Unit,
    onReview: () -> Unit,
    onSettings: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var sources by remember { mutableStateOf(prefs.sources) }
    var reviewsWaiting by remember { mutableIntStateOf(prefs.reviewsWaiting) }
    var budget by remember { mutableStateOf(prefs.lastBudget) }
    var status by remember { mutableStateOf("Checking…") }
    var healthy by remember { mutableStateOf(true) }
    var accessGranted by remember { mutableStateOf(ClerkNotificationListener.isEnabled(context)) }
    var refreshCount by remember { mutableIntStateOf(0) }
    var removing by remember { mutableStateOf<Source?>(null) }

    LaunchedEffect(Unit) {
        // Keep the push registration alive: a distributor that was updated
        // or restarted answers with the current endpoint, and a changed one
        // is passed on to Clerk. The periodic check is re-asserted too.
        if (prefs.reviewsEnabled) {
            Reviews.schedule(context)
            if (UnifiedPush.getAckDistributor(context) != null) {
                runCatching { UnifiedPush.register(context, messageForDistributor = "Actual Clerk's category questions") }
            }
        }
    }

    LaunchedEffect(refreshCount) {
        accessGranted = ClerkNotificationListener.isEnabled(context)
        val result = withContext(Dispatchers.IO) { runCatching { ClerkApi(prefs).hello() } }
        result.onSuccess { hello ->
            val fetched = parseSources(hello)
            prefs.sources = fetched
            sources = fetched
            onAccounts(parseAccounts(hello))
            hello.optJSONObject("budget")?.let { prefs.lastBudget = it; budget = it }
            if (hello.has("reviews_waiting")) {
                reviewsWaiting = hello.optInt("reviews_waiting")
                prefs.reviewsWaiting = reviewsWaiting
            }
            // Clerk forgot this phone (a reset, a restore): ask again.
            if (prefs.reviewsEnabled && hello.isNull("device")) RegisterDeviceWorker.enqueue(context)
            status = "Connected to Clerk ${hello.optString("version")}"
            healthy = true
        }.onFailure {
            status = "Clerk unreachable: ${it.message}"
            healthy = false
        }
    }

    removing?.let { source ->
        AlertDialog(
            onDismissRequest = { removing = null },
            title = { Text("Remove ${source.appLabel}?") },
            text = { Text("Clerk will drop the anticipated charges this app produced. Nothing in Actual changes.") },
            confirmButton = {
                TextButton(onClick = {
                    removing = null
                    scope.launch {
                        withContext(Dispatchers.IO) { runCatching { ClerkApi(prefs).unregisterSource(source.id) } }
                        refreshCount++
                    }
                }) { Text("Remove") }
            },
            dismissButton = { TextButton(onClick = { removing = null }) { Text("Cancel") } },
        )
    }

    Scaffold(topBar = { Bar("Actual Clerk") }) { padding ->
        LazyColumn(
            modifier = Modifier.padding(padding).fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(20.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(budgetLine(budget), style = MaterialTheme.typography.titleLarge)
                        Note(status, error = !healthy)
                        Note(prefs.serverUrl)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            TextButton(onClick = { refreshCount++ }) { Text("Refresh") }
                            TextButton(onClick = onSettings) { Text("Server settings") }
                        }
                    }
                }
            }
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        SectionTitle(if (accessGranted) "Notification access granted" else "Notification access needed")
                        Note(
                            if (accessGranted) "Charges announced by a registered app are forwarded as they arrive."
                            else "Android must allow this app to read notifications. On Android 13 and later an app installed outside the Play Store first needs “Allow restricted settings” from its App info menu (the three dots, top right).",
                        )
                        if (!accessGranted) Button(onClick = { openNotificationAccess(context) }) { Text("Open notification access") }
                        else OutlinedButton(onClick = { openNotificationAccess(context) }) { Text("Notification access settings") }
                    }
                }
            }
            item { ReviewsCard(prefs, reviewsWaiting, onReview) }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    SectionTitle("Sources")
                    Spacer(Modifier.width(12.dp))
                    Note("${sources.size} registered")
                }
            }
            if (sources.isEmpty()) {
                item { Note("No app is registered yet. Register a source, then pick the card app's notification from the list and the Actual account its charges belong to.") }
            }
            items(sources, key = { it.id }) { source ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(source.appLabel.ifBlank { source.packageName }, fontWeight = FontWeight.SemiBold)
                            Note("→ ${source.accountName.ifBlank { source.accountId }}${if (source.enabled) "" else " · paused in Clerk"}")
                            Note(source.packageName)
                        }
                        TextButton(onClick = { removing = source }) { Text("Remove") }
                    }
                }
            }
            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = onRegister, modifier = Modifier.fillMaxWidth(), enabled = accessGranted) { Text("Register a source") }
                    OutlinedButton(onClick = onLog, modifier = Modifier.fillMaxWidth()) { Text("Recent charges") }
                }
            }
            val recent = prefs.log.take(5)
            if (recent.isNotEmpty()) {
                item { SectionTitle("Latest forwarded") }
                items(recent) { entry -> LogRow(entry) }
            }
        }
    }
}

@Composable
private fun LogRow(entry: LogEntry) {
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Row {
            Text(entry.title.ifBlank { "Notification" }, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
            Note(timeLabel(entry.at))
        }
        Note(entry.detail, error = !entry.ok)
        HorizontalDivider(Modifier.padding(top = 6.dp))
    }
}

// ----------------------------------------------------------------- register

@Composable
private fun RegisterScreen(prefs: Prefs, knownAccounts: List<Account>, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var accounts by remember { mutableStateOf(knownAccounts) }
    var notifications by remember { mutableStateOf<List<ActiveNotification>?>(null) }
    var chosen by remember { mutableStateOf<ActiveNotification?>(null) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    var refreshCount by remember { mutableIntStateOf(0) }

    LaunchedEffect(refreshCount) {
        notifications = ClerkNotificationListener.instance?.snapshotActive()
        if (accounts.isEmpty()) {
            withContext(Dispatchers.IO) { runCatching { ClerkApi(prefs).hello() } }
                .onSuccess { accounts = parseAccounts(it) }
                .onFailure { error = "Could not list Actual accounts: ${it.message}" }
        }
    }

    chosen?.let { notification ->
        AlertDialog(
            onDismissRequest = { if (!busy) chosen = null },
            title = { Text(notification.appLabel) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Note("${notification.title}: ${notification.text}".take(200))
                    Text("Which Actual account do this app's charges belong to?", style = MaterialTheme.typography.bodyMedium)
                    if (accounts.isEmpty()) Note("No accounts were offered. Run a sync in Clerk first, then try again.", error = true)
                    accounts.forEach { account ->
                        Text(
                            account.name + if (account.offBudget) "  (off budget: never counted)" else "",
                            modifier = Modifier.fillMaxWidth().clickable(enabled = !busy) {
                                busy = true
                                scope.launch {
                                    val result = withContext(Dispatchers.IO) {
                                        runCatching {
                                            ClerkApi(prefs).registerSource(
                                                notification.packageName, notification.appLabel, account.id,
                                                notification.title, notification.text, notification.postedAt,
                                            )
                                        }
                                    }
                                    busy = false
                                    result.onSuccess { reply ->
                                        val source = reply.optJSONObject("source")?.let { Source.fromJson(it) }
                                        if (source != null) {
                                            prefs.sources = prefs.sources.filter { it.packageName != source.packageName } + source
                                        }
                                        // The sample was forwarded by Clerk on the phone's behalf; log what it became.
                                        reply.optJSONObject("charge")?.let { charge ->
                                            val merchant = charge.optString("merchant").ifBlank { notification.title }
                                            val amount = ForwardWorker.formatCents(charge.optLong("amount_cents"))
                                            val category = charge.optString("category_name")
                                            val detail = when {
                                                charge.optString("status") == "open" && category.isNotBlank() -> "$amount · $category (provisional)"
                                                charge.optString("status") == "open" -> "$amount · no category yet, counted as discretionary"
                                                else -> "$amount · ${charge.optString("status")}"
                                            }
                                            prefs.appendLog(LogEntry(System.currentTimeMillis(), merchant, detail, true))
                                        }
                                        chosen = null
                                        onBack()
                                    }.onFailure {
                                        error = "Could not register: ${it.message}"
                                        chosen = null
                                    }
                                }
                            }.padding(vertical = 10.dp),
                            style = MaterialTheme.typography.bodyLarge,
                        )
                    }
                    if (busy) CircularProgressIndicator()
                }
            },
            confirmButton = {},
            dismissButton = { TextButton(enabled = !busy, onClick = { chosen = null }) { Text("Cancel") } },
        )
    }

    Scaffold(topBar = { Bar("Register a source", onBack) }) { padding ->
        Column(Modifier.padding(padding).padding(20.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Note("Every notification currently on the shade is listed. Tap the one from your card app: from then on, notifications from that app are forwarded to Clerk. Make a small purchase first if the app has nothing showing.")
            if (error.isNotBlank()) Note(error, error = true)
            when (val list = notifications) {
                null -> {
                    Note("The notification listener is not connected. Grant notification access, then come back; Android can take a moment to bind it.", error = true)
                    Button(onClick = { openNotificationAccess(context) }) { Text("Open notification access") }
                    OutlinedButton(onClick = { refreshCount++ }) { Text("Try again") }
                }
                else -> {
                    if (list.isEmpty()) Note("Nothing is on the shade right now.")
                    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.weight(1f)) {
                        items(list) { notification ->
                            val registered = prefs.sourceFor(notification.packageName) != null
                            Card(modifier = Modifier.fillMaxWidth().clickable { chosen = notification }) {
                                Column(Modifier.padding(14.dp)) {
                                    Row {
                                        Text(notification.appLabel, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                                        if (registered) Note("registered")
                                    }
                                    if (notification.title.isNotBlank()) Text(notification.title, style = MaterialTheme.typography.bodyMedium)
                                    Note(notification.text.take(160))
                                }
                            }
                        }
                    }
                    OutlinedButton(onClick = { refreshCount++ }, modifier = Modifier.fillMaxWidth()) { Text("Refresh list") }
                }
            }
        }
    }
}

// ------------------------------------------------------------------ reviews

private fun distributorLabel(context: android.content.Context): String {
    val packageName = UnifiedPush.getAckDistributor(context) ?: UnifiedPush.getSavedDistributor(context) ?: return "the push app"
    val manager = context.packageManager
    return runCatching { manager.getApplicationLabel(manager.getApplicationInfo(packageName, 0)).toString() }
        .getOrDefault(packageName)
}

/**
 * Ask the distributor on the phone for an endpoint. Without one the phone
 * still takes the questions, found by its own check every quarter hour.
 */
private fun requestPush(activity: Activity, prefs: Prefs) {
    if (UnifiedPush.getDistributors(activity).isEmpty()) {
        prefs.pushError = "No push app is installed (the ntfy app is one)"
        RegisterDeviceWorker.enqueue(activity)
        return
    }
    UnifiedPush.tryUseCurrentOrDefaultDistributor(activity) { chosen ->
        if (chosen) {
            UnifiedPush.register(activity, messageForDistributor = "Actual Clerk's category questions")
        } else {
            prefs.pushError = "No push app was chosen"
            RegisterDeviceWorker.enqueue(activity)
        }
    }
}

@Composable
private fun ReviewsCard(prefs: Prefs, waiting: Int, onReview: () -> Unit) {
    val context = LocalContext.current
    var enabled by remember { mutableStateOf(prefs.reviewsEnabled) }
    var refresh by remember { mutableIntStateOf(0) }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { refresh++ }

    fun turnOn() {
        prefs.reviewsEnabled = true
        prefs.pushError = ""
        enabled = true
        Reviews.ensureChannel(context)
        Reviews.schedule(context)
        if (Build.VERSION.SDK_INT >= 33 && !Reviews.canNotify(context)) permission.launch(Manifest.permission.POST_NOTIFICATIONS)
        // Told to Clerk now, so the questions come here from this moment;
        // the push address follows when the distributor issues one.
        RegisterDeviceWorker.enqueue(context)
        (context as? Activity)?.let { requestPush(it, prefs) }
        refresh++
    }

    fun turnOff() {
        prefs.reviewsEnabled = false
        enabled = false
        runCatching { UnifiedPush.unregister(context) }
        prefs.pushEndpoint = ""
        prefs.pushAuth = ""
        prefs.pushKey = ""
        prefs.pushError = ""
        Reviews.unschedule(context)
        Reviews.clearAll(context, prefs)
        RegisterDeviceWorker.enqueue(context)
        refresh++
    }

    val detail = remember(enabled, refresh) {
        when {
            !enabled -> "Clerk asks through ntfy, as before. Turn this on and a charge Clerk cannot place is announced by this app instead; tap it, pick a category, done."
            !Reviews.canNotify(context) -> "Android is not letting this app show notifications. Allow them in the app's settings, or questions only appear when you open Review."
            prefs.pushEndpoint.isNotBlank() -> "Questions arrive the moment Clerk has one, pushed through ${distributorLabel(context)}.${if (prefs.deviceRegistered) "" else " Telling Clerk about this phone…"}"
            prefs.pushError.isNotBlank() -> "${prefs.pushError}. This phone checks Clerk every ${Reviews.CHECK_MINUTES} minutes instead."
            else -> "Waiting for the push app to issue an address; this phone checks Clerk every ${Reviews.CHECK_MINUTES} minutes meanwhile."
        }
    }

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            SectionTitle(if (enabled) "Category questions come here" else "Category questions")
            Note(detail)
            if (enabled && !Reviews.canNotify(context) && Build.VERSION.SDK_INT >= 33) {
                OutlinedButton(onClick = { permission.launch(Manifest.permission.POST_NOTIFICATIONS) }) { Text("Allow notifications") }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                if (enabled) {
                    Button(onClick = onReview) { Text(if (waiting > 0) "Review ($waiting waiting)" else "Review") }
                    TextButton(onClick = { turnOff() }) { Text("Turn off") }
                } else {
                    Button(onClick = { turnOn() }) { Text("Turn on") }
                    TextButton(onClick = onReview) { Text("Review") }
                }
            }
        }
    }
}

@Composable
private fun ReviewScreen(prefs: Prefs, focusKey: String?, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var queue by remember { mutableStateOf<ReviewQueue?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf("") }
    var refresh by remember { mutableIntStateOf(0) }
    // What the person picked instead of the suggestion, per group.
    var choices by remember { mutableStateOf<Map<String, Category>>(emptyMap()) }
    var choosing by remember { mutableStateOf<ReviewGroup?>(null) }

    LaunchedEffect(refresh) {
        loading = true
        error = ""
        val result = withContext(Dispatchers.IO) {
            runCatching {
                val fetched = Reviews.fetch(prefs)
                if (prefs.reviewsEnabled) Reviews.reconcile(context, prefs, fetched)
                fetched
            }
        }
        loading = false
        result.onSuccess { queue = it }.onFailure { error = "Could not read the review queue: ${it.message}" }
    }

    fun resolve(group: ReviewGroup, action: String, always: Boolean = false) {
        val chosen = choices[group.key]
        val categoryId = when (action) {
            "dismiss" -> null
            else -> chosen?.id ?: group.suggestionId.ifBlank { null }
        }
        if (action != "dismiss" && categoryId == null) {
            notice = "Choose a category for ${group.label} first."
            return
        }
        val send = if (action == "dismiss") "dismiss" else if (chosen != null && chosen.id != group.suggestionId) "recategorize" else "accept"
        busy = group.key
        notice = ""
        scope.launch {
            val result = withContext(Dispatchers.IO) {
                runCatching { ClerkApi(prefs).resolveReviews(group.ids, send, if (send == "recategorize") categoryId else null, always) }
            }
            busy = ""
            result.onSuccess {
                queue = queue?.let { current -> current.copy(groups = current.groups.filter { it.key != group.key }) }
                choices = choices - group.key
                Reviews.clear(context, prefs, group.key)
                prefs.reviewsWaiting = queue?.count ?: 0
                val name = chosen?.name ?: group.suggestionName
                notice = when (action) {
                    "dismiss" -> "${group.label} skipped; Clerk will not ask again."
                    else -> "${group.label} filed as $name${if (always) ", and always will be" else ""}."
                }
            }.onFailure { notice = "Could not resolve ${group.label}: ${it.message}" }
        }
    }

    choosing?.let { group ->
        CategoryDialog(
            categories = queue?.categories.orEmpty(),
            selectedId = choices[group.key]?.id ?: group.suggestionId,
            suggestedId = group.suggestionId,
            onPick = { category -> choices = choices + (group.key to category); choosing = null },
            onDismiss = { choosing = null },
        )
    }

    Scaffold(topBar = { Bar("Review", onBack) }) { padding ->
        val groups = queue?.groups.orEmpty().sortedBy { if (it.key == focusKey) 0 else 1 }
        LazyColumn(
            modifier = Modifier.padding(padding).fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (notice.isNotBlank()) item { Note(notice, error = notice.startsWith("Could not") || notice.startsWith("Choose")) }
            if (error.isNotBlank()) item { Note(error, error = true) }
            if (loading && queue == null) item { CircularProgressIndicator() }
            if (!loading && error.isBlank() && groups.isEmpty()) {
                item { Text("Nothing is waiting", style = MaterialTheme.typography.titleMedium) }
                item { Note("Known merchants are filed by themselves. A first-time merchant, from the bank or this phone, appears here with Clerk's suggestion.") }
            }
            items(groups, key = { it.key }) { group ->
                ReviewGroupCard(
                    group = group,
                    symbol = queue?.symbol ?: "$",
                    chosen = choices[group.key],
                    busy = busy == group.key,
                    highlighted = group.key == focusKey,
                    onChoose = { choosing = group },
                    onApply = { resolve(group, "accept") },
                    onAlways = { resolve(group, "accept", always = true) },
                    onSkip = { resolve(group, "dismiss") },
                )
            }
            item {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { refresh++ }, enabled = !loading) { Text("Refresh") }
                    val url = queue?.reviewUrl.orEmpty()
                    if (url.isNotBlank()) TextButton(onClick = { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }) { Text("Open in Clerk") }
                }
            }
        }
    }
}

@Composable
private fun ReviewGroupCard(
    group: ReviewGroup,
    symbol: String,
    chosen: Category?,
    busy: Boolean,
    highlighted: Boolean,
    onChoose: () -> Unit,
    onApply: () -> Unit,
    onAlways: () -> Unit,
    onSkip: () -> Unit,
) {
    val bank = group.items.size - group.phoneCount
    val counted = listOfNotNull(
        if (bank > 0) "$bank transaction${if (bank == 1) "" else "s"}" else null,
        if (group.phoneCount > 0) "${group.phoneCount} phone charge${if (group.phoneCount == 1) "" else "s"}" else null,
    ).joinToString(" + ")
    val dates = group.items.map { it.date }.filter { it.isNotBlank() }.sorted()
    val span = when {
        dates.size > 1 -> "${dates.first()} – ${dates.last()}"
        dates.size == 1 -> dates.first()
        else -> ""
    }
    val accounts = group.items.map { it.account }.filter { it.isNotBlank() }.distinct().take(2).joinToString(", ")
    val selected = chosen?.name ?: group.suggestionName
    val selectionLine = when {
        chosen != null && chosen.id != group.suggestionId -> "Filing as ${chosen.name}" + if (group.suggestionName.isNotBlank()) " (Clerk suggested ${group.suggestionName})" else ""
        group.suggestionName.isNotBlank() -> "Suggested: ${group.suggestionName} · ${(group.confidence * 100).roundToInt()}% sure"
        group.asking -> "Clerk is still asking the model…"
        group.proposed.isNotBlank() -> "Clerk found no fitting category and suggests a new one: ${group.proposed}"
        else -> "No suggestion; choose a category"
    }
    Card(modifier = Modifier.fillMaxWidth(), colors = if (highlighted) androidx.compose.material3.CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer) else androidx.compose.material3.CardDefaults.cardColors()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(group.label + if (group.phoneCount > 0) " 📱" else "", fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                Text(ForwardWorker.formatCents(abs(group.totalCents), symbol), fontWeight = FontWeight.SemiBold)
            }
            Note(listOf(counted, span, accounts).filter { it.isNotBlank() }.joinToString(" · "))
            group.items.firstOrNull { it.phoneText.isNotBlank() }?.let { Note("“${it.phoneText.take(200)}”") }
            if (group.reason.isNotBlank()) Note(group.reason.take(240))
            Text(selectionLine, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.clickable(enabled = !busy) { onChoose() })
            TextButton(onClick = onChoose, enabled = !busy) { Text(if (selected.isBlank()) "Choose a category" else "Change category") }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = onSkip, enabled = !busy) { Text("Skip") }
                Spacer(Modifier.weight(1f))
                OutlinedButton(onClick = onAlways, enabled = !busy && selected.isNotBlank()) { Text("Always") }
                Button(onClick = onApply, enabled = !busy && selected.isNotBlank()) { Text(if (group.items.size > 1) "Apply ${group.items.size}" else "Apply") }
            }
            if (busy) CircularProgressIndicator()
        }
    }
}

@Composable
private fun CategoryDialog(
    categories: List<Category>,
    selectedId: String,
    suggestedId: String,
    onPick: (Category) -> Unit,
    onDismiss: () -> Unit,
) {
    var search by remember { mutableStateOf("") }
    val shown = categories
        .filter { !it.income || it.id == selectedId }
        .filter { search.isBlank() || "${it.groupName} ${it.name}".contains(search.trim(), ignoreCase = true) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Which category?") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = search, onValueChange = { search = it }, singleLine = true,
                    placeholder = { Text("Search categories") }, modifier = Modifier.fillMaxWidth(),
                )
                if (categories.isEmpty()) Note("No categories were offered. Run a sync in Clerk first.", error = true)
                LazyColumn(modifier = Modifier.fillMaxWidth().height(360.dp)) {
                    val grouped = shown.groupBy { it.groupName.ifBlank { "Other" } }
                    grouped.forEach { (groupName, members) ->
                        item(key = "group:$groupName") { Note(groupName); }
                        items(members, key = { it.id }) { category ->
                            Text(
                                category.name + when (category.id) { suggestedId -> "  · suggested"; else -> "" },
                                fontWeight = if (category.id == selectedId) FontWeight.SemiBold else FontWeight.Normal,
                                modifier = Modifier.fillMaxWidth().clickable { onPick(category) }.padding(vertical = 10.dp, horizontal = 4.dp),
                                style = MaterialTheme.typography.bodyLarge,
                            )
                        }
                    }
                    if (shown.isEmpty() && categories.isNotEmpty()) item { Note("No matching categories") }
                }
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

// ---------------------------------------------------------------------- log

private data class ServerCharge(val merchant: String, val amount: String, val status: String, val detail: String)

@Composable
private fun LogScreen(prefs: Prefs, onBack: () -> Unit) {
    var charges by remember { mutableStateOf<List<ServerCharge>>(emptyList()) }
    var error by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(true) }
    val local = remember { prefs.log }

    LaunchedEffect(Unit) {
        val result = withContext(Dispatchers.IO) { runCatching { ClerkApi(prefs).charges() } }
        loading = false
        result.onSuccess { reply ->
            val symbol = when (reply.optJSONObject("budget")?.optString("currency", "USD")) { "EUR" -> "€"; "GBP" -> "£"; else -> "$" }
            val array = reply.optJSONArray("charges")
            charges = (0 until (array?.length() ?: 0)).mapNotNull { array?.optJSONObject(it) }.map { charge ->
                val status = charge.optString("status")
                val kind = charge.optString("kind")
                val label = when {
                    status == "open" && kind == "charge" -> "Anticipated"
                    status == "open" -> "Credit, not counted"
                    status == "matched" -> "Posted"
                    status == "expired" -> "Never posted"
                    status == "dismissed" -> "Dismissed"
                    kind == "declined" -> "Declined"
                    kind == "notice" -> "Just a notice"
                    kind == "unknown" -> "No amount read"
                    else -> status
                }
                val category = charge.optString("category_name")
                val detail = when (status) {
                    "matched" -> "as ${charge.optString("matched_payee")} on ${charge.optString("matched_date")}"
                    else -> listOf(charge.optString("noticed_date"), category).filter { it.isNotBlank() }.joinToString(" · ")
                }
                ServerCharge(
                    charge.optString("merchant").ifBlank { charge.optString("title").ifBlank { "Charge" } },
                    ForwardWorker.formatCents(charge.optLong("amount_cents"), symbol),
                    label,
                    detail,
                )
            }
        }.onFailure { error = "Could not read Clerk's ledger: ${it.message}" }
    }

    Scaffold(topBar = { Bar("Recent charges", onBack) }) { padding ->
        LazyColumn(
            modifier = Modifier.padding(padding).fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(20.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            item { SectionTitle("In Clerk") }
            if (loading) item { CircularProgressIndicator() }
            if (error.isNotBlank()) item { Note(error, error = true) }
            if (!loading && error.isBlank() && charges.isEmpty()) item { Note("Nothing forwarded from this phone yet.") }
            items(charges) { charge ->
                Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                    Row {
                        Text(charge.merchant, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                        Text(charge.amount, fontWeight = FontWeight.SemiBold)
                    }
                    Note("${charge.status} · ${charge.detail}")
                    HorizontalDivider(Modifier.padding(top = 6.dp))
                }
            }
            if (local.isNotEmpty()) {
                item { Spacer(Modifier.height(12.dp)); SectionTitle("Forwarded by this phone") }
                items(local) { entry -> LogRow(entry) }
            }
        }
    }
}
