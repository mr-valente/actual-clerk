package dev.actualclerk.companion

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
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
import java.text.DateFormat
import java.util.Date

/** An Actual account Clerk offered as a target, from the hello call. */
data class Account(val id: String, val name: String, val offBudget: Boolean)

private enum class Screen { Setup, Home, Register, Log }

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val dark = isSystemInDarkTheme()
            MaterialTheme(colorScheme = if (dark) darkColorScheme() else lightColorScheme()) {
                App()
            }
        }
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
private fun App() {
    val context = LocalContext.current
    val prefs = remember { Prefs(context) }
    var screen by remember { mutableStateOf(if (prefs.configured) Screen.Home else Screen.Setup) }
    var accounts by remember { mutableStateOf<List<Account>>(emptyList()) }

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
            onSettings = { screen = Screen.Setup },
        )
        Screen.Register -> RegisterScreen(prefs, accounts, onBack = { screen = Screen.Home })
        Screen.Log -> LogScreen(prefs, onBack = { screen = Screen.Home })
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
    onSettings: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var sources by remember { mutableStateOf(prefs.sources) }
    var budget by remember { mutableStateOf(prefs.lastBudget) }
    var status by remember { mutableStateOf("Checking…") }
    var healthy by remember { mutableStateOf(true) }
    var accessGranted by remember { mutableStateOf(ClerkNotificationListener.isEnabled(context)) }
    var refreshCount by remember { mutableIntStateOf(0) }
    var removing by remember { mutableStateOf<Source?>(null) }

    LaunchedEffect(refreshCount) {
        accessGranted = ClerkNotificationListener.isEnabled(context)
        val result = withContext(Dispatchers.IO) { runCatching { ClerkApi(prefs).hello() } }
        result.onSuccess { hello ->
            val fetched = parseSources(hello)
            prefs.sources = fetched
            sources = fetched
            onAccounts(parseAccounts(hello))
            hello.optJSONObject("budget")?.let { prefs.lastBudget = it; budget = it }
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
                                                notification.title, notification.text,
                                            )
                                        }
                                    }
                                    busy = false
                                    result.onSuccess { reply ->
                                        val source = reply.optJSONObject("source")?.let { Source.fromJson(it) }
                                        if (source != null) {
                                            prefs.sources = prefs.sources.filter { it.packageName != source.packageName } + source
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
                    kind == "unknown" -> "No amount read"
                    else -> status
                }
                val detail = when (status) {
                    "matched" -> "as ${charge.optString("matched_payee")} on ${charge.optString("matched_date")}"
                    else -> charge.optString("noticed_date")
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
